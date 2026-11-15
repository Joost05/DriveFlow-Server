"""Read-only audit of the nationwide Discovery2 candidate mapping.

Reports geometrically risky candidates *without* approving them. Reads the
existing comparison CSV and optionally the live legacy discovery DB in RO mode.
Produces a new report (exclusive creation); never edits discovery progress.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path

from road_discovery_store import CATALOG as LEGACY_CATALOG

HEADERS = ["new_id", "status", "old_id", "offset_m", "old_discovered"]
SAMPLE_LIMIT = 30


def _discovered(db_path: Path | None):
    if db_path is None:
        return None
    if db_path.is_symlink() or not db_path.is_file():
        raise ValueError("Legacy DB missing or symlink")
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def audit(candidate_csv: Path, report_json: Path, legacy_db: Path | None = None) -> dict:
    if candidate_csv.is_symlink() or not candidate_csv.is_file():
        raise ValueError("Candidate CSV missing or symlink")
    if report_json.exists() or report_json.is_symlink():
        raise FileExistsError("Never overwrite existing audit report")
    if candidate_csv.resolve() == report_json.resolve():
        raise ValueError("Input cannot be output")
    samples = {key: [] for key in (
        "ambiguous", "near_10m", "shared_legacy_segment", "unmatched", "invalid")}
    counts = Counter()
    with tempfile.TemporaryDirectory(prefix="discovery2-audit-") as tmp:
        with sqlite3.connect(str(Path(tmp) / "counts.sqlite")) as index:
            index.execute("CREATE TABLE candidate(new_id TEXT PRIMARY KEY, old_id TEXT, offset REAL)")
            index.execute("CREATE INDEX old_idx ON candidate(old_id)")
            with candidate_csv.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames != HEADERS:
                    raise ValueError("Unexpected comparison CSV schema")
                for row in reader:
                    state = row["status"]
                    if state not in {"candidate", "ambiguous", "new", "invalid_geometry"}:
                        raise ValueError("Unknown comparison status")
                    if not row["new_id"]:
                        raise ValueError("Empty new segment ID")
                    counts["total_segments"] += 1
                    counts[state] += 1
                    if state == "candidate":
                        if not row["old_id"]:
                            raise ValueError("Candidate missing legacy ID")
                        try:
                            offset = float(row["offset_m"])
                        except (TypeError, ValueError) as exc:
                            raise ValueError("Invalid candidate offset") from exc
                        if not math.isfinite(offset) or offset < 0 or offset > 10:
                            raise ValueError("Candidate offset out of range")
                        index.execute("INSERT INTO candidate VALUES (?,?,?)",
                                      (row["new_id"], row["old_id"], offset))
                        if offset >= 7:
                            counts["near_10m"] += 1
                            if len(samples["near_10m"]) < SAMPLE_LIMIT:
                                samples["near_10m"].append(
                                    {"new_id": row["new_id"], "old_id": row["old_id"],
                                     "offset_m": offset})
                    elif state == "ambiguous" and len(samples["ambiguous"]) < SAMPLE_LIMIT:
                        samples["ambiguous"].append({"new_id": row["new_id"]})
                    elif state in {"new", "invalid_geometry"}:
                        key = "invalid" if state == "invalid_geometry" else "unmatched"
                        if len(samples[key]) < SAMPLE_LIMIT:
                            samples[key].append({"new_id": row["new_id"]})
                    if counts["total_segments"] % 10000 == 0:
                        index.commit()
            index.commit()
            groups = index.execute(
                "SELECT old_id, COUNT(*) AS n FROM candidate GROUP BY old_id "
                "HAVING n>1 ORDER BY n DESC,old_id LIMIT ?", (SAMPLE_LIMIT,)
            ).fetchall()
            samples["shared_legacy_segment"] = [
                {"old_id": old, "new_segment_count": n} for old, n in groups
            ]
            counts["legacy_segments_with_multiple_candidates"] = index.execute(
                "SELECT COUNT(*) FROM (SELECT old_id FROM candidate GROUP BY old_id HAVING COUNT(*)>1)"
            ).fetchone()[0]
            counts["distinct_legacy_segments_with_candidates"] = index.execute(
                "SELECT COUNT(DISTINCT old_id) FROM candidate"
            ).fetchone()[0]
            # The candidate mapping is not an identity proof. In particular a
            # single legacy segment may legitimately split into many OSM ways.
            # This signal means review, NOT automatic rejection or approval.
            if legacy_db is not None:
                with _discovered(legacy_db) as source:
                    source.execute("ATTACH DATABASE ? AS candidate_index",
                                   (str(Path(tmp) / "counts.sqlite"),))
                    counts["server_discovered_legacy_segments"] = source.execute(
                        "SELECT COUNT(DISTINCT segment_id) FROM discovery_segments WHERE catalog=?",
                        (LEGACY_CATALOG,)
                    ).fetchone()[0]
                    counts["server_discovered_legacy_segments_with_candidates"] = source.execute(
                        "SELECT COUNT(DISTINCT d.segment_id) FROM discovery_segments d "
                        "JOIN candidate_index.candidate c ON c.old_id=d.segment_id "
                        "WHERE d.catalog=?", (LEGACY_CATALOG,)
                    ).fetchone()[0]
    result = {
        "status": "manual_review_required",
        "catalogue_activation_allowed": False,
        "safe_to_auto_approve": False,
        "legacy_database_modified": False,
        "counts": dict(counts),
        "samples": samples,
        "risk_thresholds": {"large_offset_m": 7, "candidate_max_offset_m": 10},
        "notes": "Geometry-only candidates cannot prove road identity. "
                 "Parallel roads, shared old IDs and junctions require independent verification.",
    }
    report_json.parent.mkdir(parents=True, exist_ok=True)
    with report_json.open("x", encoding="utf-8") as out:
        json.dump(result, out, ensure_ascii=False, indent=2)
        out.write("\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read-only Discovery2 candidate risk audit")
    parser.add_argument("mapping", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--legacy-db", type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(args.mapping, args.report, args.legacy_db), indent=2))
