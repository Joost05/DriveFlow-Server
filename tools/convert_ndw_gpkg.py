"""Convert NDW/WKD GeoPackage maximum speeds into DriveFlow WGS84 JSONL.

Run on a workstation/CI image with GDAL ('ogr2ogr'), not on HAOS itself.
Input data is never downloaded implicitly. This importer refuses incomplete
timed/directional records rather than manufacturing a limit.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from speed_catalog import validate_segment

def first(properties: dict, *keys):
    props = {str(k).casefold(): v for k, v in properties.items()}
    for key in keys:
        value = props.get(key.casefold())
        if value is not None and str(value).strip():
            return value
    return None

def speed_value(raw):
    try:
        value = int(str(raw).strip())
    except (ValueError, TypeError):
        return None
    return value if value in (5,15,20,30,40,50,60,70,80,90,100,120,130) else None

def clock_value(raw):
    if raw is None:
        return None
    text = str(raw).strip()
    if " " in text:
        text = text.rsplit(" ", 1)[-1]
    if "." in text:
        text = text.split(".", 1)[0]
    pieces = text.split(":")
    if len(pieces) not in (2,3):
        raise ValueError("invalid time field")
    hours, minutes = int(pieces[0]), int(pieces[1])
    seconds = int(pieces[2]) if len(pieces) > 2 else 0
    if hours not in range(24) or minutes not in range(60) or seconds not in range(60):
        raise ValueError("invalid time range")
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

def convert_feature(feature: dict, ordinal: int) -> list[dict]:
    properties = feature.get("properties") or {}
    road_id = first(properties, "FK_VELD1", "WVK_ID", "WVKID", "ID")
    speed = speed_value(first(properties, "MAXSHD"))
    if not road_id or speed is None:
        return []
    # Speed records explicitly limited to one direction cannot be resolved
    # without the road's canonical heading and a bearing-aware matcher.
    direction = first(properties, "KENM_RICHT", "KENMRICHT")
    if direction is not None and str(direction).strip().upper() not in ("", "B", "BEIDE", "BOTH", "0"):
        return []
    # Special rush-hour limits need a real-time signal: unknown instead.
    if speed_value(first(properties, "MAXSHD_STR")) is not None:
        return []
    begin_raw = first(properties, "BEGINTIJD")
    end_raw = first(properties, "EINDTIJD")
    alt_raw = first(properties, "MAXSHD_ALT")
    if any(x is not None for x in (begin_raw, end_raw, alt_raw)):
        if any(x is None for x in (begin_raw, end_raw, alt_raw)):
            return []
        try:
            begin, end = clock_value(begin_raw), clock_value(end_raw)
        except ValueError:
            return []
        alternate = speed_value(alt_raw)
        if alternate is None or begin == end:
            return []
    else:
        begin = end = alternate = None
    geometry = feature.get("geometry") or {}
    shape = geometry.get("type")
    raw = geometry.get("coordinates") or []
    pieces = [raw] if shape == "LineString" else raw if shape == "MultiLineString" else []
    output = []
    for index, piece in enumerate(pieces):
        # Do not trim a partial WKD subsection against an entire NWB road.
        # Geometry must be supplied already segmented by the source.
        if not isinstance(piece, list) or len(piece) < 2:
            continue
        for offset in range(0, len(piece)-1, 127):
            coordinates = [list(point[:2]) for point in piece[offset:offset+128]]
            if len(coordinates) < 2:
                continue
            item = {
                "id": f"{road_id}-{ordinal}-{index}-{offset}",
                "speed_kmh": speed,
                "coordinates": coordinates,
            }
            if begin is not None:
                item.update(time_begin=begin, time_end=end, alternate_kmh=alternate)
            try:
                validate_segment(item)
            except (ValueError, TypeError):
                continue
            output.append(item)
    return output

def convert(source: Path, destination: Path, layer: str | None = None):
    if not source.is_file():
        raise FileNotFoundError(source)
    command = ["ogr2ogr", "-f", "GeoJSONSeq", "/vsistdout/", str(source),
               "-t_srs", "EPSG:4326"]
    if layer:
        command.append(layer)
    counters = {"features": 0, "segments": 0, "skipped": 0}
    # Consume GDAL stdout as a stream to avoid storing hundreds of MB in RAM.
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("w", encoding="utf8") as sink:
            assert process.stdout is not None
            for line in process.stdout:
                if not line.strip():
                    continue
                counters["features"] += 1
                elements = convert_feature(json.loads(line.strip().lstrip("\x1e")), counters["features"])
                if not elements:
                    counters["skipped"] += 1
                for element in elements:
                    sink.write(json.dumps(element, separators=(",", ":")) + "\n")
                    counters["segments"] += 1
        _, errors = process.communicate()
        if process.returncode != 0:
            raise RuntimeError(f"ogr2ogr exit {process.returncode}: {errors[-1000:]}")
        if counters["segments"] == 0:
            raise RuntimeError("No valid road segments generated; verify source/layer/attributes")
        return counters
    except BaseException:
        destination.unlink(missing_ok=True)
        process.kill()
        process.wait()
        raise

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path, help="Official WKD maximum-speed GeoPackage")
    parser.add_argument("destination", type=Path, help="Validated WGS84 JSONL result")
    parser.add_argument("--layer", default=None)
    args = parser.parse_args()
    print(json.dumps(convert(args.source, args.destination, args.layer)))
