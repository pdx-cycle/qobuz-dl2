"""Additive artifact history. Legacy IDs remain evidence, never file-existence proof."""

import os
import sqlite3
from contextlib import closing

from qobuz_dl2.results import Artifact
from qobuz_dl2.storage import StagedArtifact, file_digest, read_provenance, validate_audio

DB_TIMEOUT = 5.0


def create_db(db_path):
    existing = os.path.isfile(db_path)
    with closing(sqlite3.connect(db_path, timeout=DB_TIMEOUT)) as conn:
        migrated = conn.execute("SELECT name FROM sqlite_master WHERE name='artifacts_v1'").fetchone()
        if existing and not migrated:
            backup = str(db_path) + ".pre-artifacts-v1.bak"
            if not os.path.exists(backup):
                with StagedArtifact(backup) as stage:
                    with closing(sqlite3.connect(stage.path)) as destination:
                        conn.backup(destination)
                    try:
                        stage.publish()
                    except FileExistsError:
                        pass  # another migration preserved the first rollback copy
        with conn:
            conn.execute("CREATE TABLE IF NOT EXISTS downloads (id TEXT UNIQUE NOT NULL)")
            conn.execute("""CREATE TABLE IF NOT EXISTS artifacts_v1 (
                item_type TEXT NOT NULL, item_id TEXT NOT NULL, source_track_id TEXT NOT NULL,
                path TEXT NOT NULL, requirements_key TEXT NOT NULL, format_id INTEGER NOT NULL,
                bit_depth INTEGER, sampling_rate REAL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(item_type,item_id,source_track_id,path,requirements_key))""")
    return db_path


def handle_download_id(db_path, item_id, add_id=False):
    """Legacy API retained for callers; new downloads use verified artifact records."""
    if not db_path:
        return None
    with closing(sqlite3.connect(db_path, timeout=DB_TIMEOUT)) as conn:
        with conn:
            if add_id:
                conn.execute("INSERT OR IGNORE INTO downloads(id) VALUES (?)", (str(item_id),))
                return None
            return conn.execute("SELECT id FROM downloads WHERE id=?", (str(item_id),)).fetchone()


def record_artifacts(db_path, artifacts):
    if not db_path:
        return
    rows = []
    for a in artifacts:
        validate_audio(a.path, a.format_id == 5)
        if read_provenance(a.path, a.format_id == 5) != (a.source_track_id, a.requirements_key):
            raise ValueError("Artifact provenance does not match completion record")
        rows.append(
            (
                a.item_type,
                a.item_id,
                a.source_track_id,
                os.path.realpath(a.path),
                a.requirements_key,
                a.format_id,
                a.bit_depth,
                a.sampling_rate,
                os.path.getsize(a.path),
                file_digest(a.path),
            )
        )
    with closing(sqlite3.connect(db_path, timeout=DB_TIMEOUT)) as conn:
        with conn:
            conn.executemany(
                """INSERT OR REPLACE INTO artifacts_v1
                (item_type,item_id,source_track_id,path,requirements_key,format_id,bit_depth,sampling_rate,size,sha256)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )


def quality_satisfies(format_id, bit_depth, sampling_rate, requested):
    if requested == 5:
        return format_id == 5
    if format_id == 5:
        return False
    if requested == 6:
        return (bit_depth or 0) >= 16 and (sampling_rate or 0) >= 44.1
    if requested == 7:
        return (bit_depth or 0) >= 24
    return requested == 27 and (bit_depth or 0) >= 24 and (sampling_rate or 0) > 96


def find_valid_artifacts(db_path, item_type, item_id, destination, requested_quality, requirements_key):
    if not db_path:
        return ()
    root = os.path.realpath(destination)
    with closing(sqlite3.connect(db_path, timeout=DB_TIMEOUT)) as conn:
        rows = conn.execute(
            """SELECT path,source_track_id,format_id,bit_depth,sampling_rate,size,sha256
             FROM artifacts_v1 WHERE item_type=? AND item_id=? AND requirements_key=? ORDER BY path""",
            (item_type, str(item_id), requirements_key),
        ).fetchall()
    valid = []
    for path, source_id, fmt, depth, rate, size, digest in rows:
        try:
            if os.path.commonpath([root, path]) != root or not quality_satisfies(fmt, depth, rate, requested_quality):
                continue
            if os.path.getsize(path) != size or file_digest(path) != digest:
                continue
            validate_audio(path, fmt == 5)
            if read_provenance(path, fmt == 5) != (source_id, requirements_key):
                continue
            valid.append(Artifact(path, item_type, str(item_id), source_id, requirements_key, fmt, depth, rate))
        except (OSError, ValueError):
            continue
    return tuple(valid)


def expected_artifact_digest(db_path, path, source_track_id, requirements_key):
    if not db_path:
        return None
    with closing(sqlite3.connect(db_path, timeout=DB_TIMEOUT)) as conn:
        row = conn.execute(
            """SELECT sha256 FROM artifacts_v1
             WHERE path=? AND source_track_id=? AND requirements_key=? ORDER BY completed_at DESC LIMIT 1""",
            (os.path.realpath(path), str(source_track_id), requirements_key),
        ).fetchone()
    return row[0] if row else None
