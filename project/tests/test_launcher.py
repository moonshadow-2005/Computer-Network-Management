from pathlib import Path
import os
import sqlite3

import dotenv
import pytest
import uvicorn

import run
from app import demo
from app.core.db import CURRENT_SCHEMA_VERSION


def test_demo_launch_isolates_inherited_live_config_and_loads_idempotently(tmp_path, monkeypatch):
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    for name in ("NMS_ENABLE_LIVE", "NMS_ENABLE_SERVICE_PROBE", "NMS_ENABLE_IPERF3"):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("NMS_POLL_INTERVAL", "30")
    monkeypatch.setenv("NMS_API_TOKEN", "local-test-token")
    live_db = tmp_path / "live.sqlite3"
    monkeypatch.setenv("NMS_DB", str(live_db))
    sample_db = tmp_path / "sample.sqlite3"

    assert run.main(["--db", str(sample_db), "--port", "8123"]) == 0
    assert run.main(["--db", str(sample_db), "--port", "8123"]) == 0

    assert os.environ["NMS_DB"] == str(sample_db.resolve())
    assert os.environ["NMS_API_TOKEN"] == ""
    assert all(os.environ[name] == "0" for name in (
        "NMS_ENABLE_LIVE", "NMS_POLL_INTERVAL", "NMS_ENABLE_SERVICE_PROBE", "NMS_ENABLE_IPERF3"))
    assert not live_db.exists()
    with sqlite3.connect(sample_db) as db:
        assert db.execute("SELECT COUNT(*) FROM devices WHERE data_source='sample'").fetchone()[0] == 3
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert len(calls) == 2
    assert calls[0] == (("app.main:app",), {"host": "127.0.0.1", "port": 8123, "workers": 1})


def test_live_launch_does_not_seed_sample_or_enable_network(tmp_path, monkeypatch):
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)
    db_path = tmp_path / "experiment.sqlite3"
    monkeypatch.setenv("NMS_DB", str(db_path))
    monkeypatch.setenv("NMS_ENABLE_LIVE", "0")

    assert run.main(["--mode", "live"]) == 0

    assert not db_path.exists()
    assert os.environ["NMS_ENABLE_LIVE"] == "0"
    assert Path(os.environ["NMS_DB"]) == db_path


def test_default_demo_leaves_legacy_database_intact_when_chmod_is_ineffective(tmp_path, monkeypatch):
    legacy_db = tmp_path / "data" / "demo.sqlite3"
    legacy_db.parent.mkdir()
    with sqlite3.connect(legacy_db) as db:
        db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO metadata VALUES ('preserve', 'legacy contents')")
        db.execute("PRAGMA user_version=1")
    legacy_before = legacy_db.read_bytes()
    sample_db = legacy_db.with_name(f"demo-v{CURRENT_SCHEMA_VERSION}.sqlite3")
    monkeypatch.setattr(run, "ROOT", tmp_path)
    monkeypatch.setattr(demo, "DEMO_DB", sample_db)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)
    # Simulate DrvFS without Unix metadata: chmod does not restrict access.
    monkeypatch.setattr(Path, "chmod", lambda *args, **kwargs: None)

    assert run.main([]) == 0
    assert run.main([]) == 0

    assert legacy_db.read_bytes() == legacy_before
    assert not (legacy_db.parent / "backups").exists()
    assert os.environ["NMS_DB"] == str(sample_db.resolve())
    with sqlite3.connect(sample_db) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert db.execute("SELECT COUNT(*) FROM devices WHERE data_source='sample'").fetchone()[0] == 3
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("name", ["demo.sqlite3", f"demo-v{CURRENT_SCHEMA_VERSION}.sqlite3",
                                 f"local-test-v{CURRENT_SCHEMA_VERSION}.sqlite3"])
def test_live_rejects_default_sample_database_paths(tmp_path, monkeypatch, name):
    monkeypatch.setattr(run, "ROOT", tmp_path)
    monkeypatch.setattr(demo, "DEMO_DB", tmp_path / "data" / f"demo-v{CURRENT_SCHEMA_VERSION}.sqlite3")
    monkeypatch.setattr(demo, "LOCAL_TEST_DB", tmp_path / "data" / f"local-test-v{CURRENT_SCHEMA_VERSION}.sqlite3")
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)

    with pytest.raises(SystemExit) as error:
        run.main(["--mode", "live", "--db", f"data/{name}"])

    assert error.value.code == 2
    assert not (tmp_path / "data").exists()


def test_local_mode_requires_a_token_before_creating_sample_data(tmp_path, monkeypatch):
    sample_db = tmp_path / "local.sqlite3"
    monkeypatch.setattr(demo, "LOCAL_TEST_DB", sample_db)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setenv("NMS_API_TOKEN", "")

    with pytest.raises(SystemExit) as error:
        run.main(["--mode", "local"])

    assert error.value.code == 2
    assert not sample_db.exists()


def test_local_mode_preserves_test_edits_and_disables_inherited_network_actions(tmp_path, monkeypatch):
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)
    sample_db = tmp_path / "local.sqlite3"
    monkeypatch.setattr(demo, "LOCAL_TEST_DB", sample_db)
    live_db = tmp_path / "live.sqlite3"
    live_db.write_bytes(b"preserve existing experiment data")
    monkeypatch.setenv("NMS_DB", str(live_db))
    monkeypatch.setenv("NMS_API_TOKEN", "local-test-only")
    for name in ("NMS_ENABLE_LIVE", "NMS_ENABLE_SERVICE_PROBE", "NMS_ENABLE_IPERF3"):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("NMS_POLL_INTERVAL", "30")

    assert run.main(["--mode", "local"]) == 0
    with sqlite3.connect(sample_db) as db:
        db.execute("UPDATE events SET acknowledged_at='2026-10-15T12:00:00Z' WHERE event_id=1")
    assert run.main(["--mode", "local"]) == 0

    assert os.environ["NMS_API_TOKEN"] == "local-test-only"
    assert os.environ["NMS_DB"] == str(sample_db.resolve())
    assert live_db.read_bytes() == b"preserve existing experiment data"
    assert all(os.environ[name] == "0" for name in (
        "NMS_ENABLE_LIVE", "NMS_POLL_INTERVAL", "NMS_ENABLE_SERVICE_PROBE", "NMS_ENABLE_IPERF3"))
    with sqlite3.connect(sample_db) as db:
        assert db.execute("SELECT acknowledged_at,state FROM events WHERE event_id=1").fetchone() == (
            "2026-10-15T12:00:00Z", "recovered")
        assert db.execute("SELECT COUNT(*) FROM devices WHERE data_source='sample'").fetchone()[0] == 3
