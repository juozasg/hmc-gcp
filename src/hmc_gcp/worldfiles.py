"""Rough georeferencing of drone (or phone) photos from their metadata, for QGIS previews.

For each photo a small GDAL .vrt is written that points to the original JPG and carries
an (affine, possibly rotated) geotransform and the CRS. The footprint comes from the GPS
position, the height above take-off, the heading, gimbal pitch and focal length, with
the ground taken as flat at take-off level. Expect metres of error, not centimetres.

Phone photos have GPS but no height above ground and usually no heading or tilt: the
height and heading can be given, and the camera is taken to look straight down.
"""

from __future__ import annotations

import math
import os
from html import escape
from pathlib import Path

import numpy as np
from pyproj import CRS, Transformer

from PIL import Image

from .camera import _num, axes_from_angles, focal_px, read_shot
from .plan import nearest, read_plan

EXTS = {".jpg", ".jpeg"}
PHONE_FOCAL35 = 26.0  # typical main phone camera, used when the photo has no 35 mm focal


def _compass(path: Path):
    """GPSImgDirection (degrees from north) if the photo has it."""
    try:
        with Image.open(path) as im:
            return _num(im.getexif().get_ifd(0x8825).get(17))
    except Exception:
        return None


def _axis_mapping(crs: CRS) -> str:
    """GDAL dataAxisToSRSAxisMapping for a raster (x = easting) in this CRS."""
    ax = crs.axis_info
    if len(ax) >= 2 and ax[0].direction.lower() == "north":
        return "2,1"
    return "1,2"


def _footprint(sh, height: float, n: int = 3):
    """Ground points (east, north offsets from the camera, metres, in a true-north frame)
    for an n x n grid of pixel corners; None if the view does not reach the ground."""
    r, d, f = sh.axes
    pix, ground = [], []
    for v in np.linspace(0, sh.height, n):
        for u in np.linspace(0, sh.width, n):
            ray = r * (u - sh.width / 2) / sh.fpx + d * (v - sh.height / 2) / sh.fpx + f
            if ray[2] > -0.05:  # at or above the horizon
                return None
            t = height / -ray[2]
            pix.append((u, v))
            ground.append((ray[0] * t, ray[1] * t))
    return np.array(pix), np.array(ground)


def _vrt(sh, img_path: Path, vrt_path: Path, gt, srs_wkt: str, mapping: str) -> str:
    rel = os.path.relpath(img_path, vrt_path.parent).replace("\\", "/")
    bands = []
    for i, ci in enumerate(("Red", "Green", "Blue"), start=1):
        bands.append(
            f'  <VRTRasterBand dataType="Byte" band="{i}">\n'
            f"    <ColorInterp>{ci}</ColorInterp>\n"
            f"    <SimpleSource>\n"
            f'      <SourceFilename relativeToVRT="1">{escape(rel)}</SourceFilename>\n'
            f"      <SourceBand>{i}</SourceBand>\n"
            f'      <SrcRect xOff="0" yOff="0" xSize="{sh.width}" ySize="{sh.height}"/>\n'
            f'      <DstRect xOff="0" yOff="0" xSize="{sh.width}" ySize="{sh.height}"/>\n'
            f"    </SimpleSource>\n"
            f"  </VRTRasterBand>\n")
    gts = ", ".join(f"{v:.10f}" for v in gt)
    return (f'<VRTDataset rasterXSize="{sh.width}" rasterYSize="{sh.height}">\n'
            f'  <SRS dataAxisToSRSAxisMapping="{mapping}">{escape(srs_wkt)}</SRS>\n'
            f"  <GeoTransform>{gts}</GeoTransform>\n" + "".join(bands) + "</VRTDataset>\n")


def run(images: Path, out: Path, crs: str = "EPSG:3346", plan: Path | None = None,
        height_offset: float = 0.0, focal35: float | None = None,
        height: float | None = None, heading: float | None = None) -> int:
    """Write one .vrt per photo in `images` to `out`; returns the number written.
    height: camera height above ground for photos without a drone height (phones);
    heading: direction of the top of the photo for photos without one (default north)."""
    photos = sorted(p for p in Path(images).iterdir() if p.suffix.lower() in EXTS)
    if not photos:
        raise SystemExit(f"No JPG photos in {images}")
    out.mkdir(parents=True, exist_ok=True)
    dst = CRS.from_user_input(crs)
    wkt, mapping = dst.to_wkt(), _axis_mapping(dst)
    wps = read_plan(plan) if plan else []
    n_ok, skipped = 0, []
    sources: dict[str, int] = {}
    src_f = False
    gsds: list[float] = []
    for p in photos:
        sh = read_shot(p)
        if sh.lat is None or sh.lon is None:
            skipped.append(f"{p.name}: no GPS")
            continue
        src = "exif" if sh.yaw is not None else None
        if wps:
            w = nearest(wps, sh.lat, sh.lon)
            if w is not None:
                if w.heading is not None:
                    sh.yaw, src = w.heading, "plan"
                if w.pitch is not None:
                    sh.pitch = w.pitch
                if sh.rel_alt is None and w.alt is not None:
                    sh.rel_alt = w.alt
        if sh.yaw is None:
            c = _compass(p)
            if c is not None:
                sh.yaw, src = c, "compass"
            else:
                sh.yaw, src = (heading, "--heading") if heading is not None else (0.0, "assumed north")
        if sh.rel_alt is None:
            if height is None:
                skipped.append(f"{p.name}: no height above ground (give --height, e.g. 1.5)")
                continue
            sh.rel_alt, sh.pitch = height, None
        f35 = focal35 or sh.f35
        if not f35:
            f35, src_f = PHONE_FOCAL35, True
        sh.fpx = focal_px(f35, sh.width, sh.height)
        sh.axes = axes_from_angles(sh.yaw or 0.0, -90.0 if sh.pitch is None else sh.pitch)
        fp = _footprint(sh, sh.rel_alt + height_offset)
        if fp is None:
            skipped.append(f"{p.name}: camera does not look at the ground")
            continue
        pix, ground = fp
        # true-north local frame at the camera -> output CRS
        local = CRS.from_proj4(f"+proj=tmerc +lat_0={sh.lat} +lon_0={sh.lon} +k=1 "
                               f"+x_0=0 +y_0=0 +ellps=WGS84 +units=m")
        tr = Transformer.from_crs(local, dst, always_xy=True)
        X, Y = tr.transform(ground[:, 0], ground[:, 1])
        A = np.c_[np.ones(len(pix)), pix]
        cx, *_ = np.linalg.lstsq(A, np.asarray(X), rcond=None)
        cy, *_ = np.linalg.lstsq(A, np.asarray(Y), rcond=None)
        gt = (cx[0], cx[1], cx[2], cy[0], cy[1], cy[2])
        vrt = out / (p.stem + ".vrt")
        vrt.write_text(_vrt(sh, p.resolve(), vrt.resolve(), gt, wkt, mapping), encoding="utf-8")
        n_ok += 1
        sources[src] = sources.get(src, 0) + 1
        gsds.append(math.hypot(cx[1], cy[1]) * 100)
    if gsds:
        heads = ", ".join(f"{v} from {k}" for k, v in sources.items())
        print(f"  {n_ok} photos, ground pixel {min(gsds):.2f}-{max(gsds):.2f} cm, headings: {heads}")
        if src_f:
            print(f"  some photos have no 35 mm focal length; assumed {PHONE_FOCAL35:.0f} mm "
                  f"(set --focal35)")
    for s in skipped:
        print(f"  skipped {s}")
    return n_ok
