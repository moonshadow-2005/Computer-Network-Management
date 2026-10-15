from __future__ import annotations

import asyncio
import csv
import io
import os
import sqlite3
import subprocess
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.collectors.snmp import (SnmpClient, SnmpEndOfMib, SnmpError, SnmpValue,
                                _decode_oid, _expect, _integer, _oid, _read,
                                _response, _tlv, parse_oid)
from app.core.config import Settings
from app.core.db import CURRENT_SCHEMA_VERSION, Database, SCHEMA, decode
from app.main import create_app
from app.modules.fault_config import diff_snapshots, record_interface_status, record_ping, save_snapshot
from app.modules.iperf_tasks import IperfTaskManager, TaskCancelled, TaskConflict
from app.modules.perf_accounting import record_counter, traffic_report
from app.modules.security import check_tcp, record_service_check, record_utilization_risk
from app.service import (CollectorService, IF_ADMIN, IF_DESCR, IF_DISCONTINUITY,
                         IF_HIGH_SPEED, IF_IN, IF_INDEX, IF_OPER, IF_OUT, IF_SPEED,
                         ROUTE_DEST, ROUTE_IF_INDEX, ROUTE_MASK, ROUTE_NEXT_HOP,
                         SYS_UPTIME)


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "nms.sqlite3")
    db.initialize()
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO interfaces(interface_id,device_id,if_index,query_status,data_source)
               VALUES ('r-0-1:2','r-0-1',2,'ok','live')"""
        )
    return db


def test_uint64_rate_reset_gap_and_traffic_window(database):
    start = "2026-10-13T10:00:00Z"
    mid = "2026-10-13T10:00:10Z"
    end = "2026-10-13T10:00:20Z"
    high = 2**53 + 1000
    first = record_counter(database, "r-0-1:2", high, high, start, 1000)
    assert not first["valid"] and first["invalid_reason"] == "first_or_previous_invalid_sample"
    second = record_counter(database, "r-0-1:2", high + 125, high + 250, mid, 2000)
    assert second["valid"] and second["in_rate_bps"] == 100
    third = record_counter(database, "r-0-1:2", high + 225, high + 450, end, 3000)
    assert third["valid"]
    report = traffic_report(database, "r-0-1:2", start, end)
    assert report["in_bytes"] == "225" and report["out_bytes"] == "450"
    assert report["coverage_pct"] == 100 and report["complete"]
    reset = record_counter(database, "r-0-1:2", 1, 1, "2026-10-13T10:00:30Z", 100)
    assert not reset["valid"] and reset["invalid_reason"] == "agent_restarted"
    after = traffic_report(database, "r-0-1:2", "2026-10-13T10:00:30Z", "2026-10-13T10:00:40Z")
    assert after["in_bytes"] is None and after["coverage_pct"] == 0


def test_traffic_window_ignores_invalid_interval_ending_at_window_start(database):
    start = "2026-10-13T10:00:00Z"
    end = "2026-10-13T10:00:10Z"
    record_counter(database, "r-0-1:2", 100, 200, "2026-10-13T09:55:00Z", 100)
    boundary = record_counter(database, "r-0-1:2", 110, 220, start, 200)
    assert not boundary["valid"] and boundary["invalid_reason"] == "sample_gap"
    valid = record_counter(database, "r-0-1:2", 130, 260, end, 300)
    assert valid["valid"]

    report = traffic_report(database, "r-0-1:2", start, end)
    assert report["in_bytes"] == "20" and report["out_bytes"] == "40"
    assert report["coverage_pct"] == 100 and report["complete"]
    assert report["invalid_intervals"] == []


def test_ping_fault_dedup_unknown_and_recovery(database):
    failed = {"query_status": "ok", "packets_sent": 2, "packets_received": 0,
              "rtt_ms": None, "loss_pct": 100.0, "error": None}
    unknown = {**failed, "query_status": "timeout", "packets_sent": None,
               "packets_received": None, "loss_pct": None, "error": "timeout"}
    healthy = {**failed, "packets_received": 2, "rtt_ms": 1.2, "loss_pct": 0.0}
    for n in range(3):
        result = record_ping(database, "host-1", "192.168.1.1", f"2026-10-13T10:00:0{n}Z", failed)
    event_id = result["active_event_id"]
    assert event_id and database.row("SELECT COUNT(*) AS n FROM events")["n"] == 1
    record_ping(database, "host-1", "192.168.1.1", "2026-10-13T10:00:04Z", failed)
    state = record_ping(database, "host-1", "192.168.1.1", "2026-10-13T10:00:05Z", unknown)
    assert state["active_event_id"] == event_id
    state = record_ping(database, "host-1", "192.168.1.1", "2026-10-13T10:00:06Z", healthy)
    assert state["active_event_id"] is None
    assert database.row("SELECT state,recovered_at FROM events WHERE event_id=?", (event_id,))["state"] == "recovered"


def test_fault_event_acknowledgement_is_audited_separately_from_recovery(database, monkeypatch):
    monkeypatch.setenv("NMS_API_TOKEN", "test-token")
    failed = {"query_status": "ok", "packets_sent": 2, "packets_received": 0,
              "rtt_ms": None, "loss_pct": 100.0, "error": None}
    healthy = {**failed, "packets_received": 2, "rtt_ms": 1.0, "loss_pct": 0.0}
    for n in range(3):
        result = record_ping(database, "host-1", "192.168.1.1", f"2026-10-13T10:00:0{n}Z", failed)
    event_id = result["active_event_id"]
    settings = replace(Settings(), db_path=database.path)

    async def verify():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                path = f"/api/v1/events/{event_id}/acknowledge"
                listed = (await client.get("/api/v1/events", params={"device_id":"host-1"})).json()["data"]
                before = next(item for item in listed["items"] if item["event_id"] == event_id)
                assert before["state"] == "active" and before["acknowledged_at"] is None
                assert (await client.post(path)).status_code == 401

                acknowledged = await client.post(path, headers={"X-Admin-Token":"test-token"})
                assert acknowledged.status_code == 200
                first_ack = acknowledged.json()["data"]["acknowledged_at"]
                assert first_ack and acknowledged.json()["data"]["state"] == "active"
                repeated = await client.post(path, headers={"X-Admin-Token":"test-token"})
                assert repeated.json()["data"]["acknowledged_at"] == first_ack
                assert (await client.post("/api/v1/events/999999/acknowledge",
                                          headers={"X-Admin-Token":"test-token"})).status_code == 404

                recovered = {**healthy}
                record_ping(database, "host-1", "192.168.1.1", "2026-10-13T10:00:04Z", recovered)
                after = (await client.get("/api/v1/events", params={"device_id":"host-1"})).json()["data"]
                event = next(item for item in after["items"] if item["event_id"] == event_id)
                assert event["state"] == "recovered" and event["recovered_at"]
                assert event["acknowledged_at"] == first_ack
    asyncio.run(verify())


def test_interface_fault_needs_evidence_and_recovers(database):
    interface = "r-0-1:2"
    for n in range(3):
        result = record_interface_status(database, interface, "r-0-1", f"2026-10-13T10:00:0{n}Z",
                                         "up", "down", "ok")
    event_id = result["active_event_id"]
    assert event_id and database.row("SELECT COUNT(*) AS n FROM events")["n"] == 1
    record_interface_status(database, interface, "r-0-1", "2026-10-13T10:00:03Z",
                            None, None, "error")
    assert database.row("SELECT state FROM events WHERE event_id=?", (event_id,))["state"] == "active"
    record_interface_status(database, interface, "r-0-1", "2026-10-13T10:00:04Z",
                            "up", "up", "ok")
    assert database.row("SELECT state FROM events WHERE event_id=?", (event_id,))["state"] == "recovered"


def test_snapshot_diff_and_service_risk(database):
    a = save_snapshot(database, "r-0-1", "2026-10-13T10:00:00Z",
                      [{"interface_id": "r-0-1:2", "description": "eth0", "admin_status": "up"}], [])
    same = save_snapshot(database, "r-0-1", "2026-10-13T10:00:10Z",
                         [{"interface_id": "r-0-1:2", "description": "eth0", "admin_status": "up"}], [])
    assert same == a
    b = save_snapshot(database, "r-0-1", "2026-10-13T10:00:20Z",
                      [{"interface_id": "r-0-1:2", "description": "eth0", "admin_status": "down"}], [])
    assert diff_snapshots(database, a, b)["changes"][0]["change"] == "changed"
    with database.connect() as conn:
        conn.execute("INSERT INTO service_baselines(device_id,port,allowed) VALUES ('host-1',5201,0)")
    x = record_service_check(database, "host-1", 5201, "open")
    y = record_service_check(database, "host-1", 5201, "open")
    assert x["risk_id"] == y["risk_id"]
    with database.connect() as conn:
        conn.execute("UPDATE risks SET state='acknowledged' WHERE risk_id=?", (x["risk_id"],))
    acknowledged = record_service_check(database, "host-1", 5201, "open")
    assert acknowledged["risk_id"] == x["risk_id"]
    record_service_check(database, "host-1", 5201, "unavailable")
    assert database.row("SELECT state FROM risks")["state"] == "acknowledged"
    record_service_check(database, "host-1", 5201, "closed")
    assert database.row("SELECT state FROM risks")["state"] == "resolved"


def test_risk_acknowledgement_api_requires_token_and_preserves_evidence(database, monkeypatch):
    monkeypatch.setenv("NMS_API_TOKEN", "test-token")
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO service_baselines(device_id,port,allowed) VALUES ('host-1',5201,0)"
        )
    check = record_service_check(
        database, "host-1", 5201, "open", data_source="sample",
        observed_at="2026-10-14T12:00:00Z",
    )
    risk_id = check["risk_id"]
    assert risk_id is not None
    settings = replace(Settings(), db_path=database.path)

    async def verify():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                path = f"/api/v1/risks/{risk_id}/acknowledge"
                before = (await client.get(
                    "/api/v1/risks", params={"device_id": "host-1", "state": "open"},
                )).json()["data"]["items"]
                assert len(before) == 1
                assert before[0]["risk_id"] == risk_id
                assert before[0]["evidence"]["check_id"] == check["check_id"]

                denied = await client.post(path)
                assert denied.status_code == 401
                still_open = (await client.get(
                    "/api/v1/risks", params={"device_id": "host-1", "state": "open"},
                )).json()["data"]["items"]
                assert still_open == before

                headers = {"X-Admin-Token": "test-token"}
                acknowledged = await client.post(path, headers=headers)
                assert acknowledged.status_code == 200
                first = acknowledged.json()["data"]
                assert first["state"] == "acknowledged"
                assert first["evidence"] == before[0]["evidence"]
                assert acknowledged.json()["data_source"] == "sample"

                repeated = await client.post(path, headers=headers)
                assert repeated.status_code == 200
                assert repeated.json()["data"] == first
                listed = (await client.get(
                    "/api/v1/risks", params={"device_id": "host-1", "state": "acknowledged"},
                )).json()["data"]["items"]
                assert listed == [first]

                missing = await client.post("/api/v1/risks/999999/acknowledge", headers=headers)
                assert missing.status_code == 404

    asyncio.run(verify())


def test_sustained_utilization_needs_valid_live_samples_and_recovers(database):
    interface = "r-0-1:2"
    counters = [0, 1_125_000, 2_250_000, 3_375_000, 3_475_000]
    observed = []
    for n, counter in enumerate(counters):
        metric = record_counter(database, interface, counter, counter,
                                f"2026-10-13T10:00:{n * 10:02d}Z", n * 1000,
                                reference_speed_bps=1_000_000)
        observed.append(record_utilization_risk(database, "r-0-1", interface, metric["metric_id"]))
    assert observed[:3] == [None, None, None]
    assert observed[3] is not None and observed[4] == observed[3]
    assert database.row("SELECT state FROM risks WHERE risk_id=?", (observed[3],))["state"] == "resolved"
    sample = record_counter(database, interface, counters[-1] + 1_125_000,
                            counters[-1] + 1_125_000, "2026-10-13T10:00:50Z", 5000,
                            data_source="sample", reference_speed_bps=1_000_000)
    assert record_utilization_risk(database, "r-0-1", interface, sample["metric_id"]) is None


@pytest.mark.parametrize("reference_speed_bps", [None, 0])
def test_unknown_reference_speed_keeps_rate_but_never_reports_utilization_risk(
    database, reference_speed_bps,
):
    interface = "r-0-1:2"
    record_counter(database, interface, 10_000, 20_000,
                   "2026-10-13T10:00:00Z", 1000,
                   reference_speed_bps=reference_speed_bps)
    metric = record_counter(database, interface, 1_135_000, 2_270_000,
                            "2026-10-13T10:00:10Z", 2000,
                            reference_speed_bps=reference_speed_bps)

    stored = database.row(
        "SELECT valid,in_rate_bps,out_rate_bps,in_util_pct,out_util_pct,invalid_reason "
        "FROM metric_samples WHERE metric_id=?", (metric["metric_id"],)
    )
    assert metric["valid"] and metric["in_rate_bps"] == 900_000
    assert metric["out_rate_bps"] == 1_800_000
    assert stored == {
        "valid": 1, "in_rate_bps": 900_000.0, "out_rate_bps": 1_800_000.0,
        "in_util_pct": None, "out_util_pct": None, "invalid_reason": None,
    }
    assert record_utilization_risk(database, "r-0-1", interface, metric["metric_id"]) is None
    assert database.row("SELECT COUNT(*) AS n FROM risks")["n"] == 0


def test_snmp_response_validation():
    oid = parse_oid("1.3.6.1.2.1.1.3.0")
    binding = _tlv(0x30, _oid(oid) + _tlv(0x43, b"\x01\x00"))
    pdu = _tlv(0xA2, _integer(234) + _integer(0) + _integer(0) + _tlv(0x30, binding))
    response = _tlv(0x30, _integer(1) + _tlv(0x04, b"test-only") + pdu)
    parsed = _response(response, 234, "test-only")
    assert parsed.oid == oid and parsed.value == 256
    with pytest.raises(SnmpError, match="request ID"):
        _response(response, 235, "test-only")
    with pytest.raises(SnmpError, match="community"):
        _response(response, 234, "wrong")


def test_snmp_getbulk_encodes_bounded_request_and_decodes_multiple_bindings(monkeypatch):
    client = SnmpClient("test-only")
    root = parse_oid("1.3.6.1.2.1.2.2.1.1")
    response_bindings = [
        (root + (1,), 0x02, b"\x01"),
        (root + (2,), 0x02, b"\x02"),
    ]

    def fake_exchange(address, packet, request_id):
        assert address == "192.0.2.1"
        message, end = _expect(packet, 0x30)
        assert end == len(packet)
        version_tag, version_raw, pos = _read(message)
        assert version_tag == 0x02 and int.from_bytes(version_raw, "big", signed=True) == 1
        community, pos = _expect(message, 0x04, pos)
        assert community == b"test-only"
        pdu, pos = _expect(message, 0xA5, pos)
        assert pos == len(message)
        rid_tag, rid_raw, pos = _read(pdu)
        non_repeaters_tag, non_repeaters_raw, pos = _read(pdu, pos)
        repetitions_tag, repetitions_raw, pos = _read(pdu, pos)
        varbinds, pos = _expect(pdu, 0x30, pos)
        assert pos == len(pdu)
        assert rid_tag == non_repeaters_tag == repetitions_tag == 0x02
        assert int.from_bytes(rid_raw, "big", signed=True) == request_id
        assert int.from_bytes(non_repeaters_raw, "big", signed=True) == 0
        assert int.from_bytes(repetitions_raw, "big", signed=True) == 2
        binding, end = _expect(varbinds, 0x30)
        assert end == len(varbinds)
        oid_tag, oid_raw, binding_pos = _read(binding)
        assert _decode_oid(_tlv(oid_tag, oid_raw)) == root
        null_tag, null_value, binding_pos = _read(binding, binding_pos)
        assert null_tag == 0x05 and null_value == b"" and binding_pos == len(binding)

        encoded = b"".join(
            _tlv(0x30, _oid(oid) + _tlv(tag, value))
            for oid, tag, value in response_bindings
        )
        response_pdu = _tlv(0xA2, _integer(request_id) + _integer(0) + _integer(0) +
                            _tlv(0x30, encoded))
        return _tlv(0x30, _integer(1) + _tlv(0x04, b"test-only") + response_pdu)

    monkeypatch.setattr(client, "_exchange", fake_exchange)
    values = client.get_bulk("192.0.2.1", root, max_repetitions=2)

    assert [(item.oid, item.value) for item in values] == [
        (root + (1,), 1), (root + (2,), 2),
    ]
    with pytest.raises(ValueError, match="between 1 and 16"):
        client.get_bulk("192.0.2.1", root, max_repetitions=17)


def test_snmp_walk_batches_in_order_stops_at_prefix_and_respects_limit(monkeypatch):
    client = SnmpClient("test-only")
    root = parse_oid("1.3.6.1.2.1.2.2.1.1")
    outside_prefix = root[:-1] + (root[-1] + 1,)
    calls = []

    def fake_bulk(_address, cursor, max_repetitions):
        calls.append((cursor, max_repetitions))
        if cursor == root:
            return [SnmpValue(root + (1,), 1, 0x02),
                    SnmpValue(root + (2,), 2, 0x02),
                    SnmpValue(root + (3,), 3, 0x02)]
        return [SnmpValue(outside_prefix, 4, 0x02)]

    def unexpected_next(*_args):
        raise AssertionError("GETNEXT should not run on a valid GETBULK walk")

    monkeypatch.setattr(client, "get_bulk", fake_bulk)
    monkeypatch.setattr(client, "get_next", unexpected_next)

    values = client.walk("192.0.2.1", ".".join(map(str, root)), 2)

    assert [value.oid for value in values] == [root + (1,), root + (2,)]
    assert calls == [(root, 2)]


def test_snmp_bulk_walk_stops_at_end_of_mib_view(monkeypatch):
    client = SnmpClient("test-only")
    root = parse_oid("1.3.6.1.2.1.2.2.1.1")

    def fake_bulk(_address, _cursor, _max_repetitions):
        return [
            SnmpValue(root + (1,), 1, 0x02),
            SnmpValue(root + (2,), None, 0x82),
        ]

    def unexpected_next(*_args):
        raise AssertionError("GETNEXT should not follow endOfMibView")

    monkeypatch.setattr(client, "get_bulk", fake_bulk)
    monkeypatch.setattr(client, "get_next", unexpected_next)

    values = client.walk("192.0.2.1", ".".join(map(str, root)), 8)

    assert [(value.oid, value.value) for value in values] == [(root + (1,), 1)]


def test_snmp_walk_falls_back_when_bulk_returns_no_bindings(monkeypatch):
    client = SnmpClient("test-only")
    root = parse_oid("1.3.6.1.2.1.2.2.1.1")
    bulk_calls = []
    next_calls = []

    def empty_bulk(_address, cursor, max_repetitions):
        bulk_calls.append((cursor, max_repetitions))
        return []

    def fake_next(_address, cursor):
        next_calls.append(cursor)
        if len(next_calls) == 1:
            return SnmpValue(root + (1,), 1, 0x02)
        raise SnmpEndOfMib("end of SNMP table")

    monkeypatch.setattr(client, "get_bulk", empty_bulk)
    monkeypatch.setattr(client, "get_next", fake_next)

    values = client.walk("192.0.2.1", ".".join(map(str, root)), 8)

    assert [value.oid for value in values] == [root + (1,)]
    assert bulk_calls == [(root, 8)]
    assert client._bulk_disabled == {"192.0.2.1"}
    assert next_calls == [root, root + (1,)]


def test_snmp_walk_does_not_hide_timeout(monkeypatch):
    client = SnmpClient("test-only")
    root = parse_oid("1.3.6.1.2.1.2.2.1.1")
    calls = []
    bulk_calls = []

    def fake_bulk(_address, oid, max_repetitions):
        bulk_calls.append((oid, max_repetitions))
        raise SnmpError("SNMP GETBULK unsupported")

    def fake_next(_address, oid):
        calls.append(oid)
        if len(calls) == 1:
            return SnmpValue(root + (1,), 1, 0x02)
        raise SnmpError("SNMP query timed out")

    monkeypatch.setattr(client, "get_bulk", fake_bulk)
    monkeypatch.setattr(client, "get_next", fake_next)
    with pytest.raises(SnmpError, match="timed out"):
        client.walk("192.0.2.1", ".".join(map(str, root)), 4)
    assert bulk_calls == [(root, 4)]
    assert client._bulk_disabled == {"192.0.2.1"}
    with pytest.raises(SnmpError, match="timed out"):
        client.walk("192.0.2.1", ".".join(map(str, root)), 4)
    assert bulk_calls == [(root, 4)]


def test_snmp_walk_returns_empty_when_first_getnext_leaves_prefix(monkeypatch):
    client = SnmpClient("test-only")
    root = parse_oid(IF_INDEX)
    outside_prefix = root[:-1] + (root[-1] + 1,)

    def unsupported_bulk(*_args):
        raise SnmpError("SNMP GETBULK unsupported")

    monkeypatch.setattr(client, "get_bulk", unsupported_bulk)
    monkeypatch.setattr(
        client, "get_next",
        lambda _address, _oid: SnmpValue(outside_prefix, None, 0x05),
    )

    assert client.walk("192.0.2.1", IF_INDEX, 16) == []


def test_iperf_endpoint_lock_cancel_and_restart(database, monkeypatch):
    manager = IperfTaskManager(database, replace(Settings(), enable_live=True, enable_iperf3=True))
    started = threading.Event()
    def blocked(_client, _server, _params, cancellation):
        started.set()
        cancellation.wait(2)
        if cancellation.is_set():
            raise TaskCancelled()
        return {"received_bps": 1_000_000}
    monkeypatch.setattr(manager, "_execute", blocked)
    task = manager.create("host-1", "host-2")
    assert started.wait(2)
    with pytest.raises(TaskConflict):
        manager.create("host-2", "host-3")
    manager.cancel(task["task_id"])
    for _ in range(100):
        if manager.get(task["task_id"])["task_state"] == "cancelled":
            break
        time.sleep(0.01)
    assert manager.get(task["task_id"])["task_state"] == "cancelled"
    monkeypatch.setattr(manager, "_execute", lambda *_: {"received_bps": 2_000_000})
    next_task = manager.create("host-1", "host-2")
    for _ in range(100):
        if manager.get(next_task["task_id"])["task_state"] == "succeeded":
            break
        time.sleep(0.01)
    assert manager.get(next_task["task_id"])["result"]["received_bps"] == 2_000_000


def test_database_marks_unfinished_iperf_tasks_interrupted_on_restart(database):
    with database.connect() as db:
        for task_id, state in (("task-pending", "pending"), ("task-running", "running")):
            db.execute(
                """INSERT INTO tasks(task_id,type,task_state,client_device_id,server_device_id,
                   params,created_at,data_source)
                   VALUES (?,'iperf3',?,'host-1','host-2','{}','2026-10-14T00:00:00Z','live')""",
                (task_id, state),
            )

    database.initialize()
    tasks = database.rows(
        "SELECT task_id,task_state,finished_at,error FROM tasks WHERE task_id LIKE 'task-%' ORDER BY task_id"
    )

    assert [task["task_state"] for task in tasks] == ["failed", "failed"]
    assert all(task["finished_at"] for task in tasks)
    assert all(decode(task["error"])["code"] == "INTERRUPTED" for task in tasks)


def test_iperf_execute_waits_for_remote_server_ready(database, monkeypatch):
    class FakeProcess:
        def __init__(self, stdout_text="", communicate_text="", returncode=None):
            self.stdout = io.StringIO(stdout_text)
            self.communicate_text = communicate_text
            self.returncode = returncode
            self.terminated = False

        def poll(self):
            return self.returncode

        def communicate(self, timeout=None):
            if self.returncode is None:
                self.returncode = 0
            return self.communicate_text, ""

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    class ReadySelector:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def register(self, *_args):
            pass

        def select(self, _timeout):
            return [(None, 1)]

    result_json = '{"end":{"sum_sent":{"bytes":100,"bits_per_second":800},' \
                  '"sum_received":{"bytes":90,"bits_per_second":720}}}'
    server_process = FakeProcess(stdout_text="NMS_IPERF_READY\n")
    client_process = FakeProcess(communicate_text=result_json)
    popen_calls = []

    def fake_popen(command, **_kwargs):
        popen_calls.append(command)
        return server_process if len(popen_calls) == 1 else client_process

    monkeypatch.setattr("app.modules.iperf_tasks.subprocess.Popen", fake_popen)
    monkeypatch.setattr("app.modules.iperf_tasks.selectors.DefaultSelector", ReadySelector)
    monkeypatch.setattr("app.modules.iperf_tasks.secrets.randbelow", lambda _limit: 0)
    manager = IperfTaskManager(database, Settings())
    client = {"device_id":"host-1", "address":"192.0.2.11"}
    server = {"device_id":"host-2", "address":"192.0.2.12"}

    result = manager._execute(client, server, {"duration_s":2, "target_bps":1_000_000}, threading.Event())

    server_command = popen_calls[0][-1]
    assert "NMS_IPERF_READY" in server_command and "/proc/net/tcp" in server_command
    assert "/proc/net/tcp6" in server_command and "trap cleanup_server EXIT" in server_command
    assert "timeout 15s iperf3 -s -1 -J -p 52000" in server_command
    assert popen_calls[1][-1].endswith("iperf3 -c 192.0.2.12 -J -t 2 -b 1000000 -p 52000")
    assert result["received_bytes"] == 90 and result["received_bps"] == 720
    assert server_process.terminated


def test_iperf_server_readiness_timeout_cleans_up_without_starting_client(database, monkeypatch):
    class FakeProcess:
        stdout = io.StringIO("")

        def __init__(self):
            self.returncode = None
            self.terminated = False

        def poll(self):
            return self.returncode

        def communicate(self, timeout=None):
            return "", ""

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    class NoReadySelector:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def register(self, *_args):
            pass

        def select(self, _timeout):
            return []

    server_process = FakeProcess()
    popen_calls = []
    ticks = iter((0.0, 6.0))
    monkeypatch.setattr("app.modules.iperf_tasks.subprocess.Popen",
                        lambda command, **_kwargs: (popen_calls.append(command), server_process)[1])
    monkeypatch.setattr("app.modules.iperf_tasks.selectors.DefaultSelector", NoReadySelector)
    monkeypatch.setattr("app.modules.iperf_tasks.secrets.randbelow", lambda _limit: 0)
    monkeypatch.setattr("app.modules.iperf_tasks.time.monotonic", lambda: next(ticks))
    manager = IperfTaskManager(database, Settings())
    client = {"device_id":"host-1", "address":"192.0.2.11"}
    server = {"device_id":"host-2", "address":"192.0.2.12"}

    with pytest.raises(TimeoutError, match="server readiness timed out"):
        manager._execute(client, server, {"duration_s":2, "target_bps":1_000_000}, threading.Event())

    assert len(popen_calls) == 1
    assert server_process.terminated


@pytest.mark.parametrize("cancel_at", ["server_ready", "client_run"])
def test_iperf_cancel_terminates_started_ssh_processes(database, monkeypatch, cancel_at):
    cancellation = threading.Event()

    class FakeProcess:
        def __init__(self, role, stdout_text=""):
            self.role = role
            self.stdout = io.StringIO(stdout_text)
            self.returncode = None
            self.terminated = False
            self.killed = False

        def poll(self):
            return self.returncode

        def communicate(self, timeout=None):
            if self.role == "client" and cancel_at == "client_run" and not self.terminated:
                cancellation.set()
                raise subprocess.TimeoutExpired("ssh-client", timeout)
            return "", ""

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def kill(self):
            self.killed = True
            self.returncode = -9

    class FakeSelector:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def register(self, *_args):
            pass

        def select(self, _timeout):
            if cancel_at == "server_ready":
                cancellation.set()
                return []
            return [(None, 1)]

    server_process = FakeProcess("server", "NMS_IPERF_READY\n")
    client_process = FakeProcess("client")
    processes = []

    def fake_popen(_command, **_kwargs):
        process = server_process if not processes else client_process
        processes.append(process)
        return process

    monkeypatch.setattr("app.modules.iperf_tasks.subprocess.Popen", fake_popen)
    monkeypatch.setattr("app.modules.iperf_tasks.selectors.DefaultSelector", FakeSelector)
    monkeypatch.setattr("app.modules.iperf_tasks.secrets.randbelow", lambda _limit: 0)
    manager = IperfTaskManager(database, Settings())
    client = {"device_id": "host-1", "address": "192.0.2.11"}
    server = {"device_id": "host-2", "address": "192.0.2.12"}

    with pytest.raises(TaskCancelled):
        manager._execute(client, server, {"duration_s": 2, "target_bps": 1_000_000}, cancellation)

    assert len(processes) == (1 if cancel_at == "server_ready" else 2)
    assert all(process.terminated for process in processes)


def test_collector_pipeline_with_fake_snmp(database, monkeypatch):
    class FakeSNMP:
        round = 0
        speed_queries = []
        def __init__(self, *_args): pass
        def walk(self, _address, prefix, _limit):
            if prefix == IF_INDEX:
                return [SnmpValue(parse_oid(IF_INDEX) + (2,), 2, 0x02)]
            if prefix == ROUTE_DEST:
                return [SnmpValue(parse_oid(ROUTE_DEST) + (192,168,1,0), "192.168.1.0", 0x40)]
            raise AssertionError(prefix)
        def get(self, _address, oid):
            if oid == SYS_UPTIME:
                type(self).round += 1
                return SnmpValue(parse_oid(oid), type(self).round * 1000, 0x43)
            if oid.startswith((IF_HIGH_SPEED, IF_SPEED)):
                type(self).speed_queries.append(oid)
                raise SnmpError("SNMP object unavailable")
            prefix = str(oid).rsplit('.', 1)[0]
            values = {IF_DESCR: "ens3", IF_ADMIN: 1, IF_OPER: 1,
                      IF_IN: 1000 + type(self).round * 1_125_000,
                      IF_OUT: 2000 + type(self).round * 2_250_000, IF_DISCONTINUITY: 0,
                      ROUTE_MASK: "255.255.255.0", ROUTE_NEXT_HOP: "0.0.0.0",
                      ROUTE_IF_INDEX: 2}
            if oid.startswith((ROUTE_MASK, ROUTE_NEXT_HOP, ROUTE_IF_INDEX)):
                prefix = next(k for k in (ROUTE_MASK, ROUTE_NEXT_HOP, ROUTE_IF_INDEX) if oid.startswith(k))
            return SnmpValue(parse_oid(oid), values[prefix], 0x02)
    monkeypatch.setattr("app.service.SnmpClient", FakeSNMP)
    instants = iter([
        stamp
        for second in (0, 10, 20, 30)
        for stamp in ([f"2026-10-13T10:00:{second:02d}Z"] * 3)
    ])
    monkeypatch.setattr("app.service.utc_now", lambda: next(instants))
    service = CollectorService(database, replace(Settings(), enable_live=True,
                                                  snmp_community="test-only"))
    results = [service.run_once("r-0-1") for _ in range(4)]
    assert all(result["state"] == "succeeded" for result in results)
    assert database.row("SELECT COUNT(*) AS n FROM config_snapshots")["n"] == 1
    latest = database.row(
        "SELECT valid,in_rate_bps,out_rate_bps,in_util_pct,out_util_pct "
        "FROM metric_samples ORDER BY metric_id DESC LIMIT 1"
    )
    assert latest == {
        "valid": 1, "in_rate_bps": 900_000.0, "out_rate_bps": 1_800_000.0,
        "in_util_pct": None, "out_util_pct": None,
    }
    assert database.row(
        "SELECT reference_speed_bps FROM interfaces WHERE interface_id='r-0-1:2'"
    ) == {"reference_speed_bps": None}
    assert len(FakeSNMP.speed_queries) == 8  # Both speed OIDs were unavailable on all four polls.
    assert database.row("SELECT COUNT(*) AS n FROM risks") == {"n": 0}
    assert database.row("SELECT COUNT(*) AS n FROM routes")["n"] == 1


def test_collector_uses_legacy_if_speed_when_high_speed_is_unavailable(database, monkeypatch):
    class LegacySpeedSNMP:
        round = 0

        def walk(self, _address, prefix, _limit):
            if prefix == IF_INDEX:
                return [SnmpValue(parse_oid(IF_INDEX) + (2,), 2, 0x02)]
            if prefix == ROUTE_DEST:
                return [SnmpValue(parse_oid(ROUTE_DEST) + (192, 168, 1, 0),
                                  "192.168.1.0", 0x40)]
            raise AssertionError(prefix)

        def get(self, _address, oid):
            if oid == SYS_UPTIME:
                type(self).round += 1
                return SnmpValue(parse_oid(oid), type(self).round * 1000, 0x43)
            if oid == f"{IF_HIGH_SPEED}.2":
                raise SnmpError("SNMP object unavailable")
            if oid == f"{IF_SPEED}.2":
                return SnmpValue(parse_oid(oid), 100_000_000, 0x41)
            prefix = str(oid).rsplit('.', 1)[0]
            values = {
                IF_DESCR: "ens3", IF_ADMIN: 1, IF_OPER: 1,
                IF_IN: 100 + (type(self).round - 1) * 1_125_000,
                IF_OUT: 200 + (type(self).round - 1) * 2_250_000,
                IF_DISCONTINUITY: 0,
                ROUTE_MASK: "255.255.255.0", ROUTE_NEXT_HOP: "0.0.0.0",
                ROUTE_IF_INDEX: 2,
            }
            if oid.startswith((ROUTE_MASK, ROUTE_NEXT_HOP, ROUTE_IF_INDEX)):
                prefix = next(k for k in (ROUTE_MASK, ROUTE_NEXT_HOP, ROUTE_IF_INDEX)
                              if oid.startswith(k))
            return SnmpValue(parse_oid(oid), values[prefix], 0x02)

    instants = iter(["2026-10-13T10:00:00Z", "2026-10-13T10:00:10Z"])
    monkeypatch.setattr("app.service.utc_now", lambda: next(instants))
    service = CollectorService(database, replace(Settings(), enable_live=True,
                                                  snmp_community="test-only"))
    device = {"device_id": "r-0-1", "address": "192.168.0.1"}

    first = service._router(LegacySpeedSNMP(), device)
    second = service._router(LegacySpeedSNMP(), device)

    assert first["status"] == second["status"] == "ok"
    assert database.row(
        "SELECT reference_speed_bps FROM interfaces WHERE interface_id='r-0-1:2'"
    ) == {"reference_speed_bps": 100_000_000}
    metric = database.row(
        "SELECT valid,in_rate_bps,out_rate_bps,in_util_pct,out_util_pct "
        "FROM metric_samples ORDER BY metric_id DESC LIMIT 1"
    )
    assert metric["valid"] == 1
    assert metric["in_rate_bps"] == 900_000.0
    assert metric["out_rate_bps"] == 1_800_000.0
    assert metric["in_util_pct"] == pytest.approx(0.9)
    assert metric["out_util_pct"] == pytest.approx(1.8)


@pytest.mark.parametrize(
    ("route_suffix", "destination", "problem", "empty_walk"),
    [
        ((192, 168, 1, 0), None, "invalid destination or index", False),
        ((192, 168, 1, 0), "198.51.100.0", "does not match its index", False),
        ((192, 168, 1, 999), "192.168.1.0", "invalid destination or index", False),
        ((), None, "route table returned no entries", True),
    ],
)
def test_collector_preserves_routes_and_snapshots_on_incomplete_route(database, route_suffix, destination, problem, empty_walk):
    class FakeSNMP:
        def walk(self, _address, prefix, _limit):
            if prefix == IF_INDEX:
                return [SnmpValue(parse_oid(IF_INDEX) + (2,), 2, 0x02)]
            if prefix == ROUTE_DEST:
                if empty_walk:
                    return []
                tag = 0x40 if destination is not None else 0x05
                return [SnmpValue(parse_oid(ROUTE_DEST) + route_suffix, destination, tag)]
            raise AssertionError(prefix)

        def get(self, _address, oid):
            if oid == SYS_UPTIME:
                return SnmpValue(parse_oid(oid), 1000, 0x43)
            prefix = str(oid).rsplit('.', 1)[0]
            values = {IF_DESCR: "ens3", IF_ADMIN: 1, IF_OPER: 1,
                      IF_HIGH_SPEED: 1, IF_SPEED: 1_000_000_000,
                      IF_IN: 1000, IF_OUT: 2000, IF_DISCONTINUITY: 0}
            return SnmpValue(parse_oid(oid), values[prefix], 0x02)

    previous_route = {
        "route_id": "r-0-1:192.168.1.0", "device_id": "r-0-1",
        "destination": "192.168.1.0", "mask": "255.255.255.0",
        "next_hop": "192.168.0.1", "interface_id": "r-0-1:2",
    }
    save_snapshot(database, "r-0-1", "2026-10-13T10:00:00Z", [
        {"interface_id":"r-0-1:2", "description":"ens3"},
    ], [previous_route], data_source="sample")
    with database.connect() as db:
        db.execute(
            """INSERT INTO routes(route_id,device_id,destination,mask,next_hop,interface_id,sampled_at,data_source)
               VALUES (?,?,?,?,?,?,?,?)""",
            (previous_route["route_id"], previous_route["device_id"], previous_route["destination"],
             previous_route["mask"], previous_route["next_hop"], previous_route["interface_id"],
             "2026-10-13T10:00:00Z", "sample"),
        )

    service = CollectorService(database, replace(Settings(), enable_live=True, snmp_community="test-only"))
    result = service._router(FakeSNMP(), {"device_id":"r-0-1", "address":"192.168.0.1"})

    assert result["status"] == "partial"
    assert any(problem in item for item in result["problems"])
    assert database.row("SELECT route_id,destination FROM routes WHERE device_id='r-0-1'") == {
        "route_id": previous_route["route_id"], "destination": previous_route["destination"],
    }
    assert database.row("SELECT COUNT(*) AS n FROM config_snapshots")["n"] == 1


@pytest.mark.parametrize(
    ("max_interfaces", "max_routes", "route_values", "expected_problem"),
    [
        (1, 64, [], "interface walk reached configured limit"),
        (16, 1, [SnmpValue(parse_oid(ROUTE_DEST) + (198, 51, 100, 0),
                           "198.51.100.0", 0x40)], "route walk reached configured limit"),
    ],
)
def test_collector_marks_walk_limit_partial_and_preserves_snapshot(
    database, max_interfaces, max_routes, route_values, expected_problem,
):
    old_sampled_at = "2026-10-13T10:00:00Z"
    old_interface = {"interface_id": "r-0-1:2", "description": "ens3"}
    old_route = {
        "route_id": "r-0-1:192.168.1.0", "device_id": "r-0-1",
        "destination": "192.168.1.0", "mask": "255.255.255.0",
        "next_hop": "192.168.0.1", "interface_id": "r-0-1:2",
    }
    save_snapshot(database, "r-0-1", old_sampled_at, [old_interface], [old_route], data_source="sample")
    with database.connect() as db:
        db.execute(
            """INSERT INTO routes(route_id,device_id,destination,mask,next_hop,
               interface_id,sampled_at,data_source) VALUES (?,?,?,?,?,?,?,'live')""",
            (old_route["route_id"], old_route["device_id"], old_route["destination"],
             old_route["mask"], old_route["next_hop"], old_route["interface_id"], old_sampled_at),
        )
    old_snapshot = database.row("SELECT snapshot_id,content FROM config_snapshots")
    old_route_row = database.row(
        "SELECT route_id,destination,sampled_at FROM routes WHERE device_id='r-0-1'"
    )

    class CappedWalkSNMP:
        def walk(self, _address, prefix, _limit):
            if prefix == IF_INDEX:
                return [SnmpValue(parse_oid(IF_INDEX) + (2,), 2, 0x02)]
            if prefix == ROUTE_DEST:
                return route_values
            raise AssertionError(prefix)

        def get(self, _address, oid):
            if oid == SYS_UPTIME:
                return SnmpValue(parse_oid(oid), 1000, 0x43)
            values = {IF_DESCR: "ens3", IF_ADMIN: 1, IF_OPER: 1,
                      IF_HIGH_SPEED: 1, IF_SPEED: 1_000_000_000,
                      IF_IN: 1000, IF_OUT: 2000, IF_DISCONTINUITY: 0,
                      ROUTE_MASK: "255.255.255.0", ROUTE_NEXT_HOP: "192.168.0.2",
                      ROUTE_IF_INDEX: 2}
            for root, value in values.items():
                if str(oid).startswith(f"{root}."):
                    return SnmpValue(parse_oid(oid), value, 0x02)
            raise AssertionError(oid)

    service = CollectorService(database, replace(
        Settings(), enable_live=True, snmp_community="test-only",
        max_interfaces=max_interfaces, max_routes=max_routes,
    ))
    result = service._router(CappedWalkSNMP(), {"device_id": "r-0-1", "address": "192.168.0.1"})

    assert result["status"] == "partial"
    assert expected_problem in result["problems"]
    assert database.row("SELECT snapshot_id,content FROM config_snapshots") == old_snapshot
    assert database.row(
        "SELECT route_id,destination,sampled_at FROM routes WHERE device_id='r-0-1'"
    ) == old_route_row


def test_empty_interface_walk_preserves_existing_inventory_and_snapshot(database, monkeypatch):
    old_sampled_at = "2026-10-13T10:00:00Z"
    old_interface = {
        "interface_id": "r-0-1:2", "description": "ens3",
        "admin_status": "up", "oper_status": "up", "reference_speed_bps": 1_000_000_000,
    }
    old_route = {
        "route_id": "r-0-1:192.168.1.0", "device_id": "r-0-1",
        "destination": "192.168.1.0", "mask": "255.255.255.0",
        "next_hop": "192.168.0.1", "interface_id": "r-0-1:2",
    }
    with database.connect() as db:
        db.execute(
            """UPDATE interfaces SET description=?,admin_status=?,oper_status=?,
               reference_speed_bps=?,sampled_at=?,data_source='live'
               WHERE interface_id=?""",
            (old_interface["description"], old_interface["admin_status"],
             old_interface["oper_status"], old_interface["reference_speed_bps"],
             old_sampled_at, old_interface["interface_id"]),
        )
        db.execute(
            """INSERT INTO routes(route_id,device_id,destination,mask,next_hop,
               interface_id,sampled_at,data_source) VALUES (?,?,?,?,?,?,?,'live')""",
            (old_route["route_id"], old_route["device_id"], old_route["destination"],
             old_route["mask"], old_route["next_hop"], old_route["interface_id"], old_sampled_at),
        )
    snapshot_id = save_snapshot(database, "r-0-1", old_sampled_at, [old_interface], [old_route])
    old_snapshot = database.row(
        "SELECT snapshot_id,content FROM config_snapshots WHERE snapshot_id=?", (snapshot_id,)
    )

    class EmptyInterfaceSNMP:
        def __init__(self, *_args):
            pass

        def get(self, _address, oid):
            if oid == SYS_UPTIME:
                return SnmpValue(parse_oid(oid), 1000, 0x43)
            raise AssertionError(oid)

        def walk(self, _address, prefix, _limit):
            assert prefix == IF_INDEX
            return []

    monkeypatch.setattr("app.service.SnmpClient", EmptyInterfaceSNMP)
    service = CollectorService(database, replace(Settings(), enable_live=True,
                                                  snmp_community="test-only"))

    result = service.run_once("r-0-1")

    assert result["state"] == "partial"
    assert result["details"] == [{
        "device_id": "r-0-1", "status": "error", "reason": "interface index table is empty",
    }]
    assert database.row(
        """SELECT description,admin_status,oper_status,reference_speed_bps,
           query_status,sampled_at,data_source FROM interfaces WHERE interface_id='r-0-1:2'"""
    ) == {
        "description": "ens3", "admin_status": "up", "oper_status": "up",
        "reference_speed_bps": 1_000_000_000, "query_status": "ok",
        "sampled_at": old_sampled_at, "data_source": "live",
    }
    assert database.row("SELECT route_id,destination,sampled_at FROM routes WHERE device_id='r-0-1'") == {
        "route_id": old_route["route_id"], "destination": old_route["destination"],
        "sampled_at": old_sampled_at,
    }
    assert database.row("SELECT COUNT(*) AS n FROM config_snapshots") == {"n": 1}
    assert database.row(
        "SELECT snapshot_id,content FROM config_snapshots WHERE snapshot_id=?", (snapshot_id,)
    ) == old_snapshot


def test_collector_isolates_snmp_failures_and_keeps_ping_unknown(database, monkeypatch):
    calls = []

    class FailingSNMP:
        def __init__(self, *_args): pass
        def get(self, address, _oid):
            calls.append(("snmp", address))
            raise SnmpError("SNMP query timed out")

    def failed_ping(address, _timeout):
        calls.append(("ping", address))
        return {"query_status": "timeout", "packets_sent": None, "packets_received": None,
                "rtt_ms": None, "loss_pct": None, "error": "timeout"}

    monkeypatch.setattr("app.service.SnmpClient", FailingSNMP)
    monkeypatch.setattr("app.service.ping.probe", failed_ping)
    service = CollectorService(database, replace(Settings(), enable_live=True,
                                                  snmp_community="test-only"))

    result = service.run_once()

    assert result["state"] == "partial"
    assert [item["status"] for item in result["details"]].count("error") == 4
    assert [item["status"] for item in result["details"]].count("timeout") == 3
    assert len([call for call in calls if call[0] == "snmp"]) == 4
    assert len([call for call in calls if call[0] == "ping"]) == 3
    assert database.row("SELECT COUNT(*) AS n FROM ping_samples WHERE query_status='timeout'")["n"] == 3
    assert database.row("SELECT COUNT(*) AS n FROM events")["n"] == 0
    assert database.row("SELECT COUNT(*) AS n FROM fault_state WHERE failures<>0")["n"] == 0
    saved = database.row("SELECT state,details FROM collector_runs WHERE run_id=?", (result["run_id"],))
    assert saved["state"] == "partial"
    assert "test-only" not in saved["details"]


def test_api_bootstrap_and_write_guard(tmp_path, monkeypatch):
    monkeypatch.delenv("NMS_API_TOKEN", raising=False)
    settings = replace(Settings(), db_path=tmp_path / "api.sqlite3")
    api_database = Database(settings.db_path)
    api_database.initialize()
    with api_database.connect() as conn:
        conn.execute(
            """INSERT INTO tasks(task_id,type,task_state,client_device_id,server_device_id,
               params,created_at,result,data_source)
               VALUES ('sample-task','iperf3','succeeded','host-1','host-2','{}',
                       '2026-10-13T10:00:00Z','{}','sample')"""
        )
    async def verify():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                health = await client.get("/api/v1/health")
                assert health.status_code == 200 and not health.json()["data"]["live_enabled"]
                devices = (await client.get("/api/v1/devices")).json()
                assert devices["data"]["total"] == 8
                assert all(x["data_source"] == "document" for x in devices["data"]["items"])
                sample_task = (await client.get("/api/v1/tasks/sample-task")).json()
                assert sample_task["data_source"] == sample_task["data"]["data_source"] == "sample"
                assert (await client.get("/api/v1/metrics")).json()["data"]["items"] == []
                assert (await client.get("/api/v1/metrics", params={"from": "2026-10-13T00:00:00Z"})).status_code == 422
                assert (await client.post("/api/v1/collect", json={})).status_code == 503
                assert (await client.get("/")).status_code == 200
    asyncio.run(verify())


def test_api_cancel_task_requires_token_and_signals_active_manager(tmp_path, monkeypatch):
    token = "test-only-token"
    monkeypatch.setenv("NMS_API_TOKEN", token)
    settings = replace(Settings(), db_path=tmp_path / "cancel-api.sqlite3")
    app = create_app(settings)
    cancellation = threading.Event()

    async def verify():
        async with app.router.lifespan_context(app):
            with app.state.database.connect() as db:
                db.execute(
                    """INSERT INTO tasks(task_id,type,task_state,client_device_id,server_device_id,
                       params,created_at,data_source)
                       VALUES ('task-live','iperf3','running','host-1','host-2','{}',
                               '2026-10-14T00:00:00Z','live')"""
                )
            app.state.tasks._active["task-live"] = cancellation
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                denied = await client.post("/api/v1/tasks/task-live/cancel")
                assert denied.status_code == 401
                assert not cancellation.is_set()

                response = await client.post(
                    "/api/v1/tasks/task-live/cancel", headers={"X-Admin-Token": token}
                )
                assert response.status_code == 200
                assert response.json()["data"]["task_state"] == "running"
                assert cancellation.is_set()

                missing = await client.post(
                    "/api/v1/tasks/missing/cancel", headers={"X-Admin-Token": token}
                )
                assert missing.status_code == 404

    asyncio.run(verify())


def test_api_traffic_and_csv_agree(database, tmp_path, monkeypatch):
    monkeypatch.setenv("NMS_API_TOKEN", "test-token")
    settings = replace(Settings(), db_path=database.path)
    start, end = "2026-10-13T10:00:00Z", "2026-10-13T10:00:10Z"
    first = record_counter(database, "r-0-1:2", 100, 200, start, 100)
    second = record_counter(database, "r-0-1:2", 110, 220, "2026-10-13T10:00:05Z", 150)
    third = record_counter(database, "r-0-1:2", 120, 240, end, 200)
    async def verify():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                params = {"interface_id": "r-0-1:2", "from": start, "to": end}
                report = (await client.get("/api/v1/traffic", params=params)).json()["data"]
                exported = await client.get("/api/v1/traffic/export.csv", params=params)
                assert report["in_bytes"] == "20" and report["out_bytes"] == "40"
                assert report["observation_point"] == "r-0-1:2" and report["direction"] == "both"
                assert report["source_sample_ids"] == [
                    first["sample_id"], second["sample_id"], third["sample_id"]
                ]
                assert report["metric_sample_ids"] == [
                    second["metric_id"], third["metric_id"]
                ]
                rows = list(csv.DictReader(io.StringIO(exported.text.lstrip("\ufeff"))))
                assert report["complete"] and [row["bytes"] for row in rows] == ["20", "40"]
                assert [row["direction"] for row in rows] == ["in", "out"]
                only_in = (await client.get("/api/v1/traffic", params={**params, "direction": "in"})).json()["data"]
                assert only_in["in_bytes"] == "20" and only_in["out_bytes"] is None
                exported_in = await client.get("/api/v1/traffic/export.csv", params={**params, "direction":"in"})
                in_rows = list(csv.DictReader(io.StringIO(exported_in.text.lstrip("\ufeff"))))
                assert len(in_rows) == 1 and in_rows[0]["direction"] == "in" and in_rows[0]["bytes"] == "20"
                only_out = (await client.get("/api/v1/traffic", params={**params, "direction": "out"})).json()["data"]
                exported_out = await client.get("/api/v1/traffic/export.csv", params={**params, "direction":"out"})
                out_rows = list(csv.DictReader(io.StringIO(exported_out.text.lstrip("\ufeff"))))
                assert only_out["out_bytes"] == "40" and only_out["in_bytes"] is None
                assert len(out_rows) == 1 and out_rows[0]["direction"] == "out" and out_rows[0]["bytes"] == "40"
                filtered = (await client.get("/api/v1/metrics", params={"interface_id": "r-0-1:2",
                    "from": start, "to": end})).json()["data"]
                assert filtered["total"] == 2
                assert (await client.get("/api/v1/traffic", params={**params, "from": end})).status_code == 422
                assert (await client.post("/api/v1/service-baselines", json={"device_id": "host-1", "port": 22,
                                                                        "allowed": True})).status_code == 401
    asyncio.run(verify())


def test_api_ping_history_filter_for_performance_trend(database):
    healthy = {"query_status": "ok", "packets_sent": 2, "packets_received": 2,
               "rtt_ms": 1.25, "loss_pct": 0.0, "error": None}
    partial = {**healthy, "packets_received": 1, "rtt_ms": 2.5, "loss_pct": 50.0}
    record_ping(database, "host-1", "192.168.1.1", "2026-10-13T10:00:00Z", healthy,
                data_source="sample")
    record_ping(database, "host-1", "192.168.1.1", "2026-10-13T10:00:10Z", partial,
                data_source="sample")
    record_ping(database, "host-2", "192.168.2.1", "2026-10-13T10:00:10Z", healthy,
                data_source="sample")

    async def verify():
        app = create_app(replace(Settings(), db_path=database.path))
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                result = await client.get("/api/v1/ping-samples", params={
                    "device_id": "host-1", "limit": 120,
                })
                payload = result.json()["data"]
                assert result.status_code == 200
                assert payload["total"] == 2 and len(payload["items"]) == 2
                assert all(item["device_id"] == "host-1" for item in payload["items"])
                assert payload["items"][0]["rtt_ms"] == 2.5
                assert payload["items"][0]["loss_pct"] == 50.0
                assert payload["items"][0]["data_source"] == "sample"
    asyncio.run(verify())


def test_service_check_api_keeps_unavailable_as_unknown(database):
    with database.connect() as conn:
        conn.execute("INSERT INTO service_baselines(device_id,port,allowed) VALUES ('host-1',22,0)")
    record_service_check(database, "host-1", 22, "unavailable", "TCP connect did not confirm port state",
                         data_source="sample", observed_at="2026-10-13T10:00:00Z")

    async def verify():
        app = create_app(replace(Settings(), db_path=database.path))
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                checks = await client.get("/api/v1/service-checks", params={"device_id":"host-1", "limit":30})
                item = checks.json()["data"]["items"][0]
                assert checks.status_code == 200
                assert item["state"] == "unavailable"
                assert item["error"] == "TCP connect did not confirm port state"
                assert item["data_source"] == "sample"
                risks = await client.get("/api/v1/risks", params={"device_id":"host-1"})
                assert risks.json()["data"]["items"] == []
    asyncio.run(verify())


@pytest.mark.parametrize(
    ("enable_live", "enable_service_probe"),
    [(False, False), (False, True), (True, False)],
)
def test_service_probe_api_requires_both_feature_flags(
    database, monkeypatch, enable_live, enable_service_probe,
):
    token = "test-only-token"
    monkeypatch.setenv("NMS_API_TOKEN", token)
    calls = []

    def forbidden_probe(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("probe must not run while either feature flag is disabled")

    monkeypatch.setattr("app.main.check_tcp", forbidden_probe)
    settings = replace(
        Settings(), db_path=database.path, enable_live=enable_live,
        enable_service_probe=enable_service_probe,
    )

    async def verify():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/security/probe",
                    json={"device_id": "host-1", "port": 443},
                    headers={"X-Admin-Token": token},
                )
                assert response.status_code == 503
                assert response.json()["error"]["message"] == "service probe is disabled"
                assert calls == []
                assert database.row("SELECT COUNT(*) AS n FROM service_checks")["n"] == 0
                assert database.row("SELECT COUNT(*) AS n FROM risks")["n"] == 0

    asyncio.run(verify())


def test_service_probe_api_requires_token_managed_target_and_baseline(database, monkeypatch):
    token = "test-only-token"
    real_thread_dispatch = os.environ.get("NMS_TEST_REAL_THREAD_DISPATCH") == "1"
    monkeypatch.setenv("NMS_API_TOKEN", token)
    with database.connect() as db:
        db.execute(
            "INSERT INTO service_baselines(device_id,port,allowed) VALUES ('host-1',443,0)"
        )
    calls = []
    probe_threads = []
    request_thread = threading.current_thread()

    def fake_probe(address, port):
        calls.append((address, port))
        probe_threads.append(threading.current_thread())
        return "open", None

    monkeypatch.setattr("app.main.check_tcp", fake_probe)
    if not real_thread_dispatch:
        async def inline_to_thread(function, *args, **kwargs):
            return function(*args, **kwargs)

        # The restricted local sandbox stalls on thread return; opt-in outside it to
        # verify the production asyncio.to_thread dispatch path as well.
        monkeypatch.setattr("app.main.asyncio", SimpleNamespace(to_thread=inline_to_thread))
    settings = replace(Settings(), db_path=database.path, enable_live=True,
                       enable_service_probe=True)

    async def verify():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                body = {"device_id": "host-1", "port": 443}
                assert (await client.post("/api/v1/security/probe", json=body)).status_code == 401
                assert (await client.post(
                    "/api/v1/security/probe",
                    json={"device_id": "dev", "port": 443},
                    headers={"X-Admin-Token": token},
                )).status_code == 404
                assert (await client.post(
                    "/api/v1/security/probe",
                    json={"device_id": "host-2", "port": 443},
                    headers={"X-Admin-Token": token},
                )).status_code == 422
                assert calls == []

                response = await client.post(
                    "/api/v1/security/probe", json=body,
                    headers={"X-Admin-Token": token},
                )
                assert response.status_code == 200
                assert response.json()["data"]["state"] == "open"
                assert response.json()["data_source"] == "live"
                assert calls == [("192.168.1.1", 443)]
                assert len(probe_threads) == 1
                assert (probe_threads[0] is not request_thread) is real_thread_dispatch

                checks = (await client.get(
                    "/api/v1/service-checks", params={"device_id": "host-1"},
                )).json()["data"]["items"]
                assert len(checks) == 1
                assert (checks[0]["device_id"], checks[0]["port"], checks[0]["state"]) == (
                    "host-1", 443, "open",
                )
                risks = (await client.get(
                    "/api/v1/risks", params={"device_id": "host-1"},
                )).json()["data"]["items"]
                assert len(risks) == 1
                assert risks[0]["data_source"] == "live"
                assert risks[0]["evidence"]["observed_state"] == "open"
                assert risks[0]["evidence"]["check_id"] == checks[0]["check_id"]

    asyncio.run(verify())


@pytest.mark.parametrize(
    ("outcome", "error_type", "expected_state", "expected_error"),
    [
        ("open", None, "open", None),
        ("refused", ConnectionRefusedError, "closed", None),
        ("timeout", TimeoutError, "unavailable", "TCP connect did not confirm port state"),
        ("network_error", OSError, "unavailable", "TCP connect did not confirm port state"),
    ],
)
def test_tcp_probe_distinguishes_closed_from_unknown(
    monkeypatch, outcome, error_type, expected_state, expected_error,
):
    class ConnectedSocket:
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.closed = True

    connected = ConnectedSocket()

    def fake_create_connection(address, timeout):
        assert address == ("192.0.2.10", 443)
        assert timeout == 0.25
        if error_type is not None:
            raise error_type("test probe failure")
        return connected

    monkeypatch.setattr("app.modules.security.socket.create_connection", fake_create_connection)

    result = check_tcp("192.0.2.10", 443, timeout_s=0.25)

    assert result == (expected_state, expected_error)
    if outcome == "open":
        assert connected.closed


def test_api_config_snapshot_diff_and_validation(database, tmp_path, monkeypatch):
    before_id = save_snapshot(database, "r-0-1", "2026-10-13T10:00:00Z", [
        {"interface_id": "r-0-1:1", "description": "eth0", "admin_status": "up"},
    ], [
        {"route_id": "r-0-1:192.168.1.0", "destination": "192.168.1.0",
         "mask": "255.255.255.0", "next_hop": "0.0.0.0", "interface_id": "r-0-1:1"},
    ])
    after_id = save_snapshot(database, "r-0-1", "2026-10-13T10:01:00Z", [
        {"interface_id": "r-0-1:1", "description": "eth0", "admin_status": "down"},
        {"interface_id": "r-0-1:2", "description": "eth1", "admin_status": "up"},
    ], [
        {"route_id": "r-0-1:192.168.2.0", "destination": "192.168.2.0",
         "mask": "255.255.255.0", "next_hop": "192.168.1.254", "interface_id": "r-0-1:2"},
    ])
    other_device_id = save_snapshot(database, "host-1", "2026-10-13T10:02:00Z", [], [])
    settings = replace(Settings(), db_path=database.path)

    async def verify():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                listed = await client.get("/api/v1/config-snapshots", params={"device_id": "r-0-1"})
                assert listed.status_code == 200
                assert listed.json()["data"]["total"] == 2
                assert listed.json()["data"]["items"][0]["content"]["interfaces"]["r-0-1:1"]["admin_status"] == "down"

                response = await client.get("/api/v1/config-diff", params={
                    "before_id": before_id, "after_id": after_id,
                })
                assert response.status_code == 200
                data = response.json()["data"]
                assert data["device_id"] == "r-0-1"
                assert {(item["kind"], item["change"]) for item in data["changes"]} == {
                    ("interfaces", "changed"), ("interfaces", "added"),
                    ("routes", "removed"), ("routes", "added"),
                }
                assert data["data_source"] == "live"
                assert (await client.get("/api/v1/config-diff", params={
                    "before_id": before_id, "after_id": 999999,
                })).status_code == 404
                assert (await client.get("/api/v1/config-diff", params={
                    "before_id": before_id, "after_id": other_device_id,
                })).status_code == 422
                assert (await client.get("/api/v1/config-diff")).status_code == 422

    asyncio.run(verify())


def test_database_migrates_legacy_baseline_with_backup_and_is_idempotent(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    legacy_schema = SCHEMA.replace(
        "  data_source TEXT NOT NULL DEFAULT 'configured',\n", ""
    ).replace("  data_source TEXT NOT NULL, acknowledged_at TEXT\n", "  data_source TEXT NOT NULL\n")
    raw = sqlite3.connect(path)
    raw.executescript(legacy_schema)
    raw.execute(
        """INSERT INTO devices(device_id,label,address,role,identity_status,data_source)
           VALUES ('host-1','host','192.168.1.1','test-host','candidate','document')"""
    )
    raw.execute(
        "INSERT INTO service_baselines(device_id,port,allowed,note) VALUES ('host-1',22,1,'legacy')"
    )
    raw.execute(
        """INSERT INTO events(device_id,interface_id,type,state,first_seen,last_seen,
           recovered_at,evidence_sample_ids,data_source)
           VALUES ('host-1',NULL,'ping_unreachable','active','2026-10-13T10:00:00Z',
                   '2026-10-13T10:00:00Z',NULL,'[1]','sample')"""
    )
    raw.execute("PRAGMA user_version=0")
    raw.commit()
    raw.close()

    database = Database(path)
    database.initialize()
    assert database.row("PRAGMA user_version")["user_version"] == CURRENT_SCHEMA_VERSION
    assert database.row(
        "SELECT allowed,note,data_source FROM service_baselines WHERE device_id='host-1' AND port=22"
    ) == {"allowed": 1, "note": "legacy", "data_source": "configured"}
    backups = list((tmp_path / "backups").glob("legacy.pre-v0-to-v2-*.sqlite3"))
    assert len(backups) == 1
    assert backups[0].stat().st_mode & 0o777 == 0o600
    preserved = sqlite3.connect(backups[0])
    try:
        old_columns = {row[1] for row in preserved.execute("PRAGMA table_info(service_baselines)")}
        old_event_columns = {row[1] for row in preserved.execute("PRAGMA table_info(events)")}
        assert "data_source" not in old_columns
        assert "acknowledged_at" not in old_event_columns
        assert preserved.execute("SELECT note FROM service_baselines WHERE port=22").fetchone() == ("legacy",)
        assert preserved.execute("SELECT state FROM events WHERE event_id=1").fetchone() == ("active",)
    finally:
        preserved.close()
    assert database.row("SELECT state,acknowledged_at FROM events WHERE event_id=1") == {
        "state":"active", "acknowledged_at":None,
    }

    database.initialize()
    assert len(list((tmp_path / "backups").glob("legacy.pre-v0-to-v2-*.sqlite3"))) == 1
    assert database.row("PRAGMA user_version")["user_version"] == CURRENT_SCHEMA_VERSION


def test_database_migrates_v1_events_without_losing_rows(tmp_path):
    path = tmp_path / "v1.sqlite3"
    v1_schema = SCHEMA.replace("  data_source TEXT NOT NULL, acknowledged_at TEXT\n", "  data_source TEXT NOT NULL\n")
    raw = sqlite3.connect(path)
    raw.executescript(v1_schema)
    raw.execute(
        """INSERT INTO devices(device_id,label,address,role,identity_status,data_source)
           VALUES ('host-1','host','192.168.1.1','test-host','candidate','document')"""
    )
    raw.execute(
        """INSERT INTO events(device_id,interface_id,type,state,first_seen,last_seen,
           recovered_at,evidence_sample_ids,data_source)
           VALUES ('host-1',NULL,'ping_unreachable','recovered','2026-10-13T10:00:00Z',
                   '2026-10-13T10:00:10Z','2026-10-13T10:00:10Z','[1,2]','live')"""
    )
    raw.execute("PRAGMA user_version=1")
    raw.commit()
    raw.close()

    database = Database(path)
    database.initialize()
    assert database.row("PRAGMA user_version")["user_version"] == CURRENT_SCHEMA_VERSION
    assert database.row("SELECT state,recovered_at,acknowledged_at FROM events WHERE event_id=1") == {
        "state":"recovered", "recovered_at":"2026-10-13T10:00:10Z", "acknowledged_at":None,
    }
    backups = list((tmp_path / "backups").glob("v1.pre-v1-to-v2-*.sqlite3"))
    assert len(backups) == 1
    assert backups[0].stat().st_mode & 0o777 == 0o600
    old_columns = {row[1] for row in sqlite3.connect(backups[0]).execute("PRAGMA table_info(events)")}
    assert "acknowledged_at" not in old_columns


def test_database_refuses_newer_schema_without_modifying_version(tmp_path):
    database = Database(tmp_path / "future.sqlite3")
    database.initialize()
    with database.connect() as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(RuntimeError, match="newer than supported"):
        database.initialize()
    assert database.row("PRAGMA user_version")["user_version"] == 99


def test_database_refuses_migration_when_backup_permissions_are_ineffective(tmp_path, monkeypatch):
    database = Database(tmp_path / "permissions.sqlite3")
    database.initialize()
    with database.connect() as db:
        db.execute("PRAGMA user_version=0")
    monkeypatch.setattr("pathlib.Path.chmod", lambda *_args, **_kwargs: None)
    with pytest.raises(PermissionError, match="does not enforce private backup permissions"):
        database.initialize()
    assert database.row("PRAGMA user_version")["user_version"] == 0
    assert not list((tmp_path / "backups").glob("*.sqlite3"))
