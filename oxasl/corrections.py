#!/bin/env python
"""
Motion and distortion corrections for ASL

The functions in this module calculate linear or non-linear correction transformations
to apply to the ASL and calibration images. Once calculated the ``apply_corrections``
function generates corrected images with the minimum of interpolation.

Currently four sources of transformation exist:

 - Motion correction of the ASL data. This generates a series of linear (rigid body)
   transformations in ASL space, one for each ASL volume. If calibration data is also 
   present a calibration->ASL transform is also generated as part of this process

 - Fieldmap-based distortion correction. This generates a nonlinear warp image
   in structural space which is then transformed to ASL space

 - Phase encoding reversed (CBLIP) distortion correction using TOPUP. This generates
   a nonlinear warp image in ASL space FIXME calibration space?

 - User-supplied nonlinear warp image for gradient distortion corection

Except for the TOPUP correction, all of the above can be combined in a single
transformation to minimise interpolation of the ASL data

Copyright (c) 2008-2013 Univerisity of Oxford
"""
import tempfile
import shutil

import numpy as np

import fsl.wrappers as fsl
from fsl.data.image import Image

from oxasl import reg, struc, calib
from oxasl.options import OptionCategory, IgnorableOptionGroup
from oxasl.reporting import ReportPage, LightboxImage
from oxasl.wrappers import epi_reg

class DistcorrOptions(OptionCategory):
    """
    OptionCategory which contains options for distortion correction
    """

    def __init__(self, **kwargs):
        OptionCategory.__init__(self, "distcorr", **kwargs)

    def groups(self, parser):
        ret = []
        g = IgnorableOptionGroup(parser, "Distortion correction using fieldmap")
        g.add_option("--fmap", help="fieldmap image (in rad/s)", type="image")
        g.add_option("--fmapmag", help="fieldmap magnitude image - wholehead extracted", type="image")
        g.add_option("--fmapmagbrain", help="fieldmap magnitude image - brain extracted", type="image")
        g.add_option("--nofmapreg", help="Do not perform registration of fmap to T1 (use if fmap already in T1-space)", action="store_true", default=False)
        ret.append(g)

        g = IgnorableOptionGroup(parser, "Distortion correction using phase-encode-reversed calibration image (TOPUP)")
        g.add_option("--cblip", help="phase-encode-reversed (blipped) calibration image", type="image")
        ret.append(g)

        g = IgnorableOptionGroup(parser, "General distortion correction options")
        g.add_option("--echospacing", help="Effective EPI echo spacing (sometimes called dwell time) - in seconds", type=float)
        g.add_option("--pedir", help="Phase encoding direction, dir = x/y/z/-x/-y/-z")
        g.add_option("--gdcwarp", help="Additional warp image for gradient distortion correction - will be combined with fieldmap or TOPUP distortion correction", type="image")
        ret.append(g)

        g = IgnorableOptionGroup(parser, "Sensitivity correction")
        g.add_option("--isen", help="User-supplied sensitivity correction in ASL space")
        g.add_option("--senscorr-auto", help="Apply automatic sensitivity correction using bias field from FAST", action="store_true", default=False)
        g.add_option("--senscorr-off", help="Do not apply any sensitivity correction", action="store_true", default=False)
        ret.append(g)

        g = IgnorableOptionGroup(parser, "Partial volume correction")
        g.add_option("--pvcorr", help="Apply partial volume correction", action="store_true", default=False)
        ret.append(g)

        return ret

def get_cblip_correction(wsp):
    """
    Get the cblip based distortion correction warp

    Required workspace attributes
    -----------------------------

     - ``calib`` : Calibration image
     - ``cblip`` : Phase-encode-reversed calibration image
     - ``echospacing`` :
     - ``pedir`` : 

    Optional workspace attributes
    -----------------------------

    Updated workspace attributes
    ----------------------------

     - ``cblip_warp``    : CBLIP Distortion correction warp image
     
    """
    if wsp.isdone("get_cblip_correction"):
        return

    wsp.sub("topup")
    wsp.log.write("Distortion Correction using TOPUP\n")

    topup_params = {
        "x"  : [[1, 0, 0, -99], [-1, 0, 0, -99]],
        "-x" : [[-1, 0, 0, -99], [1, 0, 0, -99]],
        "y"  : [[0, 1, 0, -99], [0, -1, 0, -99]],
        "-y" : [[0, -1, 0, -99], [0, 1, 0, -99]],
        "z"  : [[0, 0, 1, -99], [0, 0, -1, -99]],
        "-z" : [[0, 0, -1, -99], [0, 0, 1, -99]],
    }
    dim_idx = {
        "x"  : 0, "-x" : 0,
        "y"  : 1, "-y" : 1,
        "z"  : 2, "-z" : 2,
    }
    my_topup_params = np.array(topup_params[wsp.pedir], dtype=np.float)
    dimsize = wsp.asldata.shape[dim_idx[wsp.pedir]]
    my_topup_params[:, 3] = wsp.echospacing * (dimsize - 1)
    wsp.topup.params = my_topup_params
    
    # Run TOPUP to calculate correction
    wsp.topup.calib_blipped = Image(np.stack((wsp.calibration.calib_asl.data, wsp.calibration.cblip_asl.data), axis=-1), header=wsp.calibration.calib_asl.header)
    topup_result = fsl.topup(imain=wsp.topup.calib_blipped, datain=wsp.topup.params, config="b02b0.cnf", out=fsl.LOAD, iout=fsl.LOAD, fout=fsl.LOAD)
    wsp.topup.fieldcoef, wsp.topup.movpar = topup_result["out_fieldcoef"], topup_result["out_movpar"]
    wsp.topup.iout = topup_result["iout"]
    wsp.topup.fout = topup_result["fout"]

    page = ReportPage()
    page.heading("TOPUP distortion correction", level=0)
    page.text("PE direction: %s" % wsp.pedir)
    page.text("Echo spacing: %f s" % wsp.echospacing)
    page.heading("Correction image", level=1)
    #for dim in range(3):
    #    #img = Image(wsp.fmap_warp.data[..., dim], header=wsp.fmap_warp.header)
    #    wsp.report.add("fmap_warp%i" % dim, LightboxImage(img))
    #    page.text("Dimension %i" % dim)
    #    page.image("fmap_warp%i.png" % dim) 
    wsp.report.add("topup", page)

    wsp.done("get_cblip_correction")

def get_fieldmap_correction(wsp):
    """
    Get the fieldmap based distortion correction warp

    Required workspace attributes
    -----------------------------

     - ``pwi``          : Perfusion weighted image (generated by preproc_asl)
     - ``fmap``         : Fieldmap image
     - ``fmapmag``      : Fieldmap magnitude image
     - ``fmapmagbrain`` : Fieldmap magnitude brain image
     - ``echospacing``  :
     - ``pedir``        :

    Optional workspace attributes
    -----------------------------

     - ``nofmapreg``       : If True assume fieldmap in structural space

    Updated workspace attributes
    ----------------------------

     - ``fmap_warp``    : Fieldmap distortion correction warp image in ASL space
    """
    if wsp.isdone("get_fieldmap_correction"):
        return

    wsp.sub("fieldmap")
    if wsp.fmap is None or wsp.fmapmag is None or wsp.fmapmagbrain is None:
        wsp.log.write("\nNo fieldmap images for distortion correction\n")
    elif wsp.pedir is None or wsp.echospacing is None:
        wsp.log.write("\nWARNING: Fieldmap images supplied but pedir and echospacing required for distortion correction\n")
    else:
        struc.segment(wsp)

        wsp.log.write("\nDistortion correction from fieldmap images using EPI_REG\n")

        epi_reg_opts = {
            "inweight" : wsp.inweight,
            "init" : wsp.reg.asl2struc,
            "fmap" : wsp.fmap,
            "fmapmag" : wsp.fmapmag,
            "fmapmagbrain" : wsp.fmapmagbrain,
            "pedir" : wsp.pedir,
            "echospacing" : wsp.echospacing,
            "nofmapreg" : wsp.ifnone("nofmapreg", False),
        }
        
        result = epi_reg(epi=wsp.asl.data.perf_weighted(), t1=wsp.structural.struc, t1brain=wsp.structural.brain, out=fsl.LOAD, wmseg=wsp.structural.wm_seg, **epi_reg_opts)
        wsp.fieldmap.warp_struc = result["out_warp"]
        wsp.fieldmap.asl2struc = result["out"]
        wsp.fieldmap.struc2asl = np.linalg.inv(wsp.fieldmap.asl2struc)

        result = fsl.convertwarp(out=fsl.LOAD, ref=wsp.asl.data, warp1=wsp.fieldmap.warp_struc, postmat=wsp.fieldmap.struc2asl, rel=True)
        wsp.fieldmap.warp = result["out"]
        
    page = ReportPage()
    page.heading("Fieldmap distortion correction", level=0)
    page.text("PE direction: %s" % wsp.pedir)
    page.text("Echo spacing: %f s" % wsp.echospacing)
    page.heading("Correction warps", level=1)
    for dim in range(3):
        img = Image(wsp.fieldmap.warp.data[..., dim], header=wsp.fieldmap.warp.header)
        wsp.report.add("fmap_warp%i" % dim, LightboxImage(img))
        page.text("Dimension %i" % dim)
        page.image("fmap_warp%i.png" % dim)
    wsp.report.add("fmap", page)

    wsp.done("get_fieldmap_correction")

def get_motion_correction(wsp):
    """
    Calculate motion correction transforms for ASL data
    
    Note simple motion correction of multi-volume calibration data is done in preprocessing.

    The reference volume for motion correction is the calibration image, if supplied, or
    otherwise the middle volume of the ASL data is used. 
    
    If the calibration image is used, the inverse of the middle ASL volume -> calibration
    transform is applied to each transform matrix. This ensures that the middle volume of 
    the ASL data is unchanged and interpolation on the other volumes is also minimised.
    In this case, all calibration images are also themselves transformed to bring them in
    to ASL middle volume space.

    Required workspace attributes
    -----------------------------

     - ``asldata`` : ASL data image

    Optional workspace attributes
    -----------------------------

     - ``calib``    : Calibration image

    Updated workspace attributes
    ----------------------------

     - ``asldata_mc_mats`` : Sequence of matrices giving motion correction transform for each ASL volume
     - ``asl2calib``       : ASL->calibration image transformation
     - ``calib2asl``       : Calibration->ASL image transformation
    """
    if wsp.isdone("get_motion_correction"):
        return

    calib.init(wsp)
    reg.init(wsp)

    wsp.sub("moco")
    wsp.log.write("\nMotion Correction\n")
    # If available, use the calibration image as reference since this will be most consistent if the data has a range 
    # of different TIs and background suppression etc. This also removes motion effects between asldata and calibration image
    if wsp.calibration.calib:
        wsp.log.write(" - Using calibration image as reference\n")
        ref_source = "calibration image: %s" % wsp.calibration.calib.name
        wsp.moco.ref = wsp.calibration.calib
        wsp.moco.input = wsp.asldata
        mcflirt_result = fsl.mcflirt(wsp.asldata, reffile=wsp.moco.ref, out=fsl.LOAD, mats=fsl.LOAD, log=wsp.fsllog)
        mats = [mcflirt_result["out.mat/MAT_%04i" % vol] for vol in range(wsp.asldata.shape[3])]

        # To reduce interpolation of the ASL data change the transformations so that we end up in the space of the central volume of asldata
        wsp.reg.asl2calib = mats[int(float(len(mats))/2)]
        wsp.reg.calib2asl = np.linalg.inv(wsp.reg.asl2calib)
        mats = [np.dot(wsp.reg.calib2asl, mat) for mat in mats]
        wsp.log.write("   ASL middle volume->Calib:\n%s\n" % str(wsp.reg.asl2calib))
        wsp.log.write("   Calib->ASL middle volume:\n%s\n" % str(wsp.reg.calib2asl))
    else:
        wsp.log.write(" - Using ASL data middle volume as reference\n")
        ref_source = "ASL data %s middle volume: %i" % (wsp.asldata.name, int(float(wsp.asldata.shape[3])/2))
        mcflirt_result = fsl.mcflirt(wsp.asldata, out=fsl.LOAD, mats=fsl.LOAD, log=wsp.fsllog)
        mats = [mcflirt_result["out.mat/MAT_%04i" % vol] for vol in range(wsp.asldata.shape[3])]
        
    # Convert motion correction matrices into single (4*nvols, 4) matrix - convenient for writing
    # to file, and same form that applywarp expects
    wsp.moco.mc_mats = np.concatenate(mats, axis=0)

    page = ReportPage()
    page.heading("Motion correction", level=0)
    page.text("Reference volume: %s" % ref_source)
    page.heading("Motion parameters", level=1)
    for vol, mat in enumerate(mats):
        page.text("Volume %i" % vol)
        page.matrix(mat)
    wsp.report.add("moco", page)

    wsp.done("get_motion_correction")

def get_sensitivity_correction(wsp):
    """
    Get sensitivity correction image
    
    Required workspace attributes
    -----------------------------

     - ``asldata`` : ASL data

    Optional workspace attributes
    -----------------------------

     - ``isen`` : User supplied sensitivity image
     - ``calib`` : Calibration image. Used in conjunction with ``cref`` to calculate sensitivity map
     - ``cref`` : Calibration reference image 
     - ``senscorr_auto`` : If True, automatically calculate sensitivity correction using FAST
     - ``senscorr_off`` If True, do not apply sensitivity correction

    Updated workspace attributes
    ----------------------------

     - ``sensitivity``    : Sensitivity correction image in ASL space
    """
    if wsp.sensitivity is None:
        wsp.log.write("Sensitivity correction\n")
        if wsp.senscorr_off:
            wsp.log.write(" - Sensitivity correction disabled\n")
        elif wsp.isen is not None:
            wsp.log.write(" - Sensitivity image supplied by user\n")
        elif wsp.calibration.calib is not None and wsp.cref is not None:
            wsp.log.write(" - Sensitivity image calculated from calibration reference image\n")
            wsp.sensitivity = Image(wsp.calibration.calib.data / wsp.calibration.cref.data, header=wsp.calibration.calib.header)
        elif wsp.senscorr_auto and wsp.bias is not None:
            struc.segment(wsp)
            wsp.log.write(" - Sensitivity image calculated from bias field\n")
            sens = Image(np.reciprocal(wsp.bias.data), header=wsp.bias.header)
            wsp.sensitivity = reg.struc2asl(wsp, sens)
        else:
            wsp.log.write(" - No source of sensitivity correction was found\n")
        wsp.log.write("\n")

def apply_corrections(wsp):
    """
    Apply distortion and motion corrections to ASL and calibration data

    Required workspace attributes
    -----------------------------

     - ``asldata_orig`` : Uncorrected ASL data image

    Optional workspace attributes
    -----------------------------

     - ``calib_orig``      : Calibration image
     - ``cref_orig``       : Calibration reference image
     - ``cblip_orig``      : Calibration BLIP image
     - ``asldata_mc_mats`` : ASL motion correction matrices
     - ``calib2asl``       : Calibration -> ASL transformation matrix
     - ``distcorr_warp``   : Distortion correction warp image
     - ``gdc_warp``        : Gradient distortion correction warp image

    Updated workspace attributes
    ----------------------------

     - ``asldata``    : Corrected ASL data
     - ``calib``      : Corrected calibration image
     - ``cref``       : Corrected calibration reference image
     - ``cblip``      : Corrected calibration BLIP image
    """
    wsp.log.write("\nApplying corrections to data\n")

    wsp.sub("distcorr")

    if wsp.moco.mc_mats is not None:
        wsp.log.write(" - Using motion correction\n")

    warps = []
    if wsp.fieldmap is not None:
        wsp.log.write(" - Using fieldmap distortion correction\n")
        warps.append(wsp.fieldmap.warp)
    
    if wsp.gdc_warp:
        wsp.log.write(" - Using user-supplied GDC warp\n")
        warps.append(wsp.gdc_warp)
        
    if warps:
        kwargs = {}
        for idx, warp in enumerate(warps):
            kwargs["warp%i" % (idx+1)] = warp
                
        wsp.log.write(" - Converting all warps to single transform and extracting Jacobian\n")
        result = fsl.convertwarp(ref=wsp.asl.data, out=fsl.LOAD, rel=True, jacobian=fsl.LOAD, **kwargs)
        wsp.distcorr.warp = result["out"]
        jacobian = result["jacobian"]
        wsp.distcorr.jacobian = Image(np.mean(jacobian.data, 3), header=jacobian.header)

    if not warps and wsp.moco.mc_mats is None:
        wsp.log.write(" - No corrections to apply\n")
        return

    # Apply all corrections to ASL data - note that we make sure the output keeps all the ASL metadata
    wsp.log.write(" - Applying corrections to ASL data\n")
    asldata_corr = correct_img(wsp, wsp.asldata, wsp.moco.mc_mats)
    wsp.asl.data = wsp.asldata.derived(asldata_corr.data)

    # Apply corrections to calibration images
    if wsp.calib is not None:
        wsp.log.write(" - Applying corrections to calibration data\n")
        wsp.calibration.calib_asl = correct_img(wsp, wsp.calibration.calib, wsp.reg.calib2asl)
    
        if wsp.cref is not None:
            wsp.calibration.cref_asl = correct_img(wsp, wsp.calibration.cref, wsp.reg.calib2asl)
        if wsp.cblip is not None:
            wsp.calibration.cblip_asl = correct_img(wsp, wsp.calibration.cblip, wsp.reg.calib2asl)

    if wsp.topup is not None:
        wsp.log.write(" - Adding TOPUP distortion correction\n")
        # This can't currently be done using the FSL wrappers - we need the TOPUP output as two prefixed files
        # Only workaround currently is to create a temp directory to store appropriately named input files
        topup_input = tempfile.mkdtemp(prefix="topup_input")
        try:
            wsp.topup.fieldcoef.save("%s/topup_fieldcoef" % topup_input)
            movpar_file = open("%s/topup_movpar.txt" % topup_input, "w")
            for row in wsp.topup.movpar:
                movpar_file.write("\t".join([str(val) for val in row]) + "\n")
            movpar_file.close()
            # TOPUP does not do the jacboian magntiude correction - so only okay if using voxelwise calibration
            wsp.calibration.calib_asl_pretopup = wsp.calibration.calib_asl
            wsp.calibration.calib_asl = fsl.applytopup(wsp.calibration.calib_asl, datain=wsp.topup.params, index=1, topup="%s/topup" % topup_input, out=fsl.LOAD, method="jac")["out"]
            wsp.calibration.cblip_asl_pretopup = wsp.calibration.cblip_asl
            wsp.calibration.cblip_asl = fsl.applytopup(wsp.calibration.cblip_asl, datain=wsp.topup.params, index=2, topup="%s/topup" % topup_input, out=fsl.LOAD, method="jac")["out"]
            if wsp.cref:
                wsp.calibration.cref_asl = fsl.applytopup(wsp.calibration.cref_asl, datain=wsp.topup.params, index=1, topup="%s/topup" % topup_input, out=fsl.LOAD, method="jac")["out"]
            wsp.asl.data_pretopup = wsp.asl.data
            post_topup = fsl.applytopup(wsp.asl.data, datain=wsp.topup.params, index=1, topup="%s/topup" % topup_input, out=fsl.LOAD, method="jac")["out"]
            wsp.asl.data = wsp.asl.data.derived(post_topup.data)
            #if wsp.calib_method != "voxel":
            #    wsp.log.write("WARNING: Using TOPUP does not correct for magntiude using the jocbian in distortion correction")
            #    wsp.log.write("         This is not optimal when not using voxelwise calibration\n")
            #    wsp.log.write("         To avoid this supply structural image(s)\n")
        finally:
            shutil.rmtree(topup_input)

def correct_img(wsp, img, linear_mat):
    """
    Apply combined warp/linear transformations to an image
    
    :param img: fsl.data.image.Image to correct
    :param linear_mat: img->ASL space linear transformation matrix.
    :return: Corrected Image

    If a jacobian is present, also corrects for quantitative signal magnitude as volume has been locally scaled

    FIXME there are slight differences to oxford_asl here due to use of spline interpolation rather than
    applyxfm4D which uses sinc interpolation.

    Required workspace attributesd
    -----------------------------

     - ``asldata_mean`` : Mean ASL image used as reference space

    Optional workspace attributes
    -----------------------------

     - ``total_warp``      : Combined warp image
     - ``jacobian``        : Jacobian associated with warp image
    """
    warp_result = fsl.applywarp(img, wsp.asl.data, out=fsl.LOAD, warp=wsp.distcorr.warp, premat=linear_mat, super=True, superlevel="a", interp="trilinear", paddingsize=1, rel=True)
    img = warp_result["out"]
    if wsp.distcorr.jacobian is not None:
        wsp.log.write(" - Correcting for local volume scaling using Jacobian\n")
        jdata = wsp.distcorr.jacobian.data
        if img.data.ndim == 4:
            # Required to make broadcasting work
            jdata = jdata[..., np.newaxis]
        img = Image(img.data * jdata, header=img.header)
    return img

def apply_sensitivity_correction(wsp, *imgs):
    """
    Apply sensitivity correction

    :param imgs: Sequence of Image objects

    :return: Tuple of corrected Image objects corresponding to input.
             If no sensitivity correction is defined, returns the same
             images as input.

    Optional workspace attributes
    -----------------------------

     - ``sensitivity``  : Sensitivity correction image
     - ``senscorr_off`` : If True, no correction will be applied even if ``sensitivity`` image exists
    """
    if wsp.sensitivity is not None and not wsp.senscorr_off:
        wsp.log.write("Applying sensitivity correction\n")
        ret = []
        for img in imgs:
            corrected = img.data / wsp.sensitivity.data
            ret.append(Image(corrected, header=img.header))
        return tuple(ret)
    else:
        return tuple(imgs)
