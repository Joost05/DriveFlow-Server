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
