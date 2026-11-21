"""Tagging session: GCPs, photos, predictions, detections, saved state and export."""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from pyproj import CRS, Transformer

from . import gpkg
from .camera import Shot, axes_from_angles, focal_px, read_shot
from .detect import detect_target
from .imgio import ImageCache, crop_padded
from .plan import nearest, read_plan

ACCEPTED = ("accepted", "manual", "auto")
STATE_FILE = "tags.json"
MAX_RADIUS_PX = 1500


@dataclass
class Item:
    gcp: str
    image: str
    status: str = "pending"  # pending | accepted | manual | auto | absent | outside
    px: list | None = None   # confirmed pixel [x, y] in the full-size photo
    score: float | None = None
    det: dict | None = None
    pred: list | None = None

    @property
    def key(self) -> str:
        return f"{self.gcp}|{self.image}"

    def to_json(self) -> dict:
        return {"gcp": self.gcp, "image": self.image, "status": self.status, "px": self.px,
                "score": self.score, "det": self.det, "pred": self.pred}


def _log(msg: str):
    print(msg, flush=True)


class Session:
    def __init__(self, images_dir: Path, out_dir: Path, settings: dict):
        self.images_dir = Path(images_dir)
        self.out = Path(out_dir)
        self.settings = settings
        self.cache = ImageCache(4)
        self.gcps: list[dict] = []
        self.gcp_by: dict[str, dict] = {}
        self.shots: dict[str, Shot] = {}
        self.items: list[Item] = []
        self.srs: gpkg.SRS | None = None
        self.img_off: dict[str, np.ndarray] = {}
        self.glob_off = np.zeros(3)
        self.n_confirmed = 0

    # ------------------------------------------------------------ setup
    @classmethod
    def create(cls, images_dir: Path, gpkg_path: Path, layer: str, out_dir: Path, *,
               name_field=None, z_field=None, takeoff_alt=None, plan=None,
               target_size=0.40, search_radius=10.0, refine_radius=2.5,
               focal35=None, min_images=2, checkpoints=()) -> "Session":
        settings = dict(target_size=target_size, search_radius=search_radius,
                        refine_radius=refine_radius, min_images=min_images,
                        checkpoints=list(checkpoints), takeoff_alt=takeoff_alt,
                        gpkg=str(gpkg_path), layer=layer)
        s = cls(images_dir, out_dir, settings)
        s.out.mkdir(parents=True, exist_ok=True)

        pts, s.srs = gpkg.read_points(gpkg_path, layer, name_field, z_field)
        if not pts:
            raise SystemExit("No points found in the GCP layer.")
        missing_z = [p.name for p in pts if p.z is None]
        if missing_z:
            _log(f"WARNING: no height for {', '.join(missing_z)}; using 0.0. "
                 "Use a 3D layer or --z-field.")
        src_crs = CRS.from_epsg(s.srs.org_id) if s.srs.is_epsg else CRS.from_wkt(s.srs.definition)

        paths = sorted(p for p in s.images_dir.iterdir()
                       if p.suffix.lower() in (".jpg", ".jpeg") and not p.stem.endswith("_mask"))
        if not paths:
            raise SystemExit(f"No JPG photos in {s.images_dir}")
        _log(f"Reading metadata of {len(paths)} photos...")
        for p in paths:
            sh = read_shot(p)
            if sh.lat is None or sh.lon is None:
                _log(f"  skipping {p.name}: no GPS position")
                continue
            s.shots[sh.name] = sh

        wps = read_plan(plan) if plan else []
        if plan:
            _log(f"Flight plan: {len(wps)} waypoints")

        lat0 = float(np.mean([sh.lat for sh in s.shots.values()]))
        lon0 = float(np.mean([sh.lon for sh in s.shots.values()]))
        local = CRS.from_proj4(f"+proj=tmerc +lat_0={lat0} +lon_0={lon0} +k=1 +x_0=0 +y_0=0 "
                               "+ellps=GRS80 +units=m +no_defs")
        wgs_to_local = Transformer.from_crs("EPSG:4326", local, always_xy=True)
        src_to_local = Transformer.from_crs(src_crs, local, always_xy=True)

        for p in pts:
            E, N = src_to_local.transform(p.x, p.y)
            g = {"name": p.name, "x": p.x, "y": p.y,
                 "z": 0.0 if p.z is None else p.z, "E": float(E), "N": float(N)}
            s.gcps.append(g)
            s.gcp_by[g["name"]] = g

        if takeoff_alt is None:
            takeoff_alt = float(np.median([g["z"] for g in s.gcps]))
            _log(f"WARNING: --takeoff-alt not given; assuming take-off at the median GCP "
                 f"height ({takeoff_alt:.2f}). Predictions may be off if you took off higher.")
            s.settings["takeoff_alt_assumed"] = takeoff_alt

        n_plan = n_noyaw = 0
        for sh in s.shots.values():
            if wps:
                w = nearest(wps, sh.lat, sh.lon)
                if w is not None:
                    n_plan += 1
                    if w.heading is not None:
                        sh.yaw, sh.yaw_source = w.heading, "plan"
                    if w.pitch is not None:
                        sh.pitch = w.pitch
                    if sh.rel_alt is None and w.alt is not None:
                        sh.rel_alt = w.alt
            if sh.yaw is None:
                sh.yaw = 0.0
                n_noyaw += 1
            if sh.pitch is None:
                sh.pitch = -90.0
            E, N = wgs_to_local.transform(sh.lon, sh.lat)
            sh.E, sh.N = float(E), float(N)
            if sh.rel_alt is not None:
                sh.Z = takeoff_alt + sh.rel_alt
            elif sh.abs_alt is not None:
                sh.Z = sh.abs_alt
            else:
                sh.Z = takeoff_alt + 30.0
            f35 = focal35 or sh.f35 or 24.0
            sh.fpx = focal_px(f35, sh.width, sh.height)
            sh.axes = axes_from_angles(sh.yaw, sh.pitch)
        if wps:
            _log(f"  {n_plan} photos matched to waypoints")
        if n_noyaw:
            _log(f"WARNING: {n_noyaw} photos have no heading; assumed north-up.")
        if not focal35 and not any(sh.f35 for sh in s.shots.values()):
            _log("WARNING: no 35 mm focal length in EXIF; assuming 24 mm (use --focal35).")

        s._build_items()
        s._load_state()
        s.update_offsets()
        _log(f"{len(s.gcps)} GCPs, {len(s.shots)} photos, {len(s.items)} GCP-photo pairs to review")
        return s

    def _build_items(self):
        order = {g["name"]: i for i, g in enumerate(self.gcps)}
        items = []
        for sh in self.shots.values():
            for g in self.gcps:
                p = self.project(g, sh)
                if p is None:
                    continue
                gsd = self.gsd(g, sh)
                m = min(self.settings["search_radius"] / gsd, MAX_RADIUS_PX)
                if -m <= p[0] <= sh.width + m and -m <= p[1] <= sh.height + m:
                    items.append(Item(g["name"], sh.name, pred=[float(p[0]), float(p[1])]))
        items.sort(key=lambda it: (order[it.gcp], it.image))
        self.items = items

    # ------------------------------------------------------------ geometry
    def gsd(self, g: dict, sh: Shot) -> float:
        return max(2.0, sh.Z - g["z"]) / sh.fpx

    def project(self, g: dict, sh: Shot, off=None):
        C = np.array([sh.E, sh.N, sh.Z])
        if off is not None:
            C = C + off
        v = np.array([g["E"], g["N"], g["z"]]) - C
        r, d, f = sh.axes
        zc = float(v @ f)
        if zc <= 0.5:
            return None
        return sh.cx + sh.fpx * float(v @ r) / zc, sh.cy + sh.fpx * float(v @ d) / zc

    def backproject(self, u: float, v: float, sh: Shot, z: float):
        r, d, f = sh.axes
        ray = r * (u - sh.cx) / sh.fpx + d * (v - sh.cy) / sh.fpx + f
        if abs(ray[2]) < 1e-6:
            return None
        C = np.array([sh.E, sh.N, sh.Z])
        t = (z - sh.Z) / ray[2]
        return None if t <= 0 else C + t * ray

    def update_offsets(self):
        """Estimate the photo position error from confirmed tags."""
        per: dict[str, list] = {}
        allo = []
        for it in self.items:
            if it.status in ACCEPTED and it.px:
                g, sh = self.gcp_by[it.gcp], self.shots[it.image]
                G = self.backproject(it.px[0], it.px[1], sh, g["z"])
                if G is None:
                    continue
                o = np.array([g["E"], g["N"], g["z"]]) - G
                o[2] = 0.0
                per.setdefault(it.image, []).append(o)
                allo.append(o)
        self.img_off = {k: np.mean(v, axis=0) for k, v in per.items()}
        self.glob_off = np.median(np.array(allo), axis=0) if allo else np.zeros(3)
        self.n_confirmed = len(allo)
        self.refresh_outside()

    def refresh_outside(self):
        """Once the position error is known, park pairs predicted outside the photo."""
        if self.n_confirmed < 2:
            return
        for it in self.items:
            if it.status not in ("pending", "outside"):
                continue
            sh = self.shots[it.image]
            pred, rad, exp = self.predict(it)
            m = 0.5 * rad
            inside = -m <= pred[0] <= sh.width + m and -m <= pred[1] <= sh.height + m
            it.status = "pending" if inside else "outside"

    def offset(self, image: str) -> np.ndarray:
        return self.img_off.get(image, self.glob_off)

    def radius_m(self, image: str) -> float:
        if image in self.img_off:
            return 0.6 * self.settings["refine_radius"]
        if self.n_confirmed >= 2:
            return self.settings["refine_radius"]
        return self.settings["search_radius"]

    def predict(self, it: Item):
        """Update and return (pred [x, y], search radius px, expected target size px)."""
        g, sh = self.gcp_by[it.gcp], self.shots[it.image]
        p = self.project(g, sh, self.offset(it.image))
        if p is not None:
            it.pred = [float(p[0]), float(p[1])]
        elif it.pred is None:
            it.pred = [sh.cx, sh.cy]
        gsd = self.gsd(g, sh)
        rad = min(self.radius_m(it.image) / gsd, MAX_RADIUS_PX)
        return it.pred, rad, self.settings["target_size"] / gsd

    def detect(self, it: Item, img=None):
        pred, rad, exp = self.predict(it)
        if img is None:
            img = self.cache.get(self.shots[it.image].path)
        x0, y0 = int(round(pred[0] - rad)), int(round(pred[1] - rad))
        size = int(2 * rad) + 1
        crop, valid = crop_padded(img, x0, y0, x0 + size, y0 + size, with_mask=True)
        res = detect_target(crop, (pred[0] - x0, pred[1] - y0), exp, rad, valid)
        sh = self.shots[it.image]
        if res:
            res["center"] = [res["center"][0] + x0, res["center"][1] + y0]
            res["box"] = [[a + x0, b + y0] for a, b in res["box"]]
            cx, cy = res["center"]
            if not (0 <= cx < sh.width and 0 <= cy < sh.height):
                res = None
        it.det = res or {"center": None}
        it.det["pred"] = list(pred)
        it.score = res["score"] if res else 0.0
        return it.det

    def needs_detect(self, it: Item) -> bool:
        if it.status != "pending":
            return False
        if not it.det or "pred" not in it.det:
            return True
        pred, rad, _ = self.predict(it)
        old = it.det["pred"]
        return math.hypot(pred[0] - old[0], pred[1] - old[1]) > 0.3 * rad

    def auto_pass(self, threshold: float):
        """Detect targets in all unreviewed pairs; accept confident ones with a centre dot."""
        todo = sorted((it for it in self.items if it.status == "pending"),
                      key=lambda it: it.image)
        _log(f"Detecting targets in {len(todo)} crops...")
        n = 0
        for k, it in enumerate(todo, 1):
            if it.status == "pending":
                self.detect(it)
                if it.det.get("center") and it.det.get("has_dot") and (it.score or 0) >= threshold:
                    it.status, it.px = "auto", list(it.det["center"])
                    n += 1
            if k % 10 == 0:
                self.update_offsets()  # sharpen predictions as tags come in
            if k % 20 == 0:
                _log(f"  {k}/{len(todo)}")
        self.update_offsets()
        _log(f"Auto-accepted {n} crops with score >= {threshold} and a centre dot")
        self.save()

    # ------------------------------------------------------------ state
    def _load_state(self):
        f = self.out / STATE_FILE
        if not f.exists():
            return
        data = json.loads(f.read_text(encoding="utf-8"))
        old = {f"{d['gcp']}|{d['image']}": d for d in data.get("items", [])}
        n = 0
        for it in self.items:
            d = old.get(it.key)
            if d:
                it.status, it.px, it.score, it.det = d["status"], d["px"], d.get("score"), d.get("det")
                n += it.status not in ("pending", "outside")
        _log(f"Resumed {n} reviewed crops from {f}")

    def save(self):
        for it in self.items:
            self.predict(it)
        data = {
            "version": 1,
            "images_dir": str(self.images_dir.resolve()),
            "settings": self.settings,
            "crs": {"header": self.crs_header(), "srs_id": self.srs.srs_id},
            "offset_m": [round(float(v), 3) for v in self.glob_off[:2]],
            "gcps": [{k: g[k] for k in ("name", "x", "y", "z")} for g in self.gcps],
            "images": [{"name": sh.name, "width": sh.width, "height": sh.height,
                        "lat": sh.lat, "lon": sh.lon, "yaw": sh.yaw,
                        "yaw_source": sh.yaw_source} for sh in self.shots.values()],
            "items": [it.to_json() for it in self.items],
        }
        tmp = self.out / (STATE_FILE + ".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(self.out / STATE_FILE)

    def crs_header(self) -> str:
        if self.srs.is_epsg:
            return f"EPSG:{self.srs.org_id}"
        return CRS.from_wkt(self.srs.definition).to_proj4()

    # ------------------------------------------------------------ export
    def export(self) -> dict:
        tagged: dict[str, list[Item]] = {g["name"]: [] for g in self.gcps}
        for it in self.items:
            if it.status in ACCEPTED and it.px:
                tagged[it.gcp].append(it)
        forced = set(self.settings.get("checkpoints") or [])
        min_img = self.settings["min_images"]
        control, checks, untagged = [], [], []
        for g in self.gcps:
            t = tagged[g["name"]]
            if not t:
                untagged.append(g)
            elif g["name"] in forced or len(t) < min_img:
                checks.append((g, t))
            else:
                control.append((g, t))

        lines = [self.crs_header()]
        for g, t in control:
            for it in t:
                lines.append(f"{g['x']:.4f} {g['y']:.4f} {g['z']:.4f} "
                             f"{it.px[0]:.2f} {it.px[1]:.2f} {it.image} {g['name']}")
        (self.out / "gcp_list.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

        def rows(group, with_tags=True):
            out = []
            for entry in group:
                g, t = entry if with_tags else (entry, [])
                out.append({"x": g["x"], "y": g["y"], "z": g["z"], "name": g["name"],
                            "height": g["z"], "n_images": len(t),
                            "images": ";".join(it.image for it in t),
                            "pixels": ";".join(f"{it.image}:{it.px[0]:.1f},{it.px[1]:.1f}"
                                               for it in t)})
            return out

        fields = [("name", "TEXT"), ("height", "REAL"), ("n_images", "INTEGER"),
                  ("images", "TEXT"), ("pixels", "TEXT")]
        r_control, r_checks = rows(control), rows(checks)
        r_untagged = rows(untagged, with_tags=False)
        gpkg.write_points(self.out / "gcp_points.gpkg", self.srs, {
            "control": (fields, r_control),
            "checkpoints": (fields, r_checks),
            "untagged": (fields, r_untagged),
        })
        with open(self.out / "checkpoints.csv", "w", newline="", encoding="utf-8") as fh:
            wr = csv.writer(fh)
            wr.writerow(["name", "x", "y", "z", "n_images", "images", "pixels", "crs"])
            for r in r_checks:
                wr.writerow([r["name"], f"{r['x']:.4f}", f"{r['y']:.4f}", f"{r['z']:.4f}",
                             r["n_images"], r["images"], r["pixels"], self.crs_header()])

        counts = {s: sum(it.status == s for it in self.items)
                  for s in ("pending", "accepted", "manual", "auto", "absent", "outside")}
        rep = [
            "hmc-gcp tagging report",
            f"photos: {len(self.shots)}, GCPs: {len(self.gcps)}, GCP-photo pairs: {len(self.items)}",
            "pairs: " + ", ".join(f"{k} {v}" for k, v in counts.items()),
            f"estimated photo position offset (E, N): "
            f"{self.glob_off[0]:+.2f} m, {self.glob_off[1]:+.2f} m",
            "",
            f"control GCPs in gcp_list.txt (>= {min_img} photos): {len(control)}",
            *[f"  {g['name']}: {len(t)} photos" for g, t in control],
            f"checkpoints (fewer photos or chosen with --checkpoint): {len(checks)}",
            *[f"  {g['name']}: {len(t)} photos" for g, t in checks],
            f"untagged: {len(untagged)}",
            *[f"  {g['name']}" for g in untagged],
        ]
        if len(control) < 3:
            rep.append("\nWARNING: ODM needs at least 3 control GCPs; 5 or more is recommended.")
        (self.out / "tag_report.txt").write_text("\n".join(rep) + "\n", encoding="utf-8")
        return {"control": len(control), "checkpoints": len(checks), "untagged": len(untagged)}
