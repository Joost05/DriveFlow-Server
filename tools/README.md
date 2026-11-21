# DriveFlow NL speed catalog builder

The speed dataset pipeline is separate from the DriveFlow app, the HAOS server,
and all personal ride data.

The official [NDW/WKD Maximum Snelheden](https://docs.ndw.nu/handleidingen/wkd/wkd_maxsnelheden/)
GeoPackage is published monthly. On the 9th of each month the GitHub Action:

1. Downloads the most recent dated WKD ZIP from Rijkswaterstaat.
2. Uses GDAL to reproject geometry to WGS84 (EPSG:4326).
3. Validates speeds and time windows. Discards uncertain partial/directional
   segments instead of presenting a potentially incorrect limit.
4. Builds a dedicated SQLite/RTree lookup database.
5. Checks it has at least 100,000 valid segments.
6. Publishes a compressed SQLite file and SHA256 manifest as a GitHub release
   named `speed-catalog-YYYY-MM`.

Server installations can download this public asset over HTTPS, verify both
hashes and SQLite integrity, then atomically replace their **separate** speed
database. Trips, mileage history and Road Discovery are not touched.

To validate without the national download:
`python -m unittest discover -s tools/tests -p 'test_*.py' -v`

**Publication must stay disabled until a full national 2026 GeoPackage test
passes and the resulting release has been checked on the HAOS server.**
