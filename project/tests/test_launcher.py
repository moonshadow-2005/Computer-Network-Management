from pathlib import Path
import os
import sqlite3

import dotenv
import uvicorn

import run


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
