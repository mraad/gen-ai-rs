# North Sea SQL: statistics and a worked example

## Statistics

If the question asks for statistics, give the count, the minimum, the maximum, the mean, the median and the sample standard deviation: `count`, `min`, `max`, `avg`, `median`, `stddev_samp`. Calculate them on the features in the result, unless the question asks for a different population. State the population.

For a z-score: `CASE WHEN value IS NULL THEN NULL ELSE COALESCE((value - avg(value) OVER ()) / NULLIF(stddev_samp(value) OVER (), 0), 0) END`. If the standard deviation is 0 or there is one feature, the z-score is 0. A NULL value stays NULL.

To color features on the map by a value, also return that value as a numeric column named `color`, for example `... END AS color`. Values that span 0 get a blue to orange ramp centred on 0; other values go from blue (low) to orange (high).

## Two layers

Example, wells deeper than 350 m within 5 miles of an in service pipeline, with the pipelines:

```sql
-- wells_near
SELECT w.OBJECTID, w.well_name, w.water_depth, w.geometry FROM Wells w
WHERE w.water_depth > 350 AND EXISTS (SELECT 1 FROM Pipelines p WHERE p.current_phase = 'IN SERVICE'
  AND ST_DWithin(ST_Transform(w.geometry, 'EPSG:4326', '+proj=laea +lat_0=58 +lon_0=3 +datum=WGS84 +units=m +no_defs', always_xy := true),
                 ST_Transform(p.geometry, 'EPSG:4326', '+proj=laea +lat_0=58 +lon_0=3 +datum=WGS84 +units=m +no_defs', always_xy := true), 8046.72))
-- pipelines_in_service
SELECT OBJECTID, pipe_name, geometry FROM Pipelines WHERE current_phase = 'IN SERVICE'
```
