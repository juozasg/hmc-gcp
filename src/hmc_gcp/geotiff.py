"""Minimal GeoTIFF reading and writing (north-up rasters, EPSG codes; no GDAL needed)."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import tifffile

# GeoKey ids
GT_MODEL_TYPE = 1024
GT_RASTER_TYPE = 1025
GEOGRAPHIC_TYPE = 2048
PROJECTED_CS_TYPE = 3072
PROJ_LINEAR_UNITS = 3076


@dataclass
class Grid:
    """North-up raster grid: x0, y0 = top-left corner of the top-left pixel."""
    x0: float
    y0: float
    res: float
    width: int
    height: int
    epsg: int | None

    def col_row(self, x, y):
        """Fractional pixel coordinates (pixel centres at .5 offsets removed: centre = 0)."""
        return (np.asarray(x) - self.x0) / self.res - 0.5, (self.y0 - np.asarray(y)) / self.res - 0.5

    def xy(self, col, row):
        """Map coordinates of pixel centres."""
        return self.x0 + (np.asarray(col) + 0.5) * self.res, self.y0 - (np.asarray(row) + 0.5) * self.res

    @property
    def bounds(self):
        return self.x0, self.y0 - self.height * self.res, self.x0 + self.width * self.res, self.y0


def _geokeys(page) -> dict:
    tag = page.tags.get(34735)
    if tag is None:
        return {}
    v = list(tag.value)
    keys = {}
    for i in range(4, 4 + 4 * v[3], 4):
        kid, loc, cnt, val = v[i:i + 4]
        if loc == 0:
            keys[kid] = val
    return keys


def page_grid(page, path: str = "") -> tuple[Grid, float | None]:
    """Grid and nodata of a GeoTIFF page, without reading pixels."""
    scale = page.tags.get(33550)
    tie = page.tags.get(33922)
    if 34264 in page.tags:
        m = page.tags[34264].value
        if abs(m[1]) > 1e-12 or abs(m[4]) > 1e-12:
            raise ValueError(f"{path}: rotated rasters are not supported")
        sx, sy, x0, y0 = m[0], -m[5], m[3], m[7]
    elif scale is not None and tie is not None:
        sx, sy = scale.value[0], scale.value[1]
        t = tie.value
        x0 = t[3] - t[0] * sx
        y0 = t[4] + t[1] * sy
    else:
        raise ValueError(f"{path}: no georeferencing tags")
    if abs(sx - sy) > 1e-6 * max(sx, sy):
        raise ValueError(f"{path}: non-square pixels ({sx} x {sy}) are not supported")
    keys = _geokeys(page)
    if keys.get(GT_RASTER_TYPE, 1) == 2:  # PixelIsPoint: tie point at pixel centre
        x0 -= sx / 2
        y0 += sy / 2
    epsg = keys.get(PROJECTED_CS_TYPE) or keys.get(GEOGRAPHIC_TYPE)
    if epsg in (None, 32767):
        epsg = None
    nodata = None
    if 42113 in page.tags:
        try:
            nodata = float(str(page.tags[42113].value).strip().strip("\x00"))
        except ValueError:
            nodata = None
    h, w = page.shape[:2]
    return Grid(float(x0), float(y0), float(sx), w, h, epsg), nodata


def read(path: Path, band_first: bool = False):
    """Read a GeoTIFF. Returns (array, Grid, nodata). Pixel data are decoded with tifffile,
    or with OpenCV (libtiff) if tifffile lacks a codec."""
    path = str(path)
    with tifffile.TiffFile(path) as tf:
        page = tf.pages[0]
        grid, nodata = page_grid(page, path)
        h, w = page.shape[:2]
        try:
            arr = page.asarray()
        except Exception:  # missing codec: fall back to libtiff in OpenCV
            arr = cv2.imread(path, cv2.IMREAD_UNCHANGED)
            if arr is None:
                raise
            if arr.ndim == 3:  # OpenCV gives BGR(A)
                arr = arr[..., [2, 1, 0] + ([3] if arr.shape[2] == 4 else [])]
    if arr.ndim == 3 and arr.shape[0] < 8 and arr.shape[1] == h and arr.shape[2] == w:
        arr = np.moveaxis(arr, 0, -1)  # planar -> pixel-interleaved
    return arr, grid, nodata


def _geotags(grid: Grid, nodata: float | None = None) -> list:
    epsg = grid.epsg or 32767
    geokeys = [1, 1, 0, 4,
               GT_MODEL_TYPE, 0, 1, 1,            # projected
               GT_RASTER_TYPE, 0, 1, 1,           # PixelIsArea
               PROJECTED_CS_TYPE, 0, 1, epsg,
               PROJ_LINEAR_UNITS, 0, 1, 9001]     # metres
    extratags = [
        (33550, "d", 3, (grid.res, grid.res, 0.0), True),
        (33922, "d", 6, (0.0, 0.0, 0.0, grid.x0, grid.y0, 0.0), True),
        (34735, "H", len(geokeys), geokeys, True),
    ]
    if nodata is not None:
        extratags.append((42113, "s", 0, f"{nodata:g}", True))
    return extratags


def write(path: Path, arr: np.ndarray, grid: Grid, nodata: float | None = None,
          alpha: bool = False):
    """Write a tiled, zlib-compressed GeoTIFF. arr is H x W or H x W x bands.
    With alpha=True the last band of an RGBA array is written as transparency."""
    extratags = _geotags(grid, nodata)
    kwargs = {}
    if arr.ndim == 3 and arr.shape[2] in (3, 4):
        kwargs["photometric"] = "rgb"
        if arr.shape[2] == 4:
            kwargs["extrasamples"] = ["unassalpha" if alpha else "unspecified"]
    else:
        kwargs["photometric"] = "minisblack"
    big = arr.nbytes > 3_500_000_000
    tifffile.imwrite(str(path), arr, tile=(512, 512), compression="zlib",
                     extratags=extratags, bigtiff=big, metadata=None, **kwargs)


class TileReader:
    """Reads a tiled GeoTIFF tile by tile (pixel-interleaved), for files too large to
    hold in memory."""

    def __init__(self, path: Path):
        self.path = str(path)
        self.tf = tifffile.TiffFile(self.path)
        self.page = p = self.tf.pages[0]
        if not p.is_tiled:
            raise ValueError(f"{path}: not a tiled GeoTIFF")
        self.grid, self.nodata = page_grid(p, self.path)
        self.tw, self.th = p.tilewidth, p.tilelength
        self.ntx = (p.imagewidth + self.tw - 1) // self.tw
        self.nty = (p.imagelength + self.th - 1) // self.th
        self.bands = p.samplesperpixel
        self._lock = threading.Lock()  # tifffile may pull tiles from worker threads

    def close(self):
        self.tf.close()

    def tile(self, tr: int, tc: int) -> np.ndarray:
        """Tile (row tr, column tc) as th x tw x bands; edge tiles are padded."""
        p = self.page
        i = tr * self.ntx + tc
        if p.databytecounts[i] == 0:
            return np.zeros((self.th, self.tw, self.bands), np.uint8)
        with self._lock:
            fh = self.tf.filehandle
            fh.seek(p.dataoffsets[i])
            data = fh.read(p.databytecounts[i])
        a = p.decode(data, i)[0]
        a = np.asarray(a).reshape(self.th, self.tw, -1)
        return a

    def tiles(self):
        """All tiles in file order: (row0, col0, array)."""
        for tr in range(self.nty):
            for tc in range(self.ntx):
                yield tr * self.th, tc * self.tw, self.tile(tr, tc)


def write_tiled_rgba(path: Path, grid: Grid, tile_iter, tile: int, overviews=None):
    """Stream an RGBA image (grid.height x grid.width) to a tiled zlib GeoTIFF. tile_iter
    yields tile x tile x 4 uint8 tiles in row-major order. overviews: optional callable
    returning a list of reduced RGBA arrays, called after the full image is written."""
    big = grid.width * grid.height * 4 > 3_000_000_000
    opts = dict(tile=(tile, tile), compression="zlib", predictor=True, photometric="rgb",
                extrasamples=["unassalpha"], metadata=None)
    with tifffile.TiffWriter(str(path), bigtiff=big) as tw:
        tw.write(tile_iter, shape=(grid.height, grid.width, 4), dtype=np.uint8,
                 extratags=_geotags(grid), **opts)
        for level in (overviews() if overviews else []):
            tw.write(level, subfiletype=1, **opts)
