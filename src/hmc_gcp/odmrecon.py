"""Read an ODM/OpenSfM reconstruction (camera poses and lens model) and project world points
into the photos."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from pyproj import CRS, Transformer

WGS84_A = 6378137.0
WGS84_E2 = 6.69437999014e-3


def _ecef(lat, lon, alt):
    lat, lon = np.radians(lat), np.radians(lon)
    n = WGS84_A / np.sqrt(1 - WGS84_E2 * np.sin(lat) ** 2)
    x = (n + alt) * np.cos(lat) * np.cos(lon)
    y = (n + alt) * np.cos(lat) * np.sin(lon)
    z = (n * (1 - WGS84_E2) + alt) * np.sin(lat)
    return x, y, z


def _rodrigues(r) -> np.ndarray:
    r = np.asarray(r, dtype=float)
    th = np.linalg.norm(r)
    if th < 1e-12:
        return np.eye(3)
    k = r / th
    K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + math.sin(th) * K + (1 - math.cos(th)) * K @ K


@dataclass
class Camera:
    kind: str
    width: int
    height: int
    fx: float
    fy: float
    cx: float = 0.0
    cy: float = 0.0
    k1: float = 0.0
    k2: float = 0.0
    k3: float = 0.0
    p1: float = 0.0
    p2: float = 0.0

    @classmethod
    def from_json(cls, d: dict) -> "Camera":
        kind = d.get("projection_type", "perspective")
        w, h = int(d["width"]), int(d["height"])
        if kind == "brown":
            return cls(kind, w, h, d["focal_x"], d["focal_y"], d.get("c_x", 0.0), d.get("c_y", 0.0),
                       d.get("k1", 0.0), d.get("k2", 0.0), d.get("k3", 0.0),
                       d.get("p1", 0.0), d.get("p2", 0.0))
        if kind in ("perspective", "simple_radial", "radial"):
            f = d.get("focal", d.get("focal_x"))
            fy = d.get("focal_y", f)
            return cls(kind, w, h, f, fy, d.get("c_x", 0.0), d.get("c_y", 0.0),
                       d.get("k1", 0.0), d.get("k2", 0.0))
        raise ValueError(f"camera projection '{kind}' is not supported (use brown or perspective)")

    def project(self, xc, yc, zc):
        """Camera coordinates -> pixel coordinates (OpenSfM conventions)."""
        x, y = xc / zc, yc / zc
        r2 = x * x + y * y
        radial = 1 + r2 * (self.k1 + r2 * (self.k2 + r2 * self.k3))
        xd = x * radial + 2 * self.p1 * x * y + self.p2 * (r2 + 2 * x * x)
        yd = y * radial + self.p1 * (r2 + 2 * y * y) + 2 * self.p2 * x * y
        size = max(self.width, self.height)
        u = (self.fx * xd + self.cx) * size + self.width / 2.0 - 0.5
        v = (self.fy * yd + self.cy) * size + self.height / 2.0 - 0.5
        return u, v

    @property
    def focal_px(self) -> float:
        return self.fx * max(self.width, self.height)


@dataclass
class Shot:
    name: str
    camera: Camera
    R: np.ndarray        # world (topocentric) -> camera rotation
    t: np.ndarray
    centre: np.ndarray   # camera centre, topocentric
    centre_map: np.ndarray | None = None  # camera centre in the map CRS (x, y, z)

    def project(self, P: np.ndarray):
        """Topocentric points (N x 3) -> pixel u, v and depth."""
        Xc = P @ self.R.T + self.t
        u, v = self.camera.project(Xc[:, 0], Xc[:, 1], Xc[:, 2])
        return u, v, Xc[:, 2]


class Reconstruction:
    def __init__(self, odm_dir: Path):
        odm_dir = Path(odm_dir)
        rfile = odm_dir / "opensfm" / "reconstruction.json"
        if not rfile.exists():
            raise FileNotFoundError(f"{rfile} not found: has ODM finished?")
        recs = json.loads(rfile.read_text())
        rec = max(recs, key=lambda r: len(r.get("shots", {})))
        ref = rec.get("reference_lla")
        if ref is None:
            ref = json.loads((odm_dir / "opensfm" / "reference_lla.json").read_text())
        self.ref = (ref["latitude"], ref["longitude"], ref.get("altitude", 0.0))
        # A georeferenced ODM run exports reconstruction.json to its UTM zone minus the
        # offset in odm_georeferencing/coords.txt (the topocentric original is kept as
        # reconstruction.topocentric.json). Use that frame directly when present.
        self.offset = self.proj_crs = None
        coords = odm_dir / "odm_georeferencing" / "coords.txt"
        if (odm_dir / "opensfm" / "reconstruction.topocentric.json").exists() and coords.exists():
            lines = coords.read_text().split("\n")
            m = re.search(r"UTM\s*(\d+)\s*([NS])", lines[0], re.I)
            if m:
                zone, hemi = int(m.group(1)), m.group(2).upper()
                self.proj_crs = CRS.from_epsg((32600 if hemi == "N" else 32700) + zone)
                ox, oy = (float(v) for v in lines[1].split()[:2])
                self.offset = np.array([ox, oy, 0.0])
        cams = {k: Camera.from_json(v) for k, v in rec["cameras"].items()}
        self.shots: dict[str, Shot] = {}
        for name, s in rec["shots"].items():
            R = _rodrigues(s["rotation"])
            t = np.asarray(s["translation"], dtype=float)
            self.shots[name] = Shot(name, cams[s["camera"]], R, t, -R.T @ t)
        self._fit = None

    # ---- map CRS <-> topocentric ---------------------------------------------------
    def _to_topo_exact(self, x, y, z, crs: CRS):
        tr = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        lon, lat = tr.transform(x, y)
        X, Y, Z = _ecef(lat, lon, z)
        X0, Y0, Z0 = _ecef(*self.ref)
        la, lo = math.radians(self.ref[0]), math.radians(self.ref[1])
        dx, dy, dz = X - X0, Y - Y0, Z - Z0
        e = -math.sin(lo) * dx + math.cos(lo) * dy
        n = -math.sin(la) * math.cos(lo) * dx - math.sin(la) * math.sin(lo) * dy + math.cos(la) * dz
        u = math.cos(la) * math.cos(lo) * dx + math.cos(la) * math.sin(lo) * dy + math.sin(la) * dz
        return np.stack([e, n, u], axis=-1)

    def fit_map(self, crs: CRS, bounds, zrange):
        """Prepare the map -> reconstruction transform over the survey area: exact (an
        offset) for ODM's exported UTM frame, else a quadratic fit to the topocentric one."""
        crs = CRS.from_user_input(crs)
        if self.offset is not None:
            self.crs = crs
            self._to_proj = None if crs == self.proj_crs else \
                Transformer.from_crs(crs, self.proj_crs, always_xy=True)
            self._from_proj = None if crs == self.proj_crs else \
                Transformer.from_crs(self.proj_crs, crs, always_xy=True)
            for s in self.shots.values():
                c = s.centre + self.offset
                if self._from_proj is not None:
                    c[0], c[1] = self._from_proj.transform(c[0], c[1])
                s.centre_map = c
            return 0.0
        xmin, ymin, xmax, ymax = bounds
        xs = np.linspace(xmin, xmax, 25)
        ys = np.linspace(ymin, ymax, 25)
        zs = np.linspace(zrange[0], zrange[1], 4)
        X, Y, Z = (a.ravel() for a in np.meshgrid(xs, ys, zs))
        T = self._to_topo_exact(X, Y, Z, crs)
        self._c = np.array([(xmin + xmax) / 2, (ymin + ymax) / 2])
        A = self._design(X, Y, Z)
        self._coef, *_ = np.linalg.lstsq(A, T, rcond=None)
        err = np.abs(A @ self._coef - T).max()
        if err > 0.002:
            raise RuntimeError(f"map->topocentric fit error {err * 1000:.1f} mm; area too large")
        self.crs = crs
        # camera centres in map coordinates (invert the fit numerically)
        to_map = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        for s in self.shots.values():
            s.centre_map = self._topo_to_map(s.centre, to_map)
        return err

    def _design(self, X, Y, Z):
        x = (np.asarray(X) - self._c[0]) / 1000.0
        y = (np.asarray(Y) - self._c[1]) / 1000.0
        z = np.asarray(Z) / 1000.0
        return np.stack([np.ones_like(x), x, y, z, x * x, x * y, y * y, x * z, y * z], axis=-1)

    def map_to_topo(self, X, Y, Z) -> np.ndarray:
        """Map coordinates -> reconstruction frame (N x 3)."""
        if self.offset is not None:
            X, Y = np.asarray(X, float), np.asarray(Y, float)
            if self._to_proj is not None:
                X, Y = self._to_proj.transform(X, Y)
            return np.stack([X - self.offset[0], Y - self.offset[1], np.asarray(Z, float)], -1)
        return self._design(X, Y, Z) @ self._coef

    def _topo_to_map(self, p, to_map):
        # ENU -> ECEF -> geodetic -> map
        la, lo = math.radians(self.ref[0]), math.radians(self.ref[1])
        e, n, u = p
        X0, Y0, Z0 = _ecef(*self.ref)
        dx = -math.sin(lo) * e - math.sin(la) * math.cos(lo) * n + math.cos(la) * math.cos(lo) * u
        dy = math.cos(lo) * e - math.sin(la) * math.sin(lo) * n + math.cos(la) * math.sin(lo) * u
        dz = math.cos(la) * n + math.sin(la) * u
        X, Y, Z = X0 + dx, Y0 + dy, Z0 + dz
        lon = math.atan2(Y, X)
        pxy = math.hypot(X, Y)
        lat = math.atan2(Z, pxy * (1 - WGS84_E2))
        for _ in range(5):
            nn = WGS84_A / math.sqrt(1 - WGS84_E2 * math.sin(lat) ** 2)
            alt = pxy / math.cos(lat) - nn
            lat = math.atan2(Z, pxy * (1 - WGS84_E2 * nn / (nn + alt)))
        nn = WGS84_A / math.sqrt(1 - WGS84_E2 * math.sin(lat) ** 2)
        alt = pxy / math.cos(lat) - nn
        x, y = to_map.transform(math.degrees(lon), math.degrees(lat))
        return np.array([x, y, alt])
