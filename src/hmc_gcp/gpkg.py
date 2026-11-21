"""Minimal GeoPackage point reader and writer (no GDAL needed)."""

from __future__ import annotations

import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SRS:
    srs_id: int
    name: str
    organization: str
    org_id: int
    definition: str

    @property
    def is_epsg(self) -> bool:
        return self.organization.upper() == "EPSG" and self.org_id > 0


@dataclass
class GCP:
    name: str
    x: float
    y: float
    z: float | None


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _parse_wkb_point(wkb: bytes):
    bo = "<" if wkb[0] == 1 else ">"
    gtype = struct.unpack(bo + "I", wkb[1:5])[0]
    has_z = bool(gtype & 0x80000000)
    has_m = bool(gtype & 0x40000000)
    gtype &= 0x0FFFFFFF
    if gtype >= 3000:
        has_z = has_m = True
        gtype -= 3000
    elif gtype >= 2000:
        has_m = True
        gtype -= 2000
    elif gtype >= 1000:
        has_z = True
        gtype -= 1000
    if gtype == 4:  # multipoint: use the first point
        n = struct.unpack(bo + "I", wkb[5:9])[0]
        return _parse_wkb_point(wkb[9:]) if n else None
    if gtype != 1:
        raise ValueError(f"geometry type {gtype} is not a point")
    n = 2 + int(has_z) + int(has_m)
    vals = struct.unpack(bo + "d" * n, wkb[5 : 5 + 8 * n])
    if vals[0] != vals[0]:  # NaN = empty point
        return None
    return vals[0], vals[1], (vals[2] if has_z else None)


def _parse_gpkg_geom(blob: bytes):
    if blob is None:
        return None
    if blob[:2] != b"GP":
        raise ValueError("not a GeoPackage geometry blob")
    flags = blob[3]
    env = (flags >> 1) & 0b111
    env_len = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get(env)
    if env_len is None:
        raise ValueError("bad envelope flag in geometry")
    if (flags >> 4) & 1:
        return None
    return _parse_wkb_point(blob[8 + env_len :])


def list_layers(path: Path) -> list[str]:
    con = sqlite3.connect(str(path))
    try:
        rows = con.execute(
            "SELECT table_name FROM gpkg_contents WHERE data_type='features'"
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def read_points(path: Path, layer: str, name_field: str | None = None,
                z_field: str | None = None) -> tuple[list[GCP], SRS]:
    if not Path(path).exists():
        raise FileNotFoundError(path)
    con = sqlite3.connect(str(path))
    try:
        layers = [r[0] for r in con.execute(
            "SELECT table_name FROM gpkg_contents WHERE data_type='features'")]
        if layer not in layers:
            raise ValueError(f"layer '{layer}' not found. Layers: {', '.join(layers)}")
        row = con.execute(
            "SELECT column_name, srs_id FROM gpkg_geometry_columns WHERE table_name=?",
            (layer,)).fetchone()
        geom_col, srs_id = row
        srow = con.execute(
            "SELECT srs_name, organization, organization_coordsys_id, definition "
            "FROM gpkg_spatial_ref_sys WHERE srs_id=?", (srs_id,)).fetchone()
        srs = SRS(srs_id, srow[0], srow[1], int(srow[2]), srow[3])
        cols = con.execute(f"PRAGMA table_info({_q(layer)})").fetchall()
        names = [c[1] for c in cols]
        pk = next((c[1] for c in cols if c[5]), "rowid")
        for f in (name_field, z_field):
            if f and f not in names:
                raise ValueError(f"field '{f}' not in layer. Fields: {', '.join(names)}")
        sel = [_q(pk), _q(geom_col)]
        sel.append(_q(name_field) if name_field else "NULL")
        sel.append(_q(z_field) if z_field else "NULL")
        rows = con.execute(f"SELECT {', '.join(sel)} FROM {_q(layer)}").fetchall()
    finally:
        con.close()

    gcps: list[GCP] = []
    seen: set[str] = set()
    for fid, blob, name, zval in rows:
        pt = _parse_gpkg_geom(blob)
        if pt is None:
            continue
        x, y, z = pt
        if zval is not None:
            z = float(zval)
        nm = str(name if name is not None else fid).strip()
        nm = "_".join(nm.split()) or str(fid)  # ODM needs names without spaces
        if nm in seen:
            nm = f"{nm}_{fid}"
        seen.add(nm)
        gcps.append(GCP(nm, float(x), float(y), z))
    return gcps, srs


# ---------------------------------------------------------------- writer

_WGS84_WKT = (
    'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
    'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433],AUTHORITY["EPSG","4326"]]'
)


def _point_blob(srs_id: int, x: float, y: float, z: float | None) -> bytes:
    hdr = b"GP" + bytes([0, 0x01]) + struct.pack("<i", srs_id)
    if z is None:
        wkb = b"\x01" + struct.pack("<I", 1) + struct.pack("<dd", x, y)
    else:
        wkb = b"\x01" + struct.pack("<I", 1001) + struct.pack("<ddd", x, y, z)
    return hdr + wkb


def write_points(path: Path, srs: SRS, layers: dict[str, tuple[list[tuple[str, str]], list[dict]]]):
    """Write point layers. layers: name -> (fields [(name, SQL type)], rows).
    Each row needs x, y, z (z may be None) plus the field values."""
    path = Path(path)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(str(path))
    try:
        con.execute("PRAGMA application_id = 1196444487")
        con.execute("PRAGMA user_version = 10300")
        con.execute(
            "CREATE TABLE gpkg_spatial_ref_sys (srs_name TEXT NOT NULL, "
            "srs_id INTEGER PRIMARY KEY, organization TEXT NOT NULL, "
            "organization_coordsys_id INTEGER NOT NULL, definition TEXT NOT NULL, "
            "description TEXT)")
        con.executemany(
            "INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)",
            [("Undefined cartesian SRS", -1, "NONE", -1, "undefined", None),
             ("Undefined geographic SRS", 0, "NONE", 0, "undefined", None),
             ("WGS 84 geodetic", 4326, "EPSG", 4326, _WGS84_WKT, None)])
        if srs.srs_id not in (-1, 0, 4326):
            con.execute("INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)",
                        (srs.name, srs.srs_id, srs.organization, srs.org_id,
                         srs.definition, None))
        con.execute(
            "CREATE TABLE gpkg_contents (table_name TEXT NOT NULL PRIMARY KEY, "
            "data_type TEXT NOT NULL, identifier TEXT UNIQUE, description TEXT DEFAULT '', "
            "last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), "
            "min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, srs_id INTEGER, "
            "CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id) "
            "REFERENCES gpkg_spatial_ref_sys(srs_id))")
        con.execute(
            "CREATE TABLE gpkg_geometry_columns (table_name TEXT NOT NULL, "
            "column_name TEXT NOT NULL, geometry_type_name TEXT NOT NULL, "
            "srs_id INTEGER NOT NULL, z TINYINT NOT NULL, m TINYINT NOT NULL, "
            "CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name))")
        for lname, (fields, rows) in layers.items():
            fdef = ", ".join(f"{_q(n)} {t}" for n, t in fields)
            con.execute(f"CREATE TABLE {_q(lname)} (fid INTEGER PRIMARY KEY AUTOINCREMENT, "
                        f"geom POINT{', ' + fdef if fdef else ''})")
            has_z = any(r.get("z") is not None for r in rows)
            xs = [r["x"] for r in rows]
            ys = [r["y"] for r in rows]
            con.execute(
                "INSERT INTO gpkg_contents (table_name, data_type, identifier, "
                "min_x, min_y, max_x, max_y, srs_id) VALUES (?,?,?,?,?,?,?,?)",
                (lname, "features", lname, min(xs) if xs else None, min(ys) if ys else None,
                 max(xs) if xs else None, max(ys) if ys else None, srs.srs_id))
            con.execute("INSERT INTO gpkg_geometry_columns VALUES (?,?,?,?,?,?)",
                        (lname, "geom", "POINT", srs.srs_id, 1 if has_z else 0, 0))
            names = [n for n, _ in fields]
            ph = ", ".join(["?"] * (len(names) + 1))
            cols = ", ".join(["geom"] + [_q(n) for n in names])
            for r in rows:
                z = r.get("z") if has_z else None
                if has_z and z is None:
                    z = 0.0
                con.execute(f"INSERT INTO {_q(lname)} ({cols}) VALUES ({ph})",
                            [_point_blob(srs.srs_id, r["x"], r["y"], z)]
                            + [r.get(n) for n in names])
        con.commit()
    finally:
        con.close()
