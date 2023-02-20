"""
OXASL - Quantification using Basil

Includes partial volume correction

Copyright (c) 2008-2020 Univerisity of Oxford
"""

import numpy as np

from fsl.data.image import Image

from oxasl import mask, reg
from oxasl.options import OptionCategory, OptionGroup

from . import fit

try:
    import oxasl_surfpvc
except ImportError:
    oxasl_surfpvc = None

class Options(OptionCategory):
    """
    Options for corrections of the input data
    """

    def __init__(self, **kwargs):
        OptionCategory.__init__(self, "corrections")

    def groups(self, parser):
        ret = []

        g = OptionGroup(parser, "BASIL quantification")
        g.add_option("--basil-options", "--fit-options", help="File containing additional options for Basil model fitting", type="optfile", default=None)
        ret.append(g)

        g = OptionGroup(parser, "BASIL partial volume correction (PVEc)")
        g.add_option("--pvcorr", help="Apply PVEc using FAST estimates taken from --fslanat dir", action="store_true", default=False)
        g.add_option("--surf-pvcorr", help="Apply PVEc using surface PV estimates taken from --fslanat dir w/ surfaces (not mutually exclusive with --pvcorr)", action="store_true", default=False)
        g.add_option('--cores', help="Number of processor cores to use for --surf-pvcorr", type=int)
        g.add_option("--pvgm", help="GM PV estimates in ASL space (apply PVEc only, don't estimate PVs)", type="image", default=None)
        g.add_option("--pvwm", help="As above, WM PV estimates in ASL space", type="image", default=None)
        g.add_option("--pvcsf", help="As above, CSF PV estimates in ASL space", type="image", default=None)
        ret.append(g)

        return ret

def run(wsp):
    # Basic non-PVC run
    fit.run(wsp.sub("basil"))
    wsp.quantify_wsps.append("basil")

    # Re-do registration using PWI as reference
    reg.run(wsp, redo=True, struc_bbr=True, struc_flirt=False, use_quantification_wsp=wsp.basil)

    # Quantification in alternate spaces
    for quantify_space in ("struc", "std", "custom"):
        if wsp.ifnone("quantify_%s" % quantify_space, False):
            quantify_name = "basil_%s" % quantify_space
            quantify_wsp = wsp.sub(quantify_name)
            quantify_wsp.image_space = quantify_space
            fit.run(quantify_wsp) 
            wsp.quantify_wsps.append(quantify_name)

    # If the user has provided manual PV maps (pvgm and pvgm) then do PVEc, even if they
    # have not explicitly given the --pvcorr option 
    wsp.user_pv_flag = ((wsp.pvwm is not None) and (wsp.pvgm is not None))
    if wsp.pvcorr or wsp.surf_pvcorr or wsp.user_pv_flag:
        # Partial volume correction is very sensitive to the mask, so recreate it
        # if it came from the structural image as this requires accurate ASL->Struc registration
        if wsp.rois.mask_src == "struc":
            wsp.rois.mask_orig = wsp.rois.mask
            wsp.rois.mask = None
            mask.run(wsp)

        if wsp.pvcorr or wsp.user_pv_flag:
            _default_pvcorr(wsp)

        if wsp.surf_pvcorr:
            _surf_pvcorr

def _default_pvcorr(wsp):
    # Do partial volume correction fitting
    #
    # FIXME: We could at this point re-apply all corrections derived from structural space?
    # But would need to make sure corrections module re-transforms things like sensitivity map
    
    # Prepare GM and WM partial volume maps from FAST segmentation
    if wsp.user_pv_flag:
        wsp.log.write("\nUsing user-supplied PV estimates\n")
        wsp.structural.wm_pv_asl = wsp.pvwm
        wsp.structural.gm_pv_asl = wsp.pvgm
    else:
        wsp.structural.wm_pv_asl = reg.change_space(wsp, wsp.structural.wm_pv, "asl")
        wsp.structural.gm_pv_asl = reg.change_space(wsp, wsp.structural.gm_pv, "asl")

    wsp.basil_options = wsp.ifnone("basil_options", {})
    wsp.basil_options.update({"pwm" : wsp.structural.wm_pv_asl, 
                                "pgm" : wsp.structural.gm_pv_asl})
    fit.run(wsp.sub("basil_pvcorr"), prefit=False)
    wsp.quantify_wsps.append("basil_pvcorr")

def _surf_pvcorr(wsp):
    if oxasl_surfpvc is None:
        raise RuntimeError("Surface-based PVC requested but oxasl_surfpvc is not installed")
    if wsp.user_pv_flag:
        wsp.log.write(" - WARNING: Performing surface based PVC ignores user-specified PV maps\n")
    # Prepare GM and WM partial volume maps from surface using Toblerone plugin
    # Then reform the ASL ROI mask - Toblerone does not handle the cerebellum so need
    # to mask it out
    oxasl_surfpvc.prepare_surf_pvs(wsp)
    wsp.rois.mask_pvcorr = wsp.rois.mask
    min_pv = 0.01
    new_roi = (wsp.basil_options["pwm"].data > min_pv) | (wsp.basil_options["pgm"].data > min_pv)
    wsp.rois.mask = Image(new_roi.astype(np.int8), header=wsp.rois.mask_pvcorr.header)

    fit.run(wsp.sub("basil_surf_pvcorr"), prefit=False)
    wsp.quantify_wsps.append("basil_surf_pvcorr")