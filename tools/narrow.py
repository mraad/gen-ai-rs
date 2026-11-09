#!/usr/bin/env python3
"""Script that tools/gateway.py runs on the mcp-rwee server (needs duckdb).

  narrow.py layers LAYERS_JSON [TABLE]   table list, or the metadata of one table
  narrow.py query DATABASE QUERIES_JSON [geojson|parquet]
                                           read-only SELECTs, a JSON list of {"name", "sql"}; prints JSON, and for each
                                           GEOMETRY result writes one file, NAME.geojson (default) or NAME.parquet

The database opens read-only, then DuckDB drops file, network and settings access, so the SQL
cannot read other files, attach, copy out or change settings. Only the run's artifact folder stays
reachable, for the GeoParquet write.
"""
import json
import os
import re
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

PRINT_ROWS = 200
FORMATS = ("geojson", "parquet")
GEO_SAMPLE = 10
MAX_QUERIES = 6
MAX_ROWS = 100_000


CARD_LEGEND = ("Each column line: name type [unit] [flags] \"alias\": values | notes. Flags: upper = compare with the value in uppercase. "
               "like = compare with LIKE '%value%'. cast = CAST(column AS DOUBLE PRECISION) in a comparison. exact = never use LIKE. "
               "KEY=meaning values: the question uses the meaning, SQL uses KEY. Values are examples, and + means more exist.")
RULE_HINTS = (("Make sure to uppercase", "upper"), ("Make sure to use the SQL LIKE", "like"),
              ("ALWAYS use SQL CAST AS DOUBLE", "cast"), ("NEVER use SQL LIKE", "exact"))


def column_card(c):
    """One line for a column. A hint that a flag or a key already says is dropped; any other hint is kept word for word."""
    flags, notes = [], []
    for hint in c.get("hints") or []:
        flag = next((f for prefix, f in RULE_HINTS if hint.startswith(prefix)), None)
        if flag:
            flags.append(flag)
        elif not hint.startswith(("For example:", "Use '")):
            notes.append(hint)
    values = c.get("values") or []
    keyval = c.get("keyval") or {}
    numeric = c.get("dtype") in ("Double", "Integer", "SmallInteger", "Date")
    limit = 2 if numeric else 16 if "like" not in flags else 3
    shown = [f"{k}={v}" for k, v in keyval.items()] or [str(v) for v in values[:limit]]
    text = " ".join(filter(None, [c["name"], c.get("dtype"), c.get("utype"), *flags]))
    alias = c.get("alias")
    if alias and alias != c["name"].replace("_", " "):
        text += f' "{alias}"'
    if shown:
        text += ": " + ", ".join(shown) + (" +" if not keyval and len(values) > limit else "")
    return text + (" | " + " ".join(notes) if notes else "")


def card(x):
    return {"table_name": x["table_name"], "stype": x.get("stype"), "display": x.get("display"),
            "about": " ".join(x.get("hints") or []), "columns": [column_card(c) for c in x["columns"]]}


def layers(path, table=None):
    found = json.loads(Path(path).read_text(encoding="utf-8"))["layers"]
    if table is None:
        return {"legend": CARD_LEGEND, "tables": [card(x) for x in found]}
    for x in found:
        if x["table_name"].lower() == table.lower():
            return x
    raise SystemExit(f"unknown table {table}; call without a table to list them")


NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
KINDS = {"POINT": "point", "MULTIPOINT": "point", "LINESTRING": "polyline", "MULTILINESTRING": "polyline",
         "POLYGON": "polygon", "MULTIPOLYGON": "polygon"}  # GeoJSON type or ST_GeometryType, in uppercase


def column_stats(values):
    """Null count of a column; min and max too when its values can be ordered and mean something."""
    present = [v for v in values if v is not None]
    stats = {"nulls": len(values) - len(present)}
    if present and isinstance(present[0], (int, float, Decimal, date)) and not isinstance(present[0], bool):
        stats.update(min=min(present), max=max(present))
    return stats


def run_one(con, art, name, sql, print_rows, fmt):
    sql = sql.strip().rstrip(";")
    columns = con.execute(f"DESCRIBE SELECT * FROM (\n{sql}\n)").fetchall()  # fails unless SELECT
    geo = [c[0] for c in columns if str(c[1]).startswith("GEOMETRY")]
    # The query runs once, into a temporary table; the rows and the file come from it. One extra row tells if it was cut.
    con.execute(f"CREATE OR REPLACE TEMP TABLE result AS SELECT * FROM (\n{sql}\n) LIMIT {MAX_ROWS + 1}")
    if geo and fmt == "parquet":  # GeoParquet V1 for the ArcGIS ParquetLayer: one column named geometry, DOUBLE not DECIMAL
        pick = [f'"{c[0]}" AS geometry' if c[0] == geo[0] else None if c[0] in geo else
                f'CAST("{c[0]}" AS DOUBLE) AS "{c[0]}"' if str(c[1]).startswith("DECIMAL") else f'"{c[0]}"' for c in columns]
        con.execute(f"COPY (SELECT {', '.join(p for p in pick if p)} FROM result LIMIT {MAX_ROWS}) TO '{art}/{name}.parquet' "
                    "(FORMAT PARQUET, COMPRESSION SNAPPY, GEOPARQUET_VERSION 'V1')")
    # GeoJSON needs each geometry as text; GeoParquet is already written, so the rows only need the type.
    convert = "ST_AsGeoJSON({})" if fmt == "geojson" else "ST_GeometryType({})::VARCHAR"
    replace = ", ".join(convert.format(f'"{g}"') + f' AS "{g}"' for g in geo)
    cur = con.execute(f"SELECT * {f'REPLACE ({replace})' if geo else ''} FROM result")
    names = [d[0] for d in cur.description]
    rows = cur.fetchall()
    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    plain = [n for n in names if n not in geo]
    keep = [i for i, n in enumerate(names) if n in plain]
    # The map draws a geometry result from its files, so the model needs only its shape and a sample.
    shown = GEO_SAMPLE if geo else print_rows
    out = {"name": name, "columns": plain, "rows": [[r[i] for i in keep] for r in rows[:shown]], "row_count": len(rows),
           "truncated": truncated, "geometry": geo[0] if geo else None}
    if geo:
        out["stats"] = {n: column_stats([r[i] for r in rows]) for n, i in zip(plain, keep)}
        gi = names.index(geo[0])
        first = next((r[gi] for r in rows if r[gi]), None)
        if fmt == "geojson":
            features = [{"type": "Feature", "geometry": json.loads(r[gi]) if r[gi] else None,
                         "properties": {n: r[i] for n, i in zip(plain, keep)}} for r in rows]
            for f in features:
                if isinstance(f["properties"].get("OBJECTID"), int):
                    f["id"] = f["properties"]["OBJECTID"]
            (Path(art) / f"{name}.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": features}, default=str), encoding="utf-8")
            first = first and json.loads(first)["type"]
        out["format"], out["file"], out["geometry_type"] = fmt, f"{name}.{fmt}", KINDS.get((first or "").upper())
    return out


def query(database, queries, fmt="geojson"):
    import duckdb

    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {FORMATS}")
    queries = json.loads(queries) if isinstance(queries, str) else queries
    names = [q["name"] for q in queries]
    if not queries or len(queries) > MAX_QUERIES or len(set(names)) != len(names) or not all(NAME.match(n) for n in names):
        raise ValueError(f"give 1 to {MAX_QUERIES} queries with unique lowercase names such as layer_1")
    with duckdb.connect(database, read_only=True) as con:
        con.execute("LOAD spatial")
        con.execute("SET geometry_always_xy = true")
        art = os.path.realpath(os.environ["RWEE_ARTIFACT_DIR"])
        con.execute(f"SET allowed_directories = ['{art}']")
        con.execute("SET enable_external_access = false")
        con.execute("SET lock_configuration = true")
        shown = PRINT_ROWS if len(queries) == 1 else PRINT_ROWS // 4
        return {"layers": [run_one(con, art, q["name"], q["sql"], shown, fmt) for q in queries]}


if __name__ == "__main__":
    try:
        command, *args = sys.argv[1:]
        result = {"layers": layers, "query": query}[command](*args)
    except (ValueError, KeyError, TypeError) as error:
        sys.exit(f"usage error: {error!r}")
    except Exception as error:  # noqa: BLE001 - DuckDB errors go back to the model as text
        sys.exit(f"{type(error).__name__}: {error}")
    print(json.dumps(result, default=str, separators=(",", ":")))  # compact: the gateway cannot reshape it, because files follow
