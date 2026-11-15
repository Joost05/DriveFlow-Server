"""Immutable staged Road Discovery 2.0 catalogue; no migration or activation.

The alpha1 SHA-256 and size below are copied from the user's validated
manifest. Only those exact bytes may be staged. This intentionally does not
trust a client-provided digest as an authoritative identity.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import uuid
from pathlib import Path

EXPECTED_SHA256 = "2aeb95e57db9f23cc046d7ffd1fa14edc73bdebd79c0b57c82aba5b7763d2ffe"
EXPECTED_BYTES = 247_931_820
REVISION = "road-discovery-2-osm-explicit-v1"
SEGMENTS = 1_393_159
LENGTH_KM = 56_254.329
NAME = "roads_nl_2_alpha1.json"
CHUNK = 1024 * 1024


class UploadError(ValueError):
    pass


def catalogue_path(data_dir: Path):
    return data_dir / "discovery2" / NAME


def free_space_ok(data_dir: Path) -> bool:
    # Full file + intermediate upload + free reserve, even if already staged.
    return shutil.disk_usage(data_dir).free >= EXPECTED_BYTES + 128 * 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def staged_status(data_dir: Path) -> dict:
    target = catalogue_path(data_dir)
    if target.is_symlink() or not target.is_file():
        return {"state": "not_uploaded", "revision": REVISION,
                "expected_bytes": EXPECTED_BYTES, "activated": False,
                "migration_performed": False}
    if target.stat().st_size != EXPECTED_BYTES:
        return {"state": "invalid", "revision": REVISION, "activated": False,
                "migration_performed": False}
    return {"state": "staged", "revision": REVISION, "size_bytes": EXPECTED_BYTES,
            "sha256": EXPECTED_SHA256, "segments": SEGMENTS,
            "length_km": LENGTH_KM, "activated": False,
            "migration_performed": False,
            "warning": "Stored only; no discovery state or road catalogue has been switched."}


def receive_catalogue(input_stream, length: int, data_dir: Path, header_digest: str) -> dict:
    """Stream exactly one pinned alpha1 file into a private staging location."""
    if length != EXPECTED_BYTES:
        raise UploadError(f"Onjuiste bestandsgrootte: verwacht {EXPECTED_BYTES} bytes")
    if not hmac.compare_digest(header_digest.strip().lower(), EXPECTED_SHA256):
        raise UploadError("Manifest-SHA256 komt niet overeen met toegestane versie")
    target = catalogue_path(data_dir)
    directory = target.parent
    directory.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise FileExistsError("Catalogus is al aanwezig; geen overschrijving toegestaan")
    if not free_space_ok(data_dir):
        raise UploadError("Onvoldoende vrije schijfruimte")
    temp = directory / ("." + uuid.uuid4().hex + ".upload")
    checksum = hashlib.sha256()
    remaining = length
    try:
        with temp.open("xb") as out:
            while remaining:
                block = input_stream.read(min(CHUNK, remaining))
                if not block:
                    raise UploadError("Upload voortijdig afgebroken")
                out.write(block)
                checksum.update(block)
                remaining -= len(block)
            out.flush()
            os.fsync(out.fileno())
        if not hmac.compare_digest(checksum.hexdigest(), EXPECTED_SHA256):
            raise UploadError("Inhoud heeft een andere SHA256 dan het goedgekeurde manifest")
        # Same-volume staging. No existing target may be replaced.
        os.link(temp, target)
        temp.unlink()
        return staged_status(data_dir)
    finally:
        temp.unlink(missing_ok=True)
