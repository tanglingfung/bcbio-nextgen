"""High level parallel SNP and indel calling using multiple variant callers.
"""
import os
import collections
import copy

from bcbio import utils
from bcbio.distributed.split import grouped_parallel_split_combine, group_combine_parts
from bcbio.pipeline import region
from bcbio.variation import gatk, gatkfilter, multi, phasing, ploidy, vfilter

# ## Variant filtration -- shared functionality

def variant_filtration(call_file, ref_file, vrn_files, data):
    """Filter variant calls using Variant Quality Score Recalibration.

    Newer GATK with Haplotype calling has combined SNP/indel filtering.
    """
    caller = data["config"]["algorithm"].get("variantcaller")
    call_file = ploidy.filter_vcf_by_sex(call_file, data)
    if caller in ["freebayes"]:
        return vfilter.freebayes(call_file, ref_file, vrn_files, data)
    elif caller in ["gatk", "gatk-haplotype"]:
        return gatkfilter.run(call_file, ref_file, vrn_files, data)
    # no additional filtration for callers that filter as part of call process
    else:
        return call_file

# ## High level functionality to run genotyping in parallel

def get_variantcaller(data):
    return data["config"]["algorithm"].get("variantcaller", "gatk")

def combine_multiple_callers(data):
    """Collapse together variant calls from multiple approaches into variants
    """
    by_bam = collections.defaultdict(list)
    for x in data:
        work_bam = utils.get_in(x[0], ("combine", "work_bam", "out"), x[0]["work_bam"])
        by_bam[work_bam].append(x[0])
    out = []
    for grouped_calls in by_bam.itervalues():
        ready_calls = [{"variantcaller": get_variantcaller(x),
                        "vrn_file": x.get("vrn_file"),
                        "validate": x.get("validate")}
                       for x in grouped_calls]
        final = grouped_calls[0]
        def orig_variantcaller_order(x):
            return final["config"]["algorithm"]["orig_variantcaller"].index(x["variantcaller"])
        if len(ready_calls) > 1 and "orig_variantcaller" in final["config"]["algorithm"]:
            final["variants"] = sorted(ready_calls, key=orig_variantcaller_order)
        else:
            final["variants"] = ready_calls
        out.append([final])
    return out

def _split_by_ready_regions(ext, file_key, dir_ext_fn):
    """Organize splits into pre-built files generated by parallel_prep_region.

    Handles special cases where we need to pass along samples that drive
    batch processing -- like tumor samples in tumor/normal. This enables attaching the
    normal files to these during grouping.
    """
    batch_drivers = ["tumor"]
    def _do_work(data):
        if "region" in data and not data["region"][0] in ["nochrom", "noanalysis"]:
            name = data["group"][0] if "group" in data else data["description"]
            out_dir = os.path.join(data["dirs"]["work"], dir_ext_fn(data))
            out_file = os.path.join(out_dir, "%s%s" % (name, ext))
            out_parts = []
            if not utils.file_exists(out_file) or utils.get_in(data, ("metadata", "phenotype")) in batch_drivers:
                out_region_dir = os.path.join(out_dir, data["region"][0])
                out_region_file = os.path.join(out_region_dir,
                                               "%s-%s%s" % (name, region.to_safestr(data["region"]), ext))
                out_parts = [(data["region"], out_region_file)]
            return out_file, out_parts
        else:
            return None, []
    return _do_work

def parallel_variantcall_region(samples, run_parallel):
    """Perform variant calling and post-analysis on samples by region.
    """
    to_process = []
    extras = []
    to_group = []
    for x in samples:
        added = False
        for add in handle_multiple_variantcallers(x):
            added = True
            to_process.append(add)
        if not added:
            if "combine" in x[0] and x[0]["combine"].keys()[0] in x[0]:
                assert len(x) == 1
                to_group.append(x[0])
            else:
                extras.append(x)
    split_fn = _split_by_ready_regions(".vcf.gz", "work_bam", get_variantcaller)
    if len(to_group) > 0:
        extras += group_combine_parts(to_group)
    return extras + grouped_parallel_split_combine(to_process, split_fn,
                                                   multi.group_batches, run_parallel,
                                                   "variantcall_sample", "concat_variant_files",
                                                   "vrn_file", ["region", "sam_ref", "config"])

def handle_multiple_variantcallers(data):
    """Split samples that potentially require multiple variant calling approaches.
    """
    assert len(data) == 1
    callers = get_variantcaller(data[0])
    if isinstance(callers, basestring):
        return [data]
    elif not callers:
        return []
    else:
        out = []
        for caller in callers:
            base = copy.deepcopy(data[0])
            base["config"]["algorithm"]["orig_variantcaller"] = \
              base["config"]["algorithm"]["variantcaller"]
            base["config"]["algorithm"]["variantcaller"] = caller
            out.append([base])
        return out

def get_variantcallers():
    from bcbio.variation import freebayes, cortex, samtools, varscan, mutect
    return {"gatk": gatk.unified_genotyper,
            "gatk-haplotype": gatk.haplotype_caller,
            "freebayes": freebayes.run_freebayes,
            "cortex": cortex.run_cortex,
            "samtools": samtools.run_samtools,
            "varscan": varscan.run_varscan,
            "mutect": mutect.mutect_caller}

def variantcall_sample(data, region=None, out_file=None):
    """Parallel entry point for doing genotyping of a region of a sample.
    """
    if out_file is None or not os.path.exists(out_file) or not os.path.lexists(out_file):
        utils.safe_makedir(os.path.dirname(out_file))
        sam_ref = data["sam_ref"]
        config = data["config"]
        caller_fns = get_variantcallers()
        caller_fn = caller_fns[config["algorithm"].get("variantcaller", "gatk")]
        if isinstance(data["work_bam"], basestring):
            align_bams = [data["work_bam"]]
            items = [data]
        else:
            align_bams = data["work_bam"]
            items = data["group_orig"]
        call_file = "%s-raw%s" % utils.splitext_plus(out_file)
        call_file = caller_fn(align_bams, items, sam_ref,
                              data["genome_resources"]["variation"],
                              region, call_file)
        if data["config"]["algorithm"].get("phasing", False) == "gatk":
            call_file = phasing.read_backed_phasing(call_file, align_bams, sam_ref, region, config)
        utils.symlink_plus(call_file, out_file)
    data["vrn_file"] = out_file
    return [data]
