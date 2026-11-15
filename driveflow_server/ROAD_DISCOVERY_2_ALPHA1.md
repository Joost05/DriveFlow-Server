# Road Discovery 2.0 — server alpha1

This phase is a **standalone offline catalogue generator**, not a live server
migration. The current `/v1/discovery/catalog/roads` endpoint and pinned
`ha-road-discovery-nl-v0.18` catalogue remain unchanged. **Do not deploy a
generated 2.0 file over `/share/driveflow/roads_nl.json`.**

## Build locally

1. Obtain a current **Netherlands `.osm.pbf`** extract from a trusted OpenStreetMap
   distributor. Record its download date, original checksum and licensing details.
2. In a separate Python 3.12 environment install the optional generator package:
   `python -m pip install osmium`.
3. Run `python driveflow_server/build_discovery2_catalog.py netherlands-latest.osm.pbf roads_nl_2_alpha1.json`
   from the repository root. The output path must be new, to avoid overwriting anything.
4. Inspect `roads_nl_2_alpha1.json.manifest.json`: verify the SHA-256,
   file size, number of eligible ways/segments, excluded ways and kilometres.
   Confirm coverage geographically and compare to existing A/N totals.

The generated JSON has `revision` and `segments`; each segment includes
`id`, `road`, `a`, `b`, `length_m`, `highway` and `maxspeed_kmh`.
Segment IDs use `osm:w<way-id>:n<min-node-id>-n<max-node-id>`.
Only numeric **unconditional** maxspeed values >=60 km/h qualify; uncertainty,
conditional, directional and nonnumeric values are excluded. This is an
intentionally incomplete subset of the actual Dutch >=60 road network.
Highway class alone is never used to invent a speed.

## Required release gates (not implemented in alpha1)

- Review legal and conditional speed applicability (e.g. time-dependent Dutch
  motorway limits). Import currently excludes such tagged ways.
- Validate input extract integrity, output topology, nationwide coverage,
  and the provenance/revision and licensing attribution.
- Create a migration that maps the legacy HA A/N segment IDs to the 2.0 OSM IDs,
  preserving previous discovery timestamps and origins. Never wipe or double-count
  discoveries or the pending outbox.
- Add explicit versioned server download endpoints and compatible separate
  server progress denominators/ID namespaces. Keep old client sync endpoints live.
- Add Android opt-in download, verify checksum/size, import to a staging database,
  and atomically switch only when all compatibility checks pass.
- Run actual driving/offline tests and validate that the unified Android Auto
  map remains blue/unvisited, green/visited, orange/current route.

The generator is deliberately not imported or run by `server.py`. It requires
no new dependency in the Home Assistant add-on image. Nothing is pushed to
Google Play or the running server in alpha1.


## Compare old and new catalogues locally (read-only)

The original HA A/N file is usually stored at
`/config/custom_components/road_discovery/roads_nl.json` or copied to
`/share/driveflow/roads_nl.json`. This must be the **original old** JSON
file (approximately 39 MB), not the HA discoveries export.

Copy `roads_nl.json` to `D:\DriveFlow\RoadDiscovery2\` on Windows,
alongside the already-generated `roads_nl_2_alpha1.json`. Download the
`compare_discovery2_catalogs.py` script from this feature branch into
the same Windows directory.

Run these commands from PowerShell inside that directory:

```powershell
py -m pip install ijson
py .\compare_discovery2_catalogs.py .\roads_nl.json .\roads_nl_2_alpha1.json .\comparison_report.json
```

Optional: to calculate how many existing *discovered* IDs have geometric
candidates, export the old Home Assistant discovered ID list as
`old_discoveries.json` in the form `{"ids":["old-id", "..."]}` and run:

```powershell
py .\compare_discovery2_catalogs.py .\roads_nl.json .\roads_nl_2_alpha1.json .\comparison_with_discoveries.json --discovered .\old_discoveries.json
```

For a per-segment review CSV (potentially very large), append
`--mapping .\comparison_candidates.csv`. Never use the CSV as a
confirmed migration map without further review. Outputs must be new files
and are never overwritten automatically.

The report distinguishes `candidate`, `ambiguous`, `new` and
`invalid_geometry`. Candidate means **possible geographic overlap**
only; it does not prove road identity or safe migration, especially
where lanes, intersections or offset geometries differ. Direction
agreement within 25 degrees and endpoint distance <=10m are required,
with nearly-tied candidates rejected. Existing discoveries are never
changed. The old catalogue, new catalogue and running server remain
untouched. The comparison scans the full new file and may take a while.
