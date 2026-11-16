import asyncio
import ipaddress
import socket
import struct
from dataclasses import replace

import pytest
from httpx import ASGITransport, AsyncClient

from app.collectors.scan_sensor import parse_syn
from app.core.config import Settings
from app.core.db import Database, decode
from app.main import create_app
from app.modules.scan_detection import ScanDetector, ScanPolicy, scan_status, stamp


BASE = 1_800_000_000  # aligned to both minute and 15-minute windows
TARGET = "192.168.0.100"
SOURCE = "198.51.100.20"


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "scan.sqlite3")
    db.initialize()
    return db


def detector(database, **changes):
    return ScanDetector(database, replace(ScanPolicy(targets=(TARGET,), internal_networks=("192.168.0.0/16",)), **changes), BASE)


def sweep(sensor, at, ports=range(10000, 10008), source=SOURCE, target=TARGET):
    for port in ports:
        sensor.observe(source, target, port, at)
    sensor.flush(at)


def tick(sensor, start, end):
    for now in range(start, end + 1, 5):
        sensor.flush(now)


def risk(database):
    row = database.row("SELECT * FROM risks WHERE rule_id LIKE 'tcp_port_scan_%' ORDER BY risk_id DESC LIMIT 1")
    if row:
        row["evidence"] = decode(row["evidence"])
    return row


def packet(source=SOURCE, target=TARGET, port=443, flags=2, ipv6=False):
    tcp = struct.pack("!HHIIBBHHH", 42000, port, 123, 0, 5 << 4, flags, 1024, 0, 0)
    if ipv6:
        return struct.pack("!IHBB16s16s", 6 << 28, len(tcp), 6, 64,
                           ipaddress.ip_address(source).packed, ipaddress.ip_address(target).packed) + tcp
    return struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(tcp), 1, 0, 64, 6, 0,
                       socket.inet_aton(source), socket.inet_aton(target)) + tcp


def test_parses_ipv4_ipv6_syn_and_extension_headers_without_payload():
    assert parse_syn(packet(), 0x0800) == (SOURCE, TARGET, 443)
    v6 = packet("2001:db8::20", "2001:db8::100", ipv6=True)
    assert parse_syn(v6, 0x86dd) == ("2001:db8::20", "2001:db8::100", 443)
    # Destination options header before TCP.
    extended = bytearray(v6[:40] + bytes([6, 0]) + b"\0" * 6 + v6[40:])
    extended[6] = 60
    extended[4:6] = struct.pack("!H", 28)
    assert parse_syn(bytes(extended), 0x86dd) == ("2001:db8::20", "2001:db8::100", 443)
    for flags in (0, 0x10, 0x12, 0x14, 0x03, 0x06):
        assert parse_syn(packet(flags=flags), 0x0800) is None
    assert parse_syn(packet(port=0), 0x0800) is None
    assert parse_syn(packet()[:35], 0x0800) is None
    fragment = bytearray(packet()); fragment[6:8] = b"\x20\x00"
    assert parse_syn(bytes(fragment), 0x0800) is None
    fragment6 = bytearray(v6); fragment6[6] = 44
    assert parse_syn(bytes(fragment6), 0x86dd) is None
    malformed = bytearray(packet()); malformed[32] = 0xF0
    assert parse_syn(bytes(malformed), 0x0800) is None
    for length in range(40):
        assert parse_syn(b"\0" * length, 0x0800) is None


def test_normal_connections_and_retransmissions_do_not_trigger(database):
    sensor = detector(database)
    sweep(sensor, BASE + 1, ports=[22] * 100)
    assert risk(database) is None
    rows = database.rows("SELECT * FROM scan_windows")
    assert len(rows) == 2 and all(x["port_count"] == 1 and x["syn_packets"] == 100 for x in rows)
    sweep(sensor, BASE + 2, ports=range(10), target="192.168.0.2")
    assert risk(database) is None  # only configured destination addresses


def test_fast_detection_keeps_evidence_scope_and_acknowledgement(database):
    sensor = detector(database)
    sweep(sensor, BASE + 1)
    first = risk(database)
    assert first["state"] == "open" and first["data_source"] == "live"
    evidence = first["evidence"]
    assert evidence["source_ip"] == SOURCE and evidence["target_ip"] == TARGET
    assert evidence["source_scope"] == "external" and evidence["distinct_ports"] == 8
    assert not evidence["source_identity_confirmed"] and not evidence["vulnerability_confirmed"]
    with database.connect() as db:
        db.execute("UPDATE risks SET state='acknowledged' WHERE risk_id=?", (first["risk_id"],))
    sweep(sensor, BASE + 2, ports=[10008])
    assert risk(database)["risk_id"] == first["risk_id"]
    assert risk(database)["state"] == "acknowledged"
    assert risk(database)["evidence"]["distinct_ports"] == 9


def test_continuous_fast_windows_and_slow_distributed_in_time(database):
    sensor = detector(database)
    sweep(sensor, BASE + 1)
    tick(sensor, BASE + 5, BASE + 60)
    sweep(sensor, BASE + 61)
    tick(sensor, BASE + 65, BASE + 120)
    sweep(sensor, BASE + 121)
    assert risk(database)["evidence"]["consecutive_fast_windows"] == 3
    assert risk(database)["evidence"]["sustained"]
    # 7 ports per minute: below the fast threshold, 21 across 3 minutes triggers slow rule.
    slow_source = "198.51.100.21"
    slow = detector(database, sensor_id="slow-test")
    for minute in range(3):
        tick(slow, BASE + minute * 60, BASE + minute * 60)
        sweep(slow, BASE + minute * 60 + 1, range(20000 + minute * 7, 20007 + minute * 7), source=slow_source)
        tick(slow, BASE + minute * 60 + 5, BASE + minute * 60 + 55)
    assert risk(database)["evidence"]["window_seconds"] == 900
    assert risk(database)["evidence"]["active_minutes"] == 3
    assert risk(database)["evidence"]["sustained"]


def test_windows_do_not_combine_targets_or_sources_and_expire(database):
    sensor = detector(database, targets=(TARGET, "192.168.0.101"))
    sweep(sensor, BASE + 1, range(4), target=TARGET)
    sweep(sensor, BASE + 1, range(4, 8), target="192.168.0.101")
    sweep(sensor, BASE + 1, range(4, 8), source="198.51.100.21")
    assert risk(database) is None
    tick(sensor, BASE + 5, BASE + 60)
    sweep(sensor, BASE + 61, range(4, 8))
    assert risk(database) is None
    assert all(window.start + window.seconds > BASE + 61 for window in sensor.windows.values())


def test_quiet_recovery_requires_healthy_observation_and_new_sensor_warmup(database):
    sensor = detector(database)
    sweep(sensor, BASE + 1)
    tick(sensor, BASE + 5, BASE + 95)
    sensor.gap(BASE + 100, kernel_drops=2)
    tick(sensor, BASE + 100, BASE + 300)
    assert risk(database)["state"] == "open"
    tick(sensor, BASE + 305, BASE + 405)
    assert risk(database)["state"] == "resolved"
    assert "未再命中" in risk(database)["evidence"]["resolution"]
    sweep(sensor, BASE + 410)
    restarted = ScanDetector(database, sensor.policy, BASE + 411)
    tick(restarted, BASE + 415, BASE + 705)
    assert risk(database)["state"] == "open"
    tick(restarted, BASE + 710, BASE + 715)
    assert risk(database)["state"] == "resolved"


def test_clock_gap_or_stop_never_means_recovery_and_status_goes_stale(database):
    sensor = detector(database)
    sweep(sensor, BASE + 1)
    sensor.flush(BASE + 600)
    assert risk(database)["state"] == "open"
    assert scan_status(database, BASE + 601)["items"][0]["effective_state"] == "degraded"
    sensor.flush(BASE + 605, state="stopped")
    assert risk(database)["state"] == "open"
    assert scan_status(database, BASE + 606)["items"][0]["effective_state"] == "stopped"
    assert scan_status(database, BASE + 640)["items"][0]["effective_state"] == "stale"


def test_capacity_is_bounded_and_unknown_scope_is_not_called_external(database):
    sensor = detector(database, internal_networks=(), max_groups=2)
    sweep(sensor, BASE + 1)
    assert risk(database)["evidence"]["source_scope"] == "unknown"
    sweep(sensor, BASE + 2, source="198.51.100.21")
    assert len(sensor.windows) == 2 and sensor.counters["capacity_drops"] > 0
    assert scan_status(database, BASE + 2)["items"][0]["effective_state"] == "degraded"
    assert sensor.classify("127.0.0.1") == "internal"


def test_busy_writer_defers_persistence_without_losing_windows_or_restarting(database):
    sensor = detector(database)
    for port in range(10000, 10008):
        sensor.observe(SOURCE, TARGET, port, BASE + 1)
    session = sensor.session
    with database.connect() as writer:
        writer.execute("BEGIN IMMEDIATE")
        assert sensor.flush(BASE + 2) is False
        sensor.observe(SOURCE, TARGET, 10008, BASE + 3)
        assert sensor.flush(BASE + 4) is False
        assert all(window.dirty for window in sensor.windows.values())
        assert risk(database) is None
    assert sensor.flush(BASE + 5) is True
    assert sensor.session == session and sensor.counters["persistence_retries"] == 2
    assert sensor.counters["syn_packets"] == 9
    assert risk(database)["evidence"]["distinct_ports"] == 9
    assert scan_status(database, BASE + 6)["items"][0]["effective_state"] == "running"


def test_v3_migration_preserves_existing_risks_and_creates_scan_tables(database):
    with database.connect() as db:
        db.execute("INSERT INTO risks(device_id,rule_id,level,state,first_seen,last_seen,evidence,data_source) VALUES ('dev','old','warning','open','a','a','{}','live')")
        db.execute("DROP TABLE scan_windows")
        db.execute("DROP TABLE scan_sensors")
        db.execute("PRAGMA user_version=3")
    database.initialize()
    assert database.row("PRAGMA user_version")["user_version"] == 4
    assert database.row("SELECT rule_id FROM risks")["rule_id"] == "old"
    assert database.rows("SELECT * FROM scan_sensors") == []
    assert list((database.path.parent / "backups").glob("*.sqlite3"))


def test_scan_api_filters_sources_keeps_sample_label_and_acknowledges(database, monkeypatch):
    monkeypatch.setenv("NMS_API_TOKEN", "scan-test-admin-token")
    async def verify():
        app = create_app(replace(Settings(), db_path=database.path))
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                assert (await client.get("/api/v1/security/scan-status")).json()["data"]["items"] == []
                sensor = ScanDetector(database, ScanPolicy(sensor_id="sample-only", targets=(TARGET,)), BASE, data_source="sample")
                sweep(sensor, BASE + 1)
                result = (await client.get("/api/v1/security/scan-windows?matched=true")).json()
                assert result["data_source"] == "sample" and result["data"]["total"] == 1
                assert result["data"]["items"][0]["ports"] == list(range(10000, 10008))
                assert (await client.get("/api/v1/security/scan-windows?source_scope=external")).json()["data"]["total"] == 0
                assert (await client.get("/api/v1/security/scan-windows?source_scope=bad")).status_code == 422
                assert (await client.get("/api/v1/security/scan-windows?limit=501")).status_code == 422
                risk_id = risk(database)["risk_id"]
                assert (await client.post(f"/api/v1/risks/{risk_id}/acknowledge")).status_code == 401
                result = await client.post(f"/api/v1/risks/{risk_id}/acknowledge", headers={"X-Admin-Token":"scan-test-admin-token"})
                assert result.json()["data"]["state"] == "acknowledged"
                detail = (await client.get(f"/api/v1/risks/{risk_id}")).json()["data"]
                assert detail["data_source"] == "sample" and detail["evidence"]["source_ip"] == SOURCE
    asyncio.run(verify())
