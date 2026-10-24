import gc
import os
import sqlite3
from pathlib import Path

import pytest

from qobuz_dl2 import db
from qobuz_dl2.results import Artifact
from qobuz_dl2.storage import stamp_provenance


def test_legacy_ids_are_preserved_but_not_trusted(tmp_path):
    path = tmp_path / "history.db"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE downloads(id TEXT UNIQUE NOT NULL)")
        c.execute("INSERT INTO downloads VALUES (?)", ("old",))
    db.create_db(str(path))
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT id FROM downloads").fetchall() == [("old",)]
        assert c.execute("SELECT name FROM sqlite_master WHERE name='artifacts_v1'").fetchone()
    assert list(tmp_path.glob("*.pre-artifacts-v1.bak"))


def test_valid_artifacts_are_destination_quality_and_identity_scoped(tmp_path, audio_bytes):
    assert hasattr(db, "record_artifacts"), "Artifact-backed history missing"
    path = tmp_path / "history.db"
    db.create_db(str(path))
    audio = tmp_path / "one" / "track.mp3"
    audio.parent.mkdir()
    audio.write_bytes(audio_bytes)
    stamp_provenance(str(audio), True, "track-1", "key")
    artifact = Artifact(str(audio), "track", "track-1", "track-1", "key", 5)
    db.record_artifacts(str(path), (artifact,))
    assert db.find_valid_artifacts(str(path), "track", "track-1", str(audio.parent), 5, "key") == (artifact,)
    assert not db.find_valid_artifacts(str(path), "track", "track-1", str(tmp_path / "two"), 5, "key")
    assert not db.find_valid_artifacts(str(path), "track", "track-1", str(audio.parent), 27, "key")
    assert not db.find_valid_artifacts(str(path), "album", "track-1", str(audio.parent), 5, "key")
    audio.unlink()
    assert not db.find_valid_artifacts(str(path), "track", "track-1", str(audio.parent), 5, "key")


@pytest.mark.skipif(not Path("/dev/fd").exists(), reason="Unix descriptor inventory")
def test_connection_closed_without_gc(tmp_path):
    path = str(tmp_path / "db")
    db.create_db(path)
    gc.collect()
    gc.disable()
    try:
        before = len(os.listdir("/dev/fd"))
        for n in range(100):
            db.handle_download_id(path, str(n))
        assert len(os.listdir("/dev/fd")) <= before + 2
    finally:
        gc.enable()
        gc.collect()


def test_database_errors_are_not_silenced(tmp_path):
    target = tmp_path / "not-db"
    target.write_bytes(b"not sqlite")
    with pytest.raises(sqlite3.DatabaseError):
        db.create_db(str(target))
