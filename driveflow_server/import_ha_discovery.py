"""One-time migration from HA Road Discovery's exported discovery IDs.

Usage:
    DRIVEFLOW_API_TOKEN='...' python import_ha_discovery.py \
        /path/to/road_discovery_discovered.json https://api.mijndriveflow.nl

Runs on your own computer. Never sends Home Assistant credentials or GPS trails.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from road_discovery_store import CATALOG

UPLOAD_BATCH = 1000  # Stay below the HTTP body limit even for long segment IDs.


def load_ids(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("ids"), list):
        raise ValueError("verwacht Road Discovery JSON met veld 'ids'")
    return data["ids"]


def upload(ids: list[str], server: str, token: str) -> int:
    if not token or len(token) < 32:
        raise ValueError("DRIVEFLOW_API_TOKEN ontbreekt of is te kort")
    if not server.startswith("https://") and not server.startswith("http://127.0.0.1:"):
        raise ValueError("gebruik HTTPS voor een externe server")
    total = 0
    for start in range(0, len(ids), UPLOAD_BATCH):
        body = json.dumps({"catalog": CATALOG, "source": "home_assistant",
                           "ids": ids[start:start + UPLOAD_BATCH]}).encode("utf-8")
        req = Request(server.rstrip("/") + "/v1/discovery/merge", data=body,
                      headers={"Authorization": "Bearer " + token,
                               "Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=30) as resp:
            result = json.load(resp)
        if not result.get("ok"):
            raise RuntimeError("server heeft de import niet bevestigd")
        total += result["added"]
    return total


def main() -> int:
    if len(sys.argv) != 3:
        print("Gebruik: python import_ha_discovery.py <export.json> <server-URL>")
        return 2
    try:
        ids = load_ids(Path(sys.argv[1]))
        added = upload(ids, sys.argv[2], os.environ.get("DRIVEFLOW_API_TOKEN", ""))
    except (ValueError, OSError, HTTPError, URLError, RuntimeError) as exc:
        print("Import mislukt:", type(exc).__name__, file=sys.stderr)
        return 1
    print(f"Import klaar: {len(ids)} records ontvangen, {added} nieuwe segmenten opgeslagen")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
