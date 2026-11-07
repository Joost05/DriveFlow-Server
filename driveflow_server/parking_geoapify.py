"""Authenticated Geoapify parking proxy; no user coordinates in logs or backups."""
import json
import math
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

_LOCK = threading.Lock()
_CACHE = {}
_LAST_REQUEST = 0.0
RADIUS = 1200


class ParkingError(Exception):
    def __init__(self, kind, status=0):
        self.kind, self.status = kind, status


def normalize(payload, latitude, longitude):
    features = payload.get("features")
    if not isinstance(features, list):
        raise ParkingError("INVALID_RESPONSE")
    result, seen = [], set()
    for feature in features[:80]:
        properties = feature.get("properties", {})
        coordinates = feature.get("geometry", {}).get("coordinates", [])
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            continue
        lon, lat = coordinates[:2]
        if not all(isinstance(x, (float, int)) and math.isfinite(x) for x in (lat, lon)):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        uid = str(properties.get("place_id") or "")
        if not uid or uid in seen:
            continue
        seen.add(uid)
        lat_r, lon_r = math.radians(lat - latitude), math.radians(lon - longitude)
        a = math.sin(lat_r / 2) ** 2 + math.cos(math.radians(latitude)) * math.cos(math.radians(lat)) * math.sin(lon_r / 2) ** 2
        meters = 6371000 * 2 * math.asin(min(1.0, math.sqrt(a)))
        if meters > RADIUS + 100:
            continue
        name = properties.get("name") or properties.get("address_line1") or "Parkeerplaats"
        result.append({"id": uid[:100], "name": str(name)[:100], "type": "Parkeren", "lat": lat, "lon": lon, "distance_m": round(meters)})
    return sorted(result, key=lambda item: item["distance_m"])[:12]


def search(latitude, longitude, key):
    global _LAST_REQUEST
    if not key:
        raise ParkingError("NOT_CONFIGURED")
    if not all(math.isfinite(x) for x in (latitude, longitude)) or not (-90 <= latitude <= 90 and -180 <= longitude <= 180) or (latitude == 0 and longitude == 0):
        raise ParkingError("INVALID_LOCATION")
    # Quantize cache keys to avoid persisting precise GPS coordinates.
    cell = (round(latitude, 3), round(longitude, 3))
    with _LOCK:
        entry = _CACHE.get(cell)
        if entry and time.monotonic() - entry[0] < 300:
            return entry[1]
        if time.monotonic() - _LAST_REQUEST < 3:
            raise ParkingError("RATE_LIMIT", 429)
        _LAST_REQUEST = time.monotonic()
    query = urlencode({"categories": "parking", "filter": f"circle:{longitude:.6f},{latitude:.6f},{RADIUS}", "limit": 40, "apiKey": key})
    request = Request("https://api.geoapify.com/v2/places?" + query, headers={"User-Agent": "DriveFlow-Server/0.6", "Accept": "application/json"})
    try:
        with urlopen(request, timeout=12) as response:
            if response.status != 200:
                raise ParkingError("SERVER", response.status)
            raw = response.read(262145)
            if len(raw) > 262144:
                raise ParkingError("INVALID_RESPONSE")
            items = normalize(json.loads(raw), latitude, longitude)
    except HTTPError as exc:
        raise ParkingError("RATE_LIMIT" if exc.code == 429 else "SERVER", exc.code) from None
    except (URLError, TimeoutError):
        raise ParkingError("NETWORK") from None
    except (ValueError, UnicodeError, KeyError, TypeError):
        raise ParkingError("INVALID_RESPONSE") from None
    with _LOCK:
        if len(_CACHE) > 80:
            _CACHE.clear()
        _CACHE[cell] = (time.monotonic(), items)
    return items
