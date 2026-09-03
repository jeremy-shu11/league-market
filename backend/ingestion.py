from __future__ import annotations

import gzip
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = "1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def write_content_artifact(payload: object, artifact_dir: Path) -> dict:
    encoded = canonical_json(payload)
    digest = hashlib.sha256(encoded).hexdigest()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / f"{digest}.json.gz"
    if not path.exists():
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(gzip.compress(encoded, compresslevel=6, mtime=0))
        os.replace(temporary, path)
    return {
        "content_hash": digest,
        "artifact_path": str(path),
        "byte_count": len(encoded),
        "compressed_bytes": path.stat().st_size,
    }


def compact_snapshot(snapshot: dict) -> dict:
    """Keep league-scoped source data in SQLite; the global player map lives in its artifact."""
    return {
        "source": snapshot.get("source") or "sleeper",
        "provenance": snapshot.get("provenance") or "live",
        "league": snapshot.get("league") or {},
        "rosters": snapshot.get("rosters") or [],
        "users": snapshot.get("users") or [],
        "matchups": snapshot.get("matchups") or {},
        "winners_bracket": snapshot.get("winners_bracket") or [],
    }


def record_ingestion(
    conn,
    *,
    provider: str,
    dataset: str,
    league_id: str,
    season: str,
    week: int | None,
    payload: object,
    artifact_dir: Path,
    source_updated_at: str | None = None,
) -> dict:
    artifact = write_content_artifact(payload, artifact_dir)
    fetched_at = utc_now()
    existing = conn.execute(
        """
        SELECT * FROM ingestion_runs
        WHERE provider = ? AND dataset = ? AND league_id = ? AND content_hash = ?
        ORDER BY id DESC LIMIT 1
        """,
        (provider, dataset, league_id, artifact["content_hash"]),
    ).fetchone()
    if existing:
        return {**dict(existing), "deduplicated": True}
    cursor = conn.execute(
        """
        INSERT INTO ingestion_runs
          (provider, dataset, league_id, season, week, fetched_at, source_updated_at, status,
           content_hash, artifact_path, byte_count, schema_version, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'succeeded', ?, ?, ?, ?, '')
        """,
        (
            provider,
            dataset,
            league_id,
            season,
            week,
            fetched_at,
            source_updated_at,
            artifact["content_hash"],
            artifact["artifact_path"],
            artifact["byte_count"],
            SCHEMA_VERSION,
        ),
    )
    row = conn.execute("SELECT * FROM ingestion_runs WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return {**dict(row), "deduplicated": False, "compressed_bytes": artifact["compressed_bytes"]}


def record_ingestion_error(
    conn,
    *,
    provider: str,
    dataset: str,
    league_id: str,
    season: str = "",
    week: int | None = None,
    error: str,
) -> dict:
    cursor = conn.execute(
        """
        INSERT INTO ingestion_runs
          (provider, dataset, league_id, season, week, fetched_at, status, content_hash,
           artifact_path, byte_count, schema_version, error)
        VALUES (?, ?, ?, ?, ?, ?, 'failed', '', '', 0, ?, ?)
        """,
        (provider, dataset, league_id, season, week, utc_now(), SCHEMA_VERSION, error[:500]),
    )
    return dict(conn.execute("SELECT * FROM ingestion_runs WHERE id = ?", (cursor.lastrowid,)).fetchone())


def prune_artifacts(conn, artifact_dir: Path, retention_days: int = 14) -> dict:
    cutoff = datetime.now(timezone.utc).timestamp() - retention_days * 86400
    referenced = {
        str(row["artifact_path"])
        for row in conn.execute(
            """
            SELECT DISTINCT ir.artifact_path
            FROM ingestion_runs ir
            LEFT JOIN model_run_inputs mri ON mri.ingestion_run_id = ir.id
            LEFT JOIN model_runs mr ON mr.id = mri.model_run_id
            WHERE mr.published_at IS NOT NULL OR strftime('%s', ir.fetched_at) >= ?
            """,
            (int(cutoff),),
        ).fetchall()
        if row["artifact_path"]
    }
    removed = 0
    freed = 0
    if artifact_dir.exists():
        for path in artifact_dir.glob("*.json.gz"):
            if str(path) in referenced:
                continue
            freed += path.stat().st_size
            path.unlink(missing_ok=True)
            removed += 1
    return {"removed": removed, "freed_bytes": freed}
