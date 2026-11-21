"""Optional flight plan (Litchi-style CSV) for steadier heading and gimbal angle."""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Waypoint:
    lat: float
    lon: float
    alt: float | None
    heading: float | None
    pitch: float | None


def _num(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def read_plan(path: Path) -> list[Waypoint]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rd = csv.DictReader(fh)
        cols = {c.lower().strip(): c for c in (rd.fieldnames or [])}

        def find(*prefixes):
            for p in prefixes:
                for lc, c in cols.items():
                    if lc.startswith(p):
                        return c
            return None

        c_lat = find("latitude", "lat")
        c_lon = find("longitude", "lon", "lng")
        c_alt = find("altitude(m)", "altitude", "alt")
        c_head = find("heading", "yaw")
        c_pitch = find("gimbalpitchangle", "gimbal_pitch", "gimbalpitch", "pitch")
        if not c_lat or not c_lon:
            raise ValueError(f"{path}: no latitude/longitude columns found")
        wps = []
        for row in rd:
            lat, lon = _num(row.get(c_lat)), _num(row.get(c_lon))
            if lat is None or lon is None:
                continue
            wps.append(Waypoint(
                lat, lon,
                _num(row.get(c_alt)) if c_alt else None,
                _num(row.get(c_head)) if c_head else None,
                _num(row.get(c_pitch)) if c_pitch else None,
            ))
    return wps


def nearest(wps: list[Waypoint], lat: float, lon: float, max_dist: float = 15.0):
    best, best_d = None, max_dist
    k = math.cos(math.radians(lat))
    for w in wps:
        d = 111_320.0 * math.hypot(w.lat - lat, (w.lon - lon) * k)
        if d < best_d:
            best, best_d = w, d
    return best
