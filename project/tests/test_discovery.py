from __future__ import annotations

import asyncio
import csv
import io
import sqlite3
from dataclasses import replace

import pytest
from httpx import ASGITransport, AsyncClient

from app.collectors.snmp import SnmpError, SnmpValue, parse_oid
from app.core.config import Settings
from app.core.db import CURRENT_SCHEMA_VERSION, NETWORK_TABLES, SCHEMA, Database, encode
from app.main import create_app
from app.modules.discovery import (IP_ADDR, IP_IF_INDEX, IP_MASK, SYS_NAME, SYS_OBJECT_ID,
                                   read_inventory, store_inventory, topology, traffic_attribution)
from app.modules.fault_config import diff_snapshots, save_snapshot
from app.modules.perf_accounting import pair_traffic_report
from app.service import (CollectorService, IF_ADMIN, IF_DESCR, IF_DISCONTINUITY, IF_HIGH_SPEED,
                         IF_IN, IF_INDEX, IF_OPER, IF_OUT, ROUTE_DEST, ROUTE_IF_INDEX,
                         ROUTE_MASK, ROUTE_NEXT_HOP, SYS_UPTIME)


START = "2026-10-13T09:00:00Z"
END = "2026-10-13T09:00:10Z"


@pytest.fixture
def database(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    database.initialize()
    with database.connect() as db:
        db.execute("""INSERT INTO interfaces(interface_id,device_id,if_index,query_status,data_source)
                      VALUES ('r-0-1:2','r-0-1',2,'ok','live')""")
    return database


def address_row(ip="192.168.1.254", mask="255.255.255.0", network="192.168.1.0/24"):
    return {"interface_id": "r-0-1:2", "if_index": 2, "address": ip, "mask": mask, "network": network}


class Agent:
    entries = [("192.168.1.254", "192.168.1.254")]
    mask = "255.255.255.0"
    index = 2

    def __init__(self, *_args):
        pass

    def walk(self, _address, prefix, _limit):
        if prefix == IF_INDEX:
            return [SnmpValue(parse_oid(IF_INDEX) + (2,), 2, 0x02)]
        if prefix == ROUTE_DEST:
            return [SnmpValue(parse_oid(ROUTE_DEST) + (192, 168, 1, 0), "192.168.1.0", 0x40)]
        assert prefix == IP_ADDR
        return [SnmpValue(parse_oid(IP_ADDR) + tuple(map(int, suffix.split('.'))), value, 0x40)
                for suffix, value in self.entries]

    def get(self, _address, oid):
        values = {SYS_UPTIME: 1000, SYS_NAME: "router-one", SYS_OBJECT_ID: "1.3.6.1.4.1.8072",
                  f"{IF_DESCR}.2": "eth2", f"{IF_ADMIN}.2": 1, f"{IF_OPER}.2": 1,
                  f"{IF_HIGH_SPEED}.2": 1000, f"{IF_IN}.2": 100, f"{IF_OUT}.2": 200,
                  f"{IF_DISCONTINUITY}.2": 0, f"{ROUTE_MASK}.192.168.1.0": "255.255.255.0",
                  f"{ROUTE_NEXT_HOP}.192.168.1.0": "0.0.0.0", f"{ROUTE_IF_INDEX}.192.168.1.0": 2}
        if oid.startswith(IP_IF_INDEX + "."):
            value = self.index
        elif oid.startswith(IP_MASK + "."):
            value = self.mask
        else:
            if oid not in values:
                raise SnmpError("object unavailable")
            value = values[oid]
        return SnmpValue(parse_oid(oid), value, 0x02 if isinstance(value, int) else 0x04)


def test_collector_discovers_addresses_snapshots_and_topology_through_api(database, monkeypatch):
    monkeypatch.setattr("app.service.SnmpClient", Agent)
    settings = replace(Settings(), db_path=database.path, enable_live=True, snmp_community="test-only")
    result = CollectorService(database, settings).run_once("r-0-1")
    assert result["state"] == "succeeded"
    assert result["details"][0]["network_discovery"]["address_count"] == 1
    row = database.row("SELECT * FROM interface_addresses")
    assert (row["address"], row["mask"], row["network"]) == ("192.168.1.254", "255.255.255.0", "192.168.1.0/24")
    async def verify():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                data = (await client.get("/api/v1/topology")).json()["data"]
                measured = [x for x in data["edges"] if x["kind"] == "ip_subnet"]
                assert len(measured) == 1
                assert measured[0]["source"] == "r-0-1" and measured[0]["target"] == "subnet-1"
                assert measured[0]["confirmed"] and not measured[0]["physical_link_confirmed"]
                assert any(x["data_source"] == "document" for x in data["edges"])
                history = (await client.get("/api/v1/interface-addresses", params={"device_id":"r-0-1"})).json()["data"]
                assert history["total"] == 1 and history["items"][0]["data_source"] == "live"
                discovery = (await client.get("/api/v1/network-discoveries")).json()["data"]["items"][0]
                assert discovery["sys_name"] == "router-one" and discovery["query_status"] == "ok"
                snapshots = (await client.get("/api/v1/config-snapshots")).json()["data"]["items"]
                assert snapshots[0]["content"]["addresses"]
    asyncio.run(verify())


@pytest.mark.parametrize("failure", ["index", "mask", "unknown_interface", "empty", "limit"])
def test_incomplete_address_table_never_creates_confirmed_topology(database, failure):
    first = store_inventory(database, "r-0-1", START, [address_row()])
    agent = Agent()
    if failure == "index": agent.entries = [("192.168.1.253", "192.168.1.254")]
    if failure == "mask": agent.mask = "255.0.255.0"
    if failure == "unknown_interface": agent.index = 9
    if failure == "empty": agent.entries = []
    result = read_inventory(agent, database, "r-0-1", "192.168.0.1", END, {"r-0-1:2"},
                            limit=1 if failure == "limit" else 64)
    assert result["query_status"] != "ok" and result["issues"]
    assert not [e for e in topology(database)["edges"] if e["kind"] == "ip_subnet"]
    assert database.row("SELECT COUNT(*) AS n FROM interface_addresses WHERE discovery_id=?", (first["discovery_id"],))["n"] == 1


def test_window_attribution_uses_historical_mapping_and_rejects_changes(database):
    assert traffic_attribution(database, "r-0-1:2", START, END)["mapping_status"] == "unavailable"
    store_inventory(database, "r-0-1", START, [address_row()])
    report = traffic_attribution(database, "r-0-1:2", START, END)
    assert report["networks"] == ["192.168.1.0/24"] and not report["exact_host_pair"]
    store_inventory(database, "r-0-1", "2026-10-13T09:00:05Z", [address_row("192.168.2.254", network="192.168.2.0/24")])
    changed = traffic_attribution(database, "r-0-1:2", START, END)
    assert changed["mapping_status"] == "changed_or_incomplete" and changed["networks"] == []
    # A later mapping cannot be projected backwards onto an earlier window.
    assert traffic_attribution(database, "r-0-1:2", "2026-10-13T08:00:00Z", "2026-10-13T08:10:00Z")["networks"] == []


def test_snapshot_address_changes_and_unknown_sections_are_separate(database):
    before = save_snapshot(database, "r-0-1", START, [], [], addresses=[address_row()])
    after = save_snapshot(database, "r-0-1", END, [], [], addresses=[address_row(mask="255.255.0.0", network="192.168.0.0/16")])
    diff = diff_snapshots(database, before, after)
    assert len(diff["changes"]) == 1 and diff["changes"][0]["kind"] == "addresses"
    unknown = save_snapshot(database, "r-0-1", END, [], [])
    diff = diff_snapshots(database, after, unknown)
    assert diff["changes"] == [] and diff["incomplete_sections"] == ["addresses"]


def test_pair_statistics_keep_sources_and_windows_separate_and_csv_agrees(database):
    with database.connect() as db:
        for task_id, source, state, started, finished, result in (
            ("live-one", "live", "succeeded", START, END, {"received_bytes":2**53+1}),
            ("sample-one", "sample", "succeeded", START, END, {"received_bytes":25}),
            ("failed", "live", "failed", START, END, {"received_bytes":999}),
            ("crosses", "live", "succeeded", "2026-10-13T08:59:59Z", END, {"received_bytes":999}),
            ("missing", "live", "succeeded", START, END, {"received_bps":100}),
        ):
            db.execute("""INSERT INTO tasks(task_id,type,task_state,client_device_id,server_device_id,
                       params,created_at,started_at,finished_at,result,data_source)
                       VALUES (?,'iperf3',?,'host-1','host-2','{}',?,?,?,?,?)""",
                       (task_id,state,START,started,finished,encode(result),source))
    report = pair_traffic_report(database, START, END)
    assert {(x["data_source"], x["received_bytes"]) for x in report["items"]} == {("live",str(2**53+1)),("sample","25")}
    assert {x["reason"] for x in report["excluded_tasks"]} == {"crosses_window_boundary","received_bytes_unavailable"}
    assert not report["network_total_available"]
    async def verify():
        app = create_app(replace(Settings(), db_path=database.path))
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                params={"from":START,"to":END}
                data=(await client.get("/api/v1/traffic/pairs",params=params)).json()["data"]
                response=await client.get("/api/v1/traffic/pairs/export.csv",params=params)
                rows=list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))
                assert [(x["received_bytes"],x["data_source"]) for x in rows] == [(x["received_bytes"],x["data_source"]) for x in data["items"]]
                assert (await client.get("/api/v1/traffic/pairs",params={"from":END,"to":START})).status_code == 422
    asyncio.run(verify())


def test_v2_to_v3_migration_backs_up_and_preserves_existing_samples(tmp_path):
    path=tmp_path/'v2.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA.split(NETWORK_TABLES[0],1)[0])
        db.execute("INSERT INTO metadata VALUES ('keep','v2')")
        db.execute("PRAGMA user_version=2")
    database=Database(path)
    database.initialize()
    assert database.row("SELECT value FROM metadata WHERE key='keep'")["value"] == "v2"
    assert database.row("PRAGMA user_version")["user_version"] == CURRENT_SCHEMA_VERSION
    backup=list((tmp_path/'backups').glob('v2.pre-v2-to-v3-*.sqlite3'))
    assert len(backup)==1 and backup[0].stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backup[0]) as old:
        assert old.execute("PRAGMA user_version").fetchone()[0] == 2
        assert not old.execute("SELECT name FROM sqlite_master WHERE name='interface_addresses'").fetchall()
    database.initialize()
    assert len(list((tmp_path/'backups').glob('v2.pre-v2-to-v3-*.sqlite3')))==1


def test_restart_marks_unfinished_collection_instead_of_leaving_it_running(database):
    with database.connect() as db:
        db.execute("INSERT INTO collector_runs(started_at,state,details) VALUES (?,'running','[]')",(START,))
    database.initialize()
    run=database.row('SELECT * FROM collector_runs')
    assert run['state']=='failed' and run['finished_at']
    assert '重启中断' in run['details']


def test_latest_inventory_does_not_resurrect_old_success(database):
    first = store_inventory(database, 'r-0-1', START, [address_row()])
    # The newest attempt has no readable address rows. The successful history stays accessible.
    second = store_inventory(database, 'r-0-1', END, [], status='timeout', issues=['timeout'])

    async def verify():
        app = create_app(replace(Settings(), db_path=database.path, enable_live=False, poll_interval_s=0))
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
                current = (await client.get('/api/v1/interface-addresses?latest=true')).json()['data']
                assert current['total'] == 0 and current['items'] == []
                latest = (await client.get('/api/v1/network-discoveries?latest=true')).json()['data']
                assert latest['total'] == 1 and latest['items'][0]['discovery_id'] == second['discovery_id']
                assert latest['items'][0]['query_status'] == 'timeout'
                history = (await client.get('/api/v1/interface-addresses?limit=1&offset=0')).json()['data']
                assert history['total'] == 1 and history['items'][0]['discovery_id'] == first['discovery_id']
                assert (await client.get('/api/v1/interface-addresses?latest=true&discovery_id=1')).status_code == 422
                store_inventory(database, 'r-0-1', END, [address_row('192.168.1.253')], status='partial', issues=['limited'])
                current = (await client.get('/api/v1/interface-addresses?latest=true&device_id=r-0-1')).json()['data']
                assert current['total'] == 1 and current['items'][0]['address'] == '192.168.1.253'
                missing = (await client.get('/api/v1/interface-addresses?latest=true&device_id=host-1')).json()['data']
                assert missing['items'] == []
                assert (await client.get('/api/v1/network-discoveries?latest=true&offset=1')).json()['data']['items'] == []
    asyncio.run(verify())
