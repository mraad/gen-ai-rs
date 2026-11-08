---
name: northsea-duckdb
description: Answer questions about North Sea oil and gas data (wells, pipelines, discoveries) with read-only DuckDB SQL. Use when the user asks about those features, counts, statistics or a map of them.
metadata:
  gait-model-invocable: "true"
---
Tools: northsea_layers and northsea_query.

1. Call northsea_layers once with no table. It returns a card for every table, and its legend explains the flags. Match each word in the question to a column alias, a unit and a value. Obey the flags. Call it with a table only if a card does not settle what a column means.
2. Write the SELECT and call northsea_query. If it fails, read the error, change the SQL, and try again.
3. Answer only from the result. The user cannot see tool results, so write out in your answer every value the question asks for. Say how you read each term, for example: "depth" means water_depth, in meters. If a column or value is not clear, query the distinct values. Do not guess.

Data:

- northsea_query runs read-only SELECTs. It cannot read files, change data or change settings.
- Use `table_name` in SQL. `display` is the column that names a feature. Each table also has `OBJECTID` and `geometry`. Run `DESCRIBE <table>` to see all columns.
- A NULL value means no data. It does not mean 0. Leave NULL out of a comparison or a statistic, and report how many you left out.
- A result has one row for each feature. A JOIN can give duplicates, so keep one row for each `OBJECTID`.
- A result with `geometry` shows `row_count`, `stats` and 10 sample rows. A result with no `geometry` shows up to 200 rows. To read or list the values, select them without `geometry`. Use `row_count`, `count(*)` or an aggregate for totals. Do not count rows in the shown output.

Space:

- All `geometry` values are WGS84, in degrees, with x = longitude and y = latitude. `ST_DWithin(a, b, 5000)` means 5000 degrees, so it is true for all features.
- For a distance in meters, project both geometries in the WHERE or a CTE, and measure there: `ST_Transform(geometry, 'EPSG:4326', '+proj=laea +lat_0=58 +lon_0=3 +datum=WGS84 +units=m +no_defs', always_xy := true)`. Then `ST_DWithin(a, b, meters)` and `ST_Distance(a, b)` work for points, lines and polygons. 1 mi = 1609.344 m. Do not use `ST_Distance_Spheroid` for lines, EPSG:3035 or one UTM zone. Return the original `geometry` to the map.
- `ST_Intersects`, `ST_Within`, `ST_Contains` and a bounding box are valid in degrees: `ST_Within(geometry, ST_MakeEnvelope(west, south, east, north))`.

Map:

- To show features on the map, select OBJECTID, the display column, the fields the question needs, and geometry, with one geometry type in each query. The tool writes one map file for each query, in the format the user chose on the page, and the map page draws all of them. Dates must be ISO 8601 strings: `strftime(col, '%Y-%m-%dT%H:%M:%S')`. Tell the user the features are on the map. Do not paste coordinates.
- When the question picks features of one table by their relation to another table (near, within, crosses, connected to), show both: call northsea_query once with `queries`, a list of up to 6 `{name, sql}`, one for the answer and one for the related features. Names are lowercase with digits and underscores, for example `wells_near` and `pipelines_in_service`. The queries do not see each other, so repeat a shared filter in each. Map one table with `sql` alone.

Read references/rules.md with read_skill_resource only if the question asks for statistics or a z-score, or you want a worked example of two layers.
