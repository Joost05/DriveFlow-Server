"""Build a redistributable NL speed catalog from the latest official NDW ZIP.

Runs in GitHub Actions Ubuntu with GDAL installed, never on the phone/HAOS.
Only public road information is downloaded; no account data is handled.
"""
from __future__ import annotations
import argparse
import gzip
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request, urlopen
import zipfile
from datetime import datetime

from convert_ndw_gpkg import convert
from speed_catalog import import_jsonl

INDEX = "https://downloads.rijkswaterstaatdata.nl/wkd/geogegevens/Geopackage/Maximum%20Snelheden/"
ARCHIVE_LIMIT = 800 * 1024**2
GPKG_LIMIT = 4 * 1024**3
MIN_FREE = 6 * 1024**3
NAME_RE = re.compile(r"^(\d{2})-(\d{2})-(\d{4})\.zip$", re.I)

class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

def discover_latest():
    with urlopen(Request(INDEX, headers={"User-Agent": "DriveFlowSpeedCatalog/1.0"}), timeout=30) as response:
        html = response.read(100_000).decode("utf8")
    parser = Links()
    parser.feed(html)
    choices = []
    for href in parser.links:
        name = unquote(href.rsplit("/", 1)[-1])
        match = NAME_RE.fullmatch(name)
        if match:
            date = datetime(int(match[3]), int(match[2]), int(match[1]))
            if date <= datetime.now():
                choices.append((date, urljoin(INDEX, href)))
    if not choices:
        raise RuntimeError("No dated official WKD speed archive found")
    date, url = max(choices)
    if urlparse(url).hostname != "downloads.rijkswaterstaatdata.nl":
        raise RuntimeError("Unexpected archive host")
    return date.strftime("%Y-%m"), url

def download(url, dest):
    amount = 0
    with urlopen(Request(url, headers={"User-Agent": "DriveFlowSpeedCatalog/1.0"}), timeout=120) as response:
        with dest.open("wb") as sink:
            while True:
                block = response.read(1024**2)
                if not block:
                    break
                amount += len(block)
                if amount > ARCHIVE_LIMIT:
                    raise RuntimeError("NDW archive exceeds approved size")
                sink.write(block)
    if amount < 1024**2:
        raise RuntimeError("Unexpectedly small NDW archive")

def extract_gpkg(source, dest):
    with zipfile.ZipFile(source) as archive:
        valid = [x for x in archive.infolist()
                 if x.filename.lower().endswith(".gpkg") and not x.is_dir()
                 and x.file_size <= GPKG_LIMIT and not x.filename.startswith("/")
                 and ".." not in x.filename.replace("\\", "/").split("/")]
        if len(valid) != 1:
            raise RuntimeError(f"Expected exactly one GeoPackage, found {len(valid)}")
        with archive.open(valid[0]) as stream, dest.open("wb") as output:
            shutil.copyfileobj(stream, output, 1024**2)

def sha(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024**2), b""):
            result.update(block)
    return result.hexdigest()

def build(out):
    out.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(out).free < MIN_FREE:
        raise RuntimeError("Need at least 6 GiB free disk for official WKD processing")
    version, source_url = discover_latest()
    with tempfile.TemporaryDirectory(prefix="ndw-wkd-") as folder:
        tmp = Path(folder)
        archive = tmp / "source.zip"
        gpkg = tmp / "source.gpkg"
        jsonl = tmp / "roads.jsonl"
        db = tmp / "speed_nl.sqlite"
        download(source_url, archive)
        extract_gpkg(archive, gpkg)
        converted = convert(gpkg, jsonl)
        catalog = import_jsonl(jsonl, db, version)
        # Refuse obviously incomplete national catalogs.
        if catalog["segments"] < 100_000:
            raise RuntimeError("Too few valid road segments in catalog; schema mismatch?")
        compressed = out / "speed_nl.sqlite.gz"
        with db.open("rb") as original, gzip.open(compressed, "wb", compresslevel=6) as gz:
            shutil.copyfileobj(original, gz, 1024**2)
        manifest = {
            "schema": 1, "dataset_version": version,
            "source_url": source_url,
            "source": "NDW/WKD Maximum snelheden",
            "license": "CC0 1.0 (verify dataset-specific terms)",
            "segments": catalog["segments"],
            "archive_file": compressed.name,
            "archive_sha256": sha(compressed),
            "sqlite_sha256": sha(db),
            "sqlite_bytes": db.stat().st_size,
            "converted_features": converted["features"],
            "skipped_features": converted["skipped"],
        }
        (out / "speed_catalog.json").write_text(json.dumps(manifest, indent=2) + "\n")
        return manifest

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("dist"))
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    manifest = build(args.out)
    if args.github_output:
        with args.github_output.open("a") as output:
            output.write("tag=speed-catalog-" + manifest["dataset_version"] + "\n")
    print(json.dumps(manifest, indent=2))
