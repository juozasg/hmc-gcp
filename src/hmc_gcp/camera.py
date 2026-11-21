"""Photo metadata (EXIF + DJI XMP) and the camera projection model."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

_XMP_ATTR = re.compile(r'drone-dji:(\w+)="([^"]*)"')
_XMP_ELEM = re.compile(r"<drone-dji:(\w+)>([^<]*)</drone-dji:\1>")


def _num(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _dms(v, ref) -> float:
    d, m, s = (float(t) for t in v)
    val = d + m / 60.0 + s / 3600.0
    if isinstance(ref, bytes):
        ref = ref.decode(errors="ignore")
    return -val if str(ref).strip().upper() in ("S", "W") else val


@dataclass
class Shot:
    name: str
    path: str
    width: int
    height: int
    lat: float | None
    lon: float | None
    rel_alt: float | None
    abs_alt: float | None
    yaw: float | None
    pitch: float | None
    f35: float | None
    yaw_source: str = "exif"
    # filled in by the session
    E: float = 0.0
    N: float = 0.0
    Z: float = 0.0
    fpx: float = 0.0
    axes: np.ndarray | None = field(default=None, repr=False)

    @property
    def cx(self) -> float:
        return (self.width - 1) / 2.0

    @property
    def cy(self) -> float:
        return (self.height - 1) / 2.0


def read_shot(path: Path) -> Shot:
    with Image.open(path) as im:
        w, h = im.size
        exif = im.getexif()
        gps = exif.get_ifd(0x8825)
        sub = exif.get_ifd(0x8769)
    with open(path, "rb") as fh:
        head = fh.read(1 << 19).decode("latin-1")
    xmp: dict[str, str] = {}
    for m in _XMP_ATTR.finditer(head):
        xmp.setdefault(m.group(1), m.group(2))
    for m in _XMP_ELEM.finditer(head):
        xmp.setdefault(m.group(1), m.group(2))

    lat = _num(xmp.get("GpsLatitude"))
    lon = _num(xmp.get("GpsLongitude") or xmp.get("GpsLongtitude"))
    try:
        if lat is None and 2 in gps:
            lat = _dms(gps[2], gps.get(1, "N"))
        if lon is None and 4 in gps:
            lon = _dms(gps[4], gps.get(3, "E"))
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    abs_alt = _num(xmp.get("AbsoluteAltitude"))
    if abs_alt is None and 6 in gps:
        try:
            abs_alt = float(gps[6])
            if gps.get(5) in (1, b"\x01"):
                abs_alt = -abs_alt
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    yaw = _num(xmp.get("GimbalYawDegree"))
    if yaw is None:
        yaw = _num(xmp.get("FlightYawDegree"))
    f35 = sub.get(41989)
    return Shot(
        name=Path(path).name, path=str(path), width=w, height=h, lat=lat, lon=lon,
        rel_alt=_num(xmp.get("RelativeAltitude")), abs_alt=abs_alt, yaw=yaw,
        pitch=_num(xmp.get("GimbalPitchDegree")),
        f35=float(f35) if f35 else None,
    )


def axes_from_angles(yaw_deg: float, pitch_deg: float) -> np.ndarray:
    """Camera axes in local east-north-up: rows are right, down, forward.
    yaw: degrees clockwise from north (top of image points there when looking down).
    pitch: gimbal pitch, -90 = straight down. Gimbal roll is assumed 0."""
    psi = math.radians(yaw_deg)
    th = math.radians(pitch_deg)
    f = np.array([math.sin(psi) * math.cos(th), math.cos(psi) * math.cos(th), math.sin(th)])
    r = np.array([math.cos(psi), -math.sin(psi), 0.0])
    d = np.cross(f, r)
    return np.vstack([r, d, f])


def focal_px(f35: float, width: int, height: int) -> float:
    """Focal length in pixels from the 35 mm equivalent focal length."""
    return f35 / 43.2666 * math.hypot(width, height)
