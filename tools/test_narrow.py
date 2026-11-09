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
    def test_table_cards_keep_rules_and_any_unknown_hint(self):
        line = narrow.column_card({"name": "phase", "alias": "current phase", "dtype": "String", "utype": None, "keyval": {}, "values": ["A", "B"],
                                     "hints": ["The phase", "Make sure to uppercase the values", "For example: given a becomes A"]})
        self.assertEqual(line, 'phase String upper "current phase": A, B | The phase')
        self.assertEqual(narrow.column_card({"name": "country", "dtype": "String", "keyval": {"NO": "Norway"}, "values": ["NO"],
                                               "hints": ["Use 'country = 'NO'' for 'norway'.", "NEVER use SQL LIKE in the WHERE"]}), "country String exact: NO=Norway")
        self.assertEqual(narrow.column_card({"name": "depth", "dtype": "Double", "utype": "meters", "values": ["1", "2", "3"],
                                               "hints": ["ALWAYS use SQL CAST AS DOUBLE PRECISION", "Use 'depth = cast(1 as DOUBLE PRECISION)' for 'x'."]}), "depth Double meters cast: 1, 2 +")
        self.assertEqual(narrow.column_card({"name": "n", "dtype": "String", "values": [str(i) for i in range(20)], "hints": ["Make sure to use the SQL LIKE here"]}), "n String like: 0, 1, 2 +")


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
