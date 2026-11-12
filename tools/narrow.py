#!/usr/bin/env python3
"""Script that tools/gateway.py runs on the mcp-rwee server (needs duckdb).

  narrow.py layers OKF_DIR [TABLE]       cards of every table from the bundle's Ossie model, or one table's page
  narrow.py query DATABASE QUERIES_JSON [geojson|parquet]
                                           read-only SELECTs, a JSON list of {"name", "sql"}; prints JSON, and for each
                                           GEOMETRY result writes one file, NAME.geojson (default) or NAME.parquet

The database opens read-only, then DuckDB drops file, network and settings access, so the SQL
cannot read other files, attach, copy out or change settings. Only the run's artifact folder stays
reachable, for the GeoParquet write.
"""
import json
import math
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


CARD_LEGEND = ("Each column line: name type [unit] [flags] \"alias\": values | notes. Flags: upper or lower = compare with the value "
               "in that case. like or ilike = compare with LIKE or ILIKE '%value%'. exact = never use LIKE. cast:TYPE = CAST(column AS TYPE) "
               "in a comparison. KEY=meaning values: the question uses the meaning, SQL uses KEY. Values are examples, and + means more "
               "exist. relationships: joins between tables; distances are in metres.")
# Instruction lines that layers-json renders from match, cast and example; the flags already say them.
RENDERED = ("Make sure to", "ALWAYS use SQL CAST", "NEVER use SQL LIKE", "For example:", "Use '")


def extension(item, vendor):
    """The data of one custom extension (ARCGIS or OSSIE_GEO) of an Ossie item, as a dict."""
    return next((json.loads(e["data"]) for e in item.get("custom_extensions") or [] if e["vendor_name"] == vendor), {})


def column_card(f):
    """One line for an Ossie field."""
    ai, geo = f.get("ai_context") or {}, extension(f, "OSSIE_GEO")
    flags = [m for m in [ai.get("match")] if m] + ([f"cast:{ai['cast']}"] if ai.get("cast") else [])
    description = f.get("description")
    lines = (ai.get("instructions") or "").splitlines()
    notes = [description] if description else []
    notes += [n for n in lines if n and n != description and not n.startswith(RENDERED)]
    values, codes = ai.get("sample_values") or [], geo.get("codes") or []
    numeric = f["datatype"] in ("Integer", "Float", "Decimal", "Date", "DateTime")
    limit = 2 if numeric else 3 if {"like", "ilike"} & set(flags) else 16
    shown = [f"{c['value']}={c['label']}" for c in codes] or [str(v) for v in values[:limit]]
    dtype = extension(f, "ARCGIS").get("dtype") or f["datatype"]
    text = " ".join(filter(None, [f["name"], dtype, geo.get("unit"), *flags]))
    alias = next(iter(ai.get("synonyms") or []), None)
    if alias and alias != f["name"].replace("_", " "):
        text += f' "{alias}"'
    if shown:
        text += ": " + ", ".join(shown) + (" +" if not codes and len(values) > limit else "")
    return text + (" | " + " ".join(notes) if notes else "")


def card(d):
    """The card of an Ossie dataset. Fields that layers-json left out of its view, and the geometry, are not columns."""
    geometry = next((f for f in d["fields"] if f["datatype"] == "Geometry"), None)
    spatial = (geometry or {}).get("dimension", {}).get("spatial", {})
    about = " ".join(dict.fromkeys(filter(None, [d.get("description"), (d.get("ai_context") or {}).get("instructions")])))
    out = {"table_name": d["name"], "about": about, "geometry": f"{spatial.get('type')} {spatial.get('srid')}" if spatial else None,
           "display": extension(d, "ARCGIS").get("display_field"), "key": d.get("primary_key"), "rows": extension(d, "OSSIE_GEO").get("row_count")}
    if d.get("valid_time"):
        out["valid_time"] = d["valid_time"]
    out["columns"] = [column_card(f) for f in d["fields"] if f is not geometry and extension(f, "ARCGIS").get("view") is not False]
    return out


def relationship(r):
    """One line for an Ossie relationship: equal key columns, or a spatial and/or temporal predicate."""
    a, b = r["from"], r["to"]
    if "from_columns" in r:
        joins = [f"{a}({', '.join(r['from_columns'])}) = {b}({', '.join(r.get('to_columns', []))})"]
    else:
        sp, tm = r.get("spatial"), r.get("temporal")
        joins = []
        if sp:
            distance = f" {sp['distance']} m" if "distance" in sp else ""
            joins.append(f"{a}.{sp['from_field']} {sp.get('predicate', 'any spatial predicate')}{distance} {b}.{sp['to_field']}")
        if tm:
            duration = f" {tm['duration']} s" if "duration" in tm else ""
            joins.append(f"{a} valid time {tm.get('predicate', 'any temporal predicate')}{duration} {b} valid time")
    note = (r.get("ai_context") or {}).get("instructions")
    return f"{r['name']}: {' and '.join(joins)}" + (f" | {note}" if note else "")


def layers(okf, table=None):
    """The OKF bundle in folder okf: with no table, cards from its Ossie model; with a table, its Markdown page."""
    models = sorted(Path(okf).glob("*.ossie.json"))
    if len(models) != 1:
        raise SystemExit(f"the OKF folder needs exactly one *.ossie.json, found {len(models)}")
    model = json.loads(models[0].read_text(encoding="utf-8"))
    if table is None:
        out = {"legend": CARD_LEGEND, "tables": [card(d) for d in model["datasets"]]}
        if model.get("relationships"):
            out["relationships"] = [relationship(r) for r in model["relationships"]]
        return out
    # The page path uses the model's own name, never the caller's text.
    name = next((d["name"] for d in model["datasets"] if d["name"].lower() == table.lower()), None)
    if name is None:
        raise SystemExit(f"unknown table {table}; call without a table to list them")
    return (Path(okf) / "datasets" / f"{name}.md").read_text(encoding="utf-8")


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


def finite(value):
    """JSON has no NaN or Infinity: json.dumps would write them bare, and the page could not parse the result."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (list, tuple)):
        return [finite(v) for v in value]
    if isinstance(value, dict):  # STRUCT and MAP
        return {k: finite(v) for k, v in value.items()}
    return value


def quote(name):
    """A column name as a SQL identifier; a double quote inside it is doubled."""
    return '"' + name.replace('"', '""') + '"'


def run_one(con, art, name, sql, print_rows, fmt):
    sql = sql.strip().rstrip(";")
    con.execute("DROP TABLE IF EXISTS temp.main.result")  # the last query's temp table would hide a database table named result
    columns = con.execute(f"DESCRIBE SELECT * FROM (\n{sql}\n)").fetchall()  # fails unless SELECT
    geo = [c[0] for c in columns if str(c[1]).startswith("GEOMETRY")]
    # The query runs once, into a temporary table; the rows and the file come from it. One extra row tells if it was cut.
    con.execute(f"CREATE OR REPLACE TEMP TABLE result AS SELECT * FROM (\n{sql}\n) LIMIT {MAX_ROWS + 1}")
    if geo and fmt == "parquet":  # GeoParquet V1 for the ArcGIS ParquetLayer: one column named geometry, DOUBLE not DECIMAL
        pick = [f"{quote(c[0])} AS geometry" if c[0] == geo[0] else None if c[0] in geo else
                f"CAST({quote(c[0])} AS DOUBLE) AS {quote(c[0])}" if str(c[1]).startswith("DECIMAL") else quote(c[0]) for c in columns]
        con.execute(f"COPY (SELECT {', '.join(p for p in pick if p)} FROM result LIMIT {MAX_ROWS}) TO '{art}/{name}.parquet' "
                    "(FORMAT PARQUET, COMPRESSION SNAPPY, GEOPARQUET_VERSION 'V1')")
    # GeoJSON needs each geometry as text; GeoParquet is already written, so the rows only need the type.
    convert = "ST_AsGeoJSON({})" if fmt == "geojson" else "ST_GeometryType({})::VARCHAR"
    replace = ", ".join(convert.format(quote(g)) + f" AS {quote(g)}" for g in geo)
    cur = con.execute(f"SELECT * {f'REPLACE ({replace})' if geo else ''} FROM result")
    names = [d[0] for d in cur.description]
    rows = [tuple(finite(v) for v in r) for r in cur.fetchall()]
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
    print(result if isinstance(result, str) else json.dumps(result, default=str, separators=(",", ":")))  # compact: the gateway cannot reshape it, because files follow
