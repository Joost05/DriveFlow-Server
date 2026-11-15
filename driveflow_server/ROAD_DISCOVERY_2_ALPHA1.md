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


## Experimental server-side staged upload (not released; do not install yet)

This feature branch now exposes **authenticated**, revision-pinned routes:

- `PUT /v1/discovery2/catalog/stage` accepts exactly 247,931,820 bytes of
  `application/json` via streaming with the header
  `X-Content-SHA256: 2aeb95e57db9f23cc046d7ffd1fa14edc73bdebd79c0b57c82aba5b7763d2ffe`.
  The actual bytes are hashed while streaming; a client-provided digest alone
  is insufficient. A partial or invalid file is deleted.
- `GET /v1/discovery2/catalog/status` reports whether the known version
  is staged. This route and the upload both require the same Bearer API token
  as other DriveFlow endpoints.

The new file is stored at `/data/discovery2/roads_nl_2_alpha1.json`;
the old `/share/driveflow/roads_nl.json`, legacy endpoints, existing
discovery DB and Android installation **are not modified**. Overwrites are
rejected (HTTP 409). This is **staging only**: no comparison, migration,
activation, Android sync or Play deployment is included yet.

After the revised add-on is explicitly reviewed, installed, and verified,
use a machine on the same LAN as Home Assistant. In PowerShell:

```powershell
cd D:\DriveFlow\RoadDiscovery2
$server = "http://<HOME_ASSISTANT_IP>:8098"
$token = Read-Host "DriveFlow API-token"
curl.exe --fail-with-body -X PUT "$server/v1/discovery2/catalog/stage" -H "Authorization: Bearer $token" -H "Content-Type: application/json" -H "X-Content-SHA256: 2aeb95e57db9f23cc046d7ffd1fa14edc73bdebd79c0b57c82aba5b7763d2ffe" --data-binary "@roads_nl_2_alpha1.json"
curl.exe --fail-with-body "$server/v1/discovery2/catalog/status" -H "Authorization: Bearer $token"
Remove-Variable token
```

**Do not use the Cloudflare tunnel for this 248 MB upload.**
Upload from a trusted local network directly to the LAN IP on port 8098;
public reverse proxies can impose body-size and request-duration limits.
Do not paste API tokens in chats, screenshots, or a saved script.

Release gates remaining: exercise real HTTP upload on test HAOS instance,
disk space checks, safe deployment/rollback, server-side legacy comparison,
mapping of synchronized **and pending offline** discoveries, verified activation
and Android opt-in download. No database migration should be enabled until
these pass.


## Experimental background comparison on the server (not yet released)

The feature branch now has these additional **authenticated** endpoints:

- `POST /v1/discovery2/compare/start` with an empty body, replies HTTP 202
  when the worker starts; HTTP 409 when a job is running or results exist;
  HTTP 503 when the verified legacy catalogue is unavailable.
- `GET /v1/discovery2/compare/status` returns worker stage, processed count,
  any error and report availability.
- `GET /v1/discovery2/compare/report` returns a **review-only** comparison
  summary, including the cursor of the legacy discoveries at snapshot time.

After the revised server has been tested on an isolated HAOS copy and
deployed, start the comparison from a LAN PowerShell session:

```powershell
curl.exe --fail-with-body -X POST "$server/v1/discovery2/compare/start" -H "Authorization: Bearer $token" -H "Content-Length: 0"
curl.exe --fail-with-body "$server/v1/discovery2/compare/status" -H "Authorization: Bearer $token"
curl.exe --fail-with-body "$server/v1/discovery2/compare/report" -H "Authorization: Bearer $token"
```

The server verifies the legacy `roads_nl.json` file and the staged
SHA-256. It reads its own `discovery_segments` data via a SQLite
**read-only** connection. The full new catalogue is streamed by `ijson`;
the old catalogue gets an on-disk spatial index inside the add-on's
private data directory. Requires at least **1 GiB** of free storage for
temporary indexing, mapping and safety margin. The 1.39-million-segment
scan runs on a daemon thread so standard HTTP requests can continue.

Files created only inside `/data/discovery2/`:

- `comparison_report.json` — counts and progress-cursor snapshot
- `comparison_candidates.csv` — per-segment *candidate* geometric mapping

Mapping may be unreliable near ramps, intersections or parallel lanes,
and matches are deliberately conservative. These are NOT authorized
migration records. Discoveries that remain in Android's local pending
outbox after the server snapshot are not included; there is no automatic
switch, green-state transfer, or download to Android in this phase.
Do not remove the old catalogue, alter `discovery_segments` rows or
install from this development branch until integration and HAOS tests pass.


## Discovery2 migration shadow preview (development branch, not released)

This implements a **non-destructive**, repeatable *refresh* of an isolated
preview; it is not an activated v2 discovery table.

After successful upload and completed comparison, the server offers:

- `POST /v1/discovery2/preview/build`: one-time import of comparison
  candidates to `/data/discovery2/shadow_preview.sqlite`; rejects a
  duplicate build without overwriting it.
- `POST /v1/discovery2/preview/refresh`: replay up to 5,000 new,
  server-synchronized legacy discovery records into shadow preview and
  persist the processed legacy cursor. This is safe to repeat; call it
  until `caught_up_with_server` becomes true.
- `GET /v1/discovery2/preview/status`: candidate/preview counts,
  processed cursor and explicit `ready_for_switch: false`.

All routes use the existing Bearer token and POST routes expect an empty body.
The source legacy SQLite DB is opened **read-only**, with a separate
`shadow_preview.sqlite` for computed candidate state. Every candidate is
marked **needs_review**; none is treated as approved green. Ambiguous
parallel roads are excluded. The original `discovery_segments`, its cursor,
legacy geometry, and Android cache/outbox are untouched.

Offline handling: a discovery made on Android while offline continues to
sit in the existing Android outbox; when it later reaches the ordinary
`POST /v1/discovery/merge` endpoint, calling
`/v1/discovery2/preview/refresh` will include it automatically. This is
only an eventual-replay mechanism, **not** proof all Android outboxes are
empty. The server cannot attest that without an explicit Android handshake.

### Remaining release blockers

- Verify candidate accuracy on a sample of parallel carriageways,
  junctions, bridges, and roads whose segmentation differs.
- Build an **explicit** validated approval/mapping process; the current
  `needs_review` preview must never be used as approved green progress.
- Build offline-outbox reconciliation and a client opt-in/rollback handshake
  for Android and Android Auto before v2 activation.
- Exercise Python tests and integration tests in an isolated HAOS setup.
  In particular, confirm available RAM and free disk on Odroid M1S.
- Publish a tested server release and Android update **after** these gates;
  nothing here is deployed to the running server automatically.


### Automatically including discoveries synchronized after preview creation

After `/preview/build`, the server automatically schedules the first shadow
refresh. Every successful existing `POST /v1/discovery/merge` that adds
legacy segment IDs schedules another coalesced background refresh. A service
restart also queues a replay when `shadow_preview.sqlite` is present.
The job reads new events in ordered batches of 5,000 and saves its cursor in
the separate preview database. Retries are idempotent.

**This does not make a phone's unsent discoveries magically visible.** They
must first be delivered by the existing Android outbox. The server cannot
mark v2 as fully ready until the Android client provides a trustworthy
outbox-drained acknowledgement and the route candidates are reviewed.
The server continues to report `ready_for_switch: false`.


## Client synchronization preflight + inactive v2 download (development)

The server also accepts an **advisory** client synchronization acknowledgement:

- `POST /v1/discovery2/clients/sync-complete`: authenticated,
  `X-DriveFlow-Device-Id` required, JSON body containing
  `{"catalog":"ha-road-discovery-nl-v0.18","downloaded_cursor":123,
    "pending_count":0,"sync_complete":true}`.
  The server checks the device identifier, confirms the cursor equals the
  currently visible legacy server cursor, and records the acknowledgement
  in a separate SQLite DB. Older servers may return HTTP 404; Android
  treats the acknowledgement as optional, **after** ordinary legacy
  sync succeeds and the local outbox is empty.
- `GET /v1/discovery2/clients/status`: reports the number of client
  acknowledgements at the current legacy cursor. It **never** reports
  `ready_for_switch:true`: the server cannot know every phone is
  online, nor can a one-time acknowledgement guarantee its outbox stays
  empty while GPS discovery is running.
- `GET /v1/discovery2/catalog/download`: returns the separately staged
  v2 catalogue only when its exact pinned SHA-256 and length pass. Uses
  the original authentication and returns `X-Content-SHA256`.

Android's separate `stageDiscovery2Catalog(onProgress)` API streams
the full 247,931,820-byte file to its own private directory and checks
the exact SHA-256 before a rename. This method is **not wired to an
automatic job or UI**; it does not replace the active segment database,
mark any new roads as discovered, or start another GPS tracker.

Release blockers: real HAOS upload/download tests, conservative mapping
review, explicit approval of source-to-target geometry, storage/memory
measurements on Android, an atomic client/database switch with rollback,
and functional Android Auto driving tests. Do not publish a partial
migration as a complete v2 release.
