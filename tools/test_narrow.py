#!/usr/bin/env python3
"""narrow.py must stay read-only and locked, and must write one GeoJSON or one GeoParquet file per layer. Needs duckdb:
uv run --with duckdb python3 tools/test_narrow.py"""
import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

try:
    import duckdb
except ImportError:  # the gateway tests do not need it
    duckdb = None

import narrow


def one(db, sql):
    return narrow.query(db, [{"name": "result", "sql": sql}])["layers"][0]


@unittest.skipIf(duckdb is None, "duckdb is not installed")
class Narrow(unittest.TestCase):
    def bundle(self, *extra_models):
        """A small OKF bundle: an Ossie model with two datasets and one relationship, and one dataset page."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        okf = Path(tmp.name)
        geo = lambda data: [{"vendor_name": "OSSIE_GEO", "data": json.dumps(data)}]
        arcgis = lambda data: [{"vendor_name": "ARCGIS", "data": json.dumps(data)}]
        wells = {"name": "Wells", "description": "Oil and gas wells.", "primary_key": ["OBJECTID"],
                 "valid_time": {"start": "entry_date", "end": "completion_date"},
                 "custom_extensions": arcgis({"display_field": "well_name"}) + geo({"row_count": 2}),
                 "fields": [
                     {"name": "OBJECTID", "datatype": "Integer", "custom_extensions": arcgis({"view": False})},
                     {"name": "well_name", "description": "The name of the well", "datatype": "String",
                      "ai_context": {"synonyms": ["well name"], "match": "ilike", "sample_values": ["a", "b", "c", "d"],
                                     "instructions": "The name of the well\nMake sure to use the SQL ILIKE in the WHERE clause.\nFor example: x"}},
                     {"name": "phase", "datatype": "String", "ai_context": {"synonyms": ["current phase"], "match": "upper", "sample_values": ["A", "B"],
                                                                            "instructions": "Make sure to uppercase the values\nAsk which phase if unsure"}},
                     {"name": "country", "datatype": "String", "ai_context": {"match": "exact", "instructions": "Use 'country = 'NO'' for 'norway'."},
                      "custom_extensions": geo({"codes": [{"value": "NO", "label": "Norway"}]})},
                     {"name": "kind", "datatype": "Integer", "ai_context": {"match": "exact", "cast": "SMALLINT", "sample_values": ["0", "1", "2"]},
                      "custom_extensions": arcgis({"dtype": "SmallInteger"})},
                     {"name": "depth", "datatype": "Float", "ai_context": {"match": "exact", "cast": "DOUBLE PRECISION", "sample_values": ["1", "2", "3"]},
                      "custom_extensions": geo({"unit": "meters"})},
                     {"name": "geometry", "datatype": "Geometry", "dimension": {"spatial": {"type": "point", "multi": False, "srid": 4326}}}]}
        pipes = {"name": "Pipes", "fields": [{"name": "geometry", "datatype": "Geometry", "dimension": {"spatial": {"type": "line", "srid": 4326}}}]}
        model = {"version": "0.2", "name": "Demo", "datasets": [wells, pipes], "relationships": [
            {"name": "near", "from": "Wells", "to": "Pipes", "spatial": {"from_field": "geometry", "to_field": "geometry", "predicate": "within_distance", "distance": 500},
             "ai_context": {"instructions": "Use EPSG:3035."}},
            {"name": "same_time", "from": "Wells", "to": "Wells", "temporal": {"predicate": "during"}},
            {"name": "by_key", "from": "Pipes", "to": "Wells", "from_columns": ["well_id"], "to_columns": ["OBJECTID"]}]}
        for name in ("Demo", *extra_models):
            (okf / f"{name}.ossie.json").write_text(json.dumps(model))
        (okf / "datasets").mkdir()
        (okf / "datasets" / "Wells.md").write_text("---\ntype: \"Ossie Dataset\"\n---\n\n# Schema\n| `well_name` |\n")
        (okf / "Wells.md").write_text("outside datasets/")
        return okf

    def test_cards_come_from_the_ossie_model(self):
        out = narrow.layers(self.bundle())
        assert isinstance(out, dict)  # a table name would give the page text instead
        wells, pipes = out["tables"]
        self.assertEqual({k: v for k, v in wells.items() if k != "columns"}, {"table_name": "Wells", "about": "Oil and gas wells.", "geometry": "point 4326",
                         "display": "well_name", "key": ["OBJECTID"], "rows": 2, "valid_time": {"start": "entry_date", "end": "completion_date"}})
        self.assertEqual(wells["columns"], [  # OBJECTID is out of the view, and the geometry is in the table line
            "well_name String ilike: a, b, c + | The name of the well",
            'phase String upper "current phase": A, B | Ask which phase if unsure',
            "country String exact: NO=Norway",
            "kind SmallInteger exact cast:SMALLINT: 0, 1 +",
            "depth Float meters exact cast:DOUBLE PRECISION: 1, 2 +"])
        self.assertEqual((pipes["geometry"], pipes["columns"], "valid_time" in pipes), ("line 4326", [], False))
        self.assertEqual(out["relationships"], ["near: Wells.geometry within_distance 500 m Pipes.geometry | Use EPSG:3035.",
                                                "same_time: Wells valid time during Wells valid time",
                                                "by_key: Pipes(well_id) = Wells(OBJECTID)"])
        self.assertIn("cast:TYPE", out["legend"])

    def test_a_table_gives_its_page_by_the_model_name(self):
        okf = self.bundle()
        self.assertEqual(narrow.layers(okf, "wells"), (okf / "datasets" / "Wells.md").read_text())
        for name in ("../Wells", "datasets/Wells", "Nope"):
            with self.assertRaises(SystemExit):
                narrow.layers(okf, name)

    def test_the_folder_needs_exactly_one_model(self):
        with self.assertRaises(SystemExit):
            narrow.layers(self.bundle("Second"))
        with tempfile.TemporaryDirectory() as empty, self.assertRaises(SystemExit):
            narrow.layers(empty)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.art = Path(tmp.name, "art")
        self.art.mkdir()
        self.db = str(Path(tmp.name, "t.ddb"))
        with duckdb.connect(self.db) as con:
            con.execute("LOAD spatial")
            con.execute("CREATE TABLE Wells(OBJECTID INTEGER, name VARCHAR, depth DECIMAL(9,2), geometry GEOMETRY)")
            con.execute("INSERT INTO Wells VALUES (1, 'a', 10.50, ST_Point(2, 58)), (2, 'b', NULL, ST_Point(3, 59))")
        os.environ["RWEE_ARTIFACT_DIR"] = str(self.art)
        self.addCleanup(os.environ.pop, "RWEE_ARTIFACT_DIR")

    def test_nan_and_infinity_become_null_so_the_output_is_valid_json(self):
        sql = "SELECT * FROM (VALUES ('nan'::DOUBLE, ST_Point(1, 2)), ('inf'::DOUBLE, ST_Point(3, 4)), (1.5, ST_Point(5, 6))) t(v, geometry)"
        strict = {"parse_constant": lambda c: self.fail(f"invalid JSON constant {c}")}
        for fmt in narrow.FORMATS:
            out = narrow.query(self.db, [{"name": "result", "sql": sql}], fmt)["layers"][0]
            self.assertEqual(out["rows"], [[None], [None], [1.5]])
            self.assertEqual(out["stats"]["v"], {"nulls": 2, "min": 1.5, "max": 1.5})
            json.loads(json.dumps(out, default=str), **strict)
        features = json.loads((self.art / "result.geojson").read_text(), **strict)["features"]
        self.assertEqual([f["properties"]["v"] for f in features], [None, None, 1.5])
        nested = one(self.db, "SELECT ['nan'::DOUBLE, 2.0] AS l, {'x': 'inf'::DOUBLE} AS s, MAP {'k': '-inf'::DOUBLE} AS m, ST_Point(1, 2) AS geometry")
        self.assertEqual(nested["rows"], [[[None, 2.0], {"x": None}, {"k": None}]])
        json.loads(json.dumps(nested, default=str), **strict)
        json.loads((self.art / "result.geojson").read_text(), **strict)

    def test_column_names_with_a_double_quote_work_in_both_formats(self):
        sql = 'SELECT name AS "say ""hi""", geometry AS "g""h" FROM Wells ORDER BY OBJECTID'
        for fmt in narrow.FORMATS:
            out = narrow.query(self.db, [{"name": "result", "sql": sql}], fmt)["layers"][0]
            self.assertEqual((out["columns"], out["geometry"], out["row_count"]), (['say "hi"'], 'g"h', 2))
        self.assertTrue((self.art / "result.parquet").is_file())

    def test_geometry_query_writes_only_the_chosen_file(self):
        out = one(self.db, "SELECT OBJECTID, name, depth, geometry AS g FROM Wells ORDER BY OBJECTID;")
        self.assertEqual(out["columns"], ["OBJECTID", "name", "depth"])
        self.assertEqual(out["rows"], [[1, "a", Decimal("10.50")], [2, "b", None]])
        self.assertEqual(out["stats"]["depth"], {"nulls": 1, "min": Decimal("10.50"), "max": Decimal("10.50")})
        self.assertEqual(out["stats"]["name"], {"nulls": 0})
        self.assertEqual((out["row_count"], out["geometry_type"], out["format"], out["file"]), (2, "point", "geojson", "result.geojson"))
        self.assertEqual([p.name for p in self.art.iterdir()], ["result.geojson"])
        features = json.loads((self.art / "result.geojson").read_text())["features"]
        self.assertEqual([f["id"] for f in features], [1, 2])
        self.assertEqual(features[0]["geometry"]["coordinates"], [2, 58])
        (self.art / "result.geojson").unlink()
        sql = "SELECT OBJECTID, name, depth, geometry AS g FROM Wells ORDER BY OBJECTID"
        out = narrow.query(self.db, json.dumps([{"name": "result", "sql": sql}]), "parquet")["layers"][0]
        self.assertEqual((out["format"], out["file"], out["geometry_type"], out["rows"]), ("parquet", "result.parquet", "point", [[1, "a", Decimal("10.50")], [2, "b", None]]))
        self.assertEqual([p.name for p in self.art.iterdir()], ["result.parquet"])
        with self.assertRaises(ValueError):
            narrow.query(self.db, json.dumps([{"name": "result", "sql": sql}]), "csv")
        with duckdb.connect() as con:  # the ParquetLayer needs a column named geometry and DOUBLE, not DECIMAL
            con.execute("LOAD spatial")
            types = {r[0]: r[1] for r in con.execute(f"DESCRIBE SELECT * FROM '{self.art / 'result.parquet'}'").fetchall()}
        self.assertTrue(types["geometry"].startswith("GEOMETRY"))
        self.assertEqual(types["depth"], "DOUBLE")

    def test_several_queries_write_one_file_each(self):
        with duckdb.connect(self.db) as con:
            con.execute("LOAD spatial")
            con.execute("CREATE TABLE Pipes(OBJECTID INTEGER, geometry GEOMETRY)")
            con.execute("INSERT INTO Pipes VALUES (1, ST_GeomFromText('LINESTRING(2 58, 3 59)'))")
        out = narrow.query(self.db, json.dumps([{"name": "wells", "sql": "SELECT OBJECTID, geometry FROM Wells"},
                                                  {"name": "pipes", "sql": "SELECT OBJECTID, geometry FROM Pipes"}]))["layers"]
        self.assertEqual([(x["name"], x["geometry_type"], x["file"]) for x in out],
                         [("wells", "point", "wells.geojson"), ("pipes", "polyline", "pipes.geojson")])
        self.assertEqual(sorted(p.name for p in self.art.iterdir()), ["pipes.geojson", "wells.geojson"])
        for bad in ([], [{"name": "A", "sql": "SELECT 1"}], [{"name": "a", "sql": "SELECT 1"}] * 2, [{"name": "../x", "sql": "SELECT 1"}]):
            with self.assertRaises(ValueError):
                narrow.query(self.db, bad)

    def test_metre_distance_in_a_local_projection(self):  # the recipe in rules.md
        sql = ("SELECT round(ST_Distance(ST_Transform(ST_Point(2, 58), 'EPSG:4326', '+proj=laea +lat_0=58 +lon_0=3 +datum=WGS84 +units=m +no_defs', always_xy := true),"
               " ST_Transform(ST_Point(2.1, 58), 'EPSG:4326', '+proj=laea +lat_0=58 +lon_0=3 +datum=WGS84 +units=m +no_defs', always_xy := true))) AS m")
        self.assertAlmostEqual(one(self.db, sql)["rows"][0][0], 5900, delta=30)  # 0.1 degree of longitude at 58N

    def test_geometry_result_prints_a_sample_but_writes_every_row(self):
        with duckdb.connect(self.db) as con:
            con.execute("LOAD spatial")
            con.execute("CREATE TABLE Many AS SELECT i AS OBJECTID, ST_Point(i, 1) AS geometry FROM range(1, 26) t(i)")
        out = one(self.db, "SELECT OBJECTID, geometry FROM Many ORDER BY OBJECTID")
        self.assertEqual((out["row_count"], len(out["rows"]), out["stats"]["OBJECTID"]), (25, 10, {"nulls": 0, "min": 1, "max": 25}))
        self.assertEqual(len(json.loads((self.art / "result.geojson").read_text())["features"]), 25)
        self.assertEqual(len(one(self.db, "SELECT OBJECTID FROM Many")["rows"]), 25)  # no geometry: the rows themselves

    def test_geoparquet_runs_the_query_once_and_marks_a_cut(self):
        q = lambda sql: narrow.query(self.db, json.dumps([{"name": "result", "sql": sql}]), "parquet")["layers"][0]
        out = q("SELECT random() AS r, ST_Point(1, 2) AS geometry")
        with duckdb.connect() as con:
            con.execute("LOAD spatial")
            in_file = con.execute(f"SELECT r FROM '{self.art / 'result.parquet'}'").fetchone()[0]
        self.assertEqual(out["rows"][0][0], in_file)  # a second run would give another random number
        old, narrow.MAX_ROWS = narrow.MAX_ROWS, 3
        self.addCleanup(setattr, narrow, "MAX_ROWS", old)
        out = q("SELECT i AS OBJECTID, ST_Point(i, 1) AS geometry FROM range(5) t(i)")
        with duckdb.connect() as con:
            con.execute("LOAD spatial")
            count = con.execute(f"SELECT count(*) FROM '{self.art / 'result.parquet'}'").fetchone()[0]
        self.assertEqual((out["row_count"], out["truncated"], count), (3, True, 3))
        self.assertEqual(sorted(p.name for p in self.art.iterdir()), ["result.parquet"])  # no .tmp left

    def test_a_database_table_named_result_is_not_hidden(self):
        with duckdb.connect(self.db) as con:
            con.execute("CREATE TABLE result AS SELECT 7 AS n")
        out = narrow.query(self.db, json.dumps([{"name": "a", "sql": "SELECT 1 AS x"}, {"name": "b", "sql": "SELECT n FROM result"}]))
        self.assertEqual(out["layers"][1]["rows"], [[7]])

    def test_a_database_schema_named_temp_does_not_break_queries(self):
        with duckdb.connect(self.db) as con:
            con.execute("CREATE SCHEMA temp")
        self.assertEqual(one(self.db, "SELECT count(*) AS n FROM Wells")["rows"], [[2]])

    def test_plain_query_writes_no_files(self):
        out = one(self.db, "SELECT count(*) AS n FROM Wells")
        self.assertEqual((out["columns"], out["rows"]), (["n"], [[2]]))
        self.assertEqual(list(self.art.iterdir()), [])

    def test_the_sql_cannot_leave_the_sandbox(self):
        for sql in ("SELECT * FROM read_csv('/etc/hosts')", "SELECT 1; DROP TABLE Wells", "DROP TABLE Wells",
                    f"COPY (SELECT 1) TO '{self.art.parent}/x.csv'", "SET enable_external_access = true",
                    "CREATE TABLE t AS SELECT 1", "SELECT * FROM Wells WHERE 1=1) UNION ALL (SELECT * FROM read_csv('/etc/hosts'"):
            with self.assertRaises(Exception, msg=sql):
                one(self.db, sql)
        self.assertFalse((self.art.parent / "x.csv").exists())
        self.assertEqual(one(self.db, "SELECT count(*) n FROM Wells")["rows"], [[2]])


if __name__ == "__main__":
    unittest.main()
