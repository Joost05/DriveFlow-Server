# DriveFlow Server v0.3.0

HAOS local app: back-up vault, incremental rit-sync, historical HA records, fuel/service/parking details and Home Assistant read-only API.

- Keeps `/data/backups` and `/data/driveflow_server.db` from version 0.2.
- Indexes the newest verified snapshot per device at startup. Read-only snapshot extraction.
- Conservative HA history reconciliation: original observations are separate from approved additional km/minutes.
- Trip identity deduplication across backup and sync.
- Independent, replaceable detailed records: fuel, maintenance and parking.
- Endpoints: `/v1/status`, `/v1/backups`, `/v1/ha/summary`, `/v1/ha/trips`, `/v1/ha/records`, `/v1/sync/batch`, `/v1/sync/details`.
- Home Assistant integration version 0.3 ships separately.

Never expose port 8098 unencrypted to the Internet. Back up the HAOS app before updating.
# v0.6.0 — Geoapify parking proxy

The optional `geoapify_api_key` add-on setting is read at startup. The key stays on
the HAOS server. `GET /v1/parking/nearby?lat=...&lon=...` requires the existing
`Authorization: Bearer <DriveFlow API token>` header. Cloudflare Access should
also be enforced at the external `api.mijndriveflow.nl` hostname. Results are
returned as `places[]` with `id`, `name`, `type`, `lat`, `lon`, `distance_m` and
`occupancy_known=false`. The service caches approximate 100m search cells for
five minutes, rejects 0,0 and rate-limits upstream queries. It never stores
search locations or logs the provider request URL/API key.

Upgrade the existing add-on *in place*. Never remove it or clear `/data`.
Do not publish HAOS port 8123 through this tunnel.


## Privacy and deployment data

The optional `ha_observation_windows.json` contains historical personal movement timestamps and is intentionally not committed. The server handles its absence; if historical migration overlap reconciliation is required, restore the file privately beside `snapshot_index.py` and include it in the deployed container as a private asset. Do not publish it in Git history.

# DriveFlow Server v0.3.0

HAOS local app: back-up vault, incremental rit-sync, historical HA records, fuel/service/parking details and Home Assistant read-only API.

- Keeps `/data/backups` and `/data/driveflow_server.db` from version 0.2.
- Indexes the newest verified snapshot per device at startup. Read-only snapshot extraction.
- Conservative HA history reconciliation: original observations are separate from approved additional km/minutes.
- Trip identity deduplication across backup and sync.
- Independent, replaceable detailed records: fuel, maintenance and parking.
- Endpoints: `/v1/status`, `/v1/backups`, `/v1/ha/summary`, `/v1/ha/trips`, `/v1/ha/records`, `/v1/sync/batch`, `/v1/sync/details`.
- Home Assistant integration version 0.3 ships separately.

Never expose port 8098 unencrypted to the Internet. Back up the HAOS app before updating.
# v0.6.0 — Geoapify parking proxy

The optional `geoapify_api_key` add-on setting is read at startup. The key stays on
the HAOS server. `GET /v1/parking/nearby?lat=...&lon=...` requires the existing
`Authorization: Bearer <DriveFlow API token>` header. Cloudflare Access should
also be enforced at the external `api.mijndriveflow.nl` hostname. Results are
returned as `places[]` with `id`, `name`, `type`, `lat`, `lon`, `distance_m` and
`occupancy_known=false`. The service caches approximate 100m search cells for
five minutes, rejects 0,0 and rate-limits upstream queries. It never stores
search locations or logs the provider request URL/API key.

Upgrade the existing add-on *in place*. Never remove it or clear `/data`.
Do not publish HAOS port 8123 through this tunnel.

# Road Discovery server storage (v0.6.3)

The server is the durable source of truth for *discovered segment IDs* from the
Home Assistant Road Discovery v0.18 Dutch road catalog. This phase does not
transfer road geometry or start a second GPS tracker. The existing DriveFlow
trip database and odometer calculations are unchanged.

All routes below use the existing `Authorization: Bearer <DriveFlow API token>`.

- `POST /v1/discovery/merge` accepts `{"catalog":"ha-road-discovery-nl-v0.18","source":"home_assistant","ids":["..."]}`. Batch max: 5000 IDs / 512 KiB. Duplicate IDs are ignored (also across devices).
- `GET /v1/discovery/status` returns `discovered_segments`, server `cursor`, and catalog ID.
- `GET /v1/discovery/changes?since=0&limit=1000` returns an incremental page of IDs with the next cursor and `has_more` flag. Page max: 2000 IDs.

To **migrate existing HA discoveries**, first export just the `ids` array from
Home Assistant's `.storage/road_discovery.discovered` or the published
`/local/road_discovery_discovered.json`. Send it in batches to the merge endpoint
with source `home_assistant`. Do not upload your Home Assistant credentials or
the entire `.storage` folder. Repeating a batch is safe.

For now, discovery IDs are append-only to avoid accidentally losing historical
progress. Catalog changes must be handled by a versioned migration, never by
silently reusing IDs from a different road geometry file. An offline area cache,
road geometry and automatic HA-side push will follow in later integration work.

## v0.6.6 — Authenticated HA roads catalogue
Copy the exact original `roads_nl.json` from Home Assistant's
`/config/custom_components/road_discovery/roads_nl.json` to the shared
`/share/driveflow/roads_nl.json` file (make the folder if needed).
The HAOS app mounts the share read-only. The file is *not* published on GitHub,
and is not included in the app image. Do not delete or replace the original.

`GET /v1/discovery/catalog/roads` requires the existing bearer API token and
returns the exact pinned HA v0.18 catalogue only after size and SHA-1 Git-blob
verification. Missing/wrong files return HTTP 503. The Android app validates
the file again before import and caches it offline.
