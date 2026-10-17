from __future__ import annotations

import ipaddress
import json
import threading
from dataclasses import dataclass

from app.collectors import ping
from app.collectors.snmp import SnmpClient, SnmpError
from app.core.config import Settings
from app.core.db import Database, utc_now
from app.modules.fault_config import record_interface_status, record_ping, save_snapshot
from app.modules.perf_accounting import record_counter
from app.modules.security import record_snmp_access_risk, record_utilization_risk
from app.modules.discovery import read_inventory


IF_INDEX = "1.3.6.1.2.1.2.2.1.1"
IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
IF_ADMIN = "1.3.6.1.2.1.2.2.1.7"
IF_OPER = "1.3.6.1.2.1.2.2.1.8"
IF_SPEED = "1.3.6.1.2.1.2.2.1.5"
IF_HIGH_SPEED = "1.3.6.1.2.1.31.1.1.1.15"
IF_IN = "1.3.6.1.2.1.31.1.1.1.6"
IF_OUT = "1.3.6.1.2.1.31.1.1.1.10"
IF_DISCONTINUITY = "1.3.6.1.2.1.31.1.1.1.19"
ROUTE_DEST = "1.3.6.1.2.1.4.21.1.1"
ROUTE_IF_INDEX = "1.3.6.1.2.1.4.21.1.2"
ROUTE_NEXT_HOP = "1.3.6.1.2.1.4.21.1.7"
ROUTE_MASK = "1.3.6.1.2.1.4.21.1.11"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"


@dataclass
class CollectorService:
    database: Database
    settings: Settings

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.settings.enable_live and self.settings.poll_interval_s > 0:
            self._thread = threading.Thread(target=self._loop, name="nms-collector", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)

    def _loop(self) -> None:
        while not self._stop.wait(self.settings.poll_interval_s):
            try:
                self.run_once()
            except Exception:
                # run_once persists a run error; the next cycle may still succeed.
                continue

    def run_once(self, device_id: str | None = None) -> dict:
        if not self.settings.enable_live:
            raise RuntimeError("live collection is disabled; set NMS_ENABLE_LIVE=1 on DEV")
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("a collection cycle is already running")
        started = utc_now()
        details: list[dict] = []
        with self.database.connect() as db:
            run_id = db.execute(
                "INSERT INTO collector_runs(started_at,state,details) VALUES (?,'running','[]')",
                (started,),
            ).lastrowid
        try:
            rows = self.database.rows(
                "SELECT * FROM devices WHERE role IN ('router-candidate','test-host') AND (? IS NULL OR device_id=?)",
                (device_id, device_id),
            )
            if not rows:
                raise ValueError("no matching managed device")
            snmp = SnmpClient(self.settings.snmp_community, self.settings.snmp_timeout_s) if self.settings.snmp_community else None
            for device in rows:
                try:
                    if device["role"] == "router-candidate":
                        if not snmp:
                            details.append({"device_id": device["device_id"], "status": "unavailable",
                                            "reason": "NMS_SNMP_COMMUNITY is not configured"})
                            continue
                        outcome = self._router(snmp, device)
                    else:
                        outcome = self._host(device)
                    details.append({"device_id": device["device_id"], **outcome})
                except (SnmpError, OSError, ValueError) as exc:
                    details.append({"device_id": device["device_id"], "status": "error",
                                    "reason": str(exc)[:180]})
            state = "succeeded" if all(x["status"] == "ok" for x in details) else "partial"
            return {"run_id": run_id, "started_at": started, "state": state, "details": details}
        except BaseException as exc:
            state = "failed"
            details.append({"status": "error", "reason": str(exc)[:180]})
            raise
        finally:
            with self.database.connect() as db:
                db.execute(
                    "UPDATE collector_runs SET finished_at=?,state=?,details=? WHERE run_id=?",
                    (utc_now(), state, json.dumps(details, ensure_ascii=False), run_id),
                )
            self._lock.release()

    def _host(self, device: dict) -> dict:
        result = ping.probe(device["address"], self.settings.ping_timeout_s)
        saved = record_ping(self.database, device["device_id"], device["address"],
                            utc_now(), result, self.settings.fault_threshold)
        return {"status": result["query_status"], "ping_sample_id": saved["sample_id"],
                "active_event_id": saved["active_event_id"], "loss_pct": result["loss_pct"]}

    def _router(self, snmp: SnmpClient, device: dict) -> dict:
        device_id, address = device["device_id"], device["address"]
        sampled_at = utc_now()
        uptime = snmp.get(address, SYS_UPTIME).value
        record_snmp_access_risk(self.database, device_id, sampled_at,
                                self.settings.snmp_community in {"public", "private"})
        index_values = snmp.walk(address, IF_INDEX, self.settings.max_interfaces)
        if not index_values:
            raise SnmpError("interface index table is empty")
        interfaces: list[dict] = []
        problems: list[str] = []
        for item in index_values:
            if_index = item.value
            if not isinstance(if_index, int) or if_index <= 0:
                problems.append("invalid interface index")
                continue
            fields = {}
            for name, oid in (("description", IF_DESCR), ("admin_status", IF_ADMIN),
                              ("oper_status", IF_OPER), ("high_speed", IF_HIGH_SPEED),
                              ("in_octets", IF_IN), ("out_octets", IF_OUT),
                              ("discontinuity", IF_DISCONTINUITY)):
                try:
                    fields[name] = snmp.get(address, f"{oid}.{if_index}").value
                except SnmpError:
                    fields[name] = None
                    if name in {"admin_status", "oper_status", "in_octets", "out_octets"}:
                        problems.append(f"interface {if_index}: {name} unavailable")
            speed = (int(fields["high_speed"]) * 1_000_000
                     if isinstance(fields["high_speed"], int) and fields["high_speed"] > 0 else None)
            if speed is None:
                try:
                    raw_speed = snmp.get(address, f"{IF_SPEED}.{if_index}").value
                    speed = raw_speed if isinstance(raw_speed, int) and raw_speed > 0 else None
                except SnmpError:
                    pass
            interface_id = f"{device_id}:{if_index}"
            interface = {
                "interface_id": interface_id, "device_id": device_id, "if_index": if_index,
                "description": fields["description"],
                "admin_status": _status(fields["admin_status"]),
                "oper_status": _status(fields["oper_status"]),
                "reference_speed_bps": speed,
                "query_status": "ok" if fields["admin_status"] is not None and fields["oper_status"] is not None else "error",
                "sampled_at": sampled_at, "data_source": "live",
            }
            with self.database.connect() as db:
                db.execute(
                    """INSERT INTO interfaces
                       (interface_id,device_id,if_index,description,admin_status,oper_status,
                        reference_speed_bps,query_status,sampled_at,data_source)
                       VALUES (:interface_id,:device_id,:if_index,:description,:admin_status,:oper_status,
                        :reference_speed_bps,:query_status,:sampled_at,:data_source)
                       ON CONFLICT(interface_id) DO UPDATE SET
                       description=excluded.description,admin_status=excluded.admin_status,
                       oper_status=excluded.oper_status,reference_speed_bps=excluded.reference_speed_bps,
                       query_status=excluded.query_status,sampled_at=excluded.sampled_at,
                       data_source=excluded.data_source""", interface,
                )
            interfaces.append(interface)
            record_interface_status(
                self.database, interface_id, device_id, sampled_at,
                interface["admin_status"], interface["oper_status"],
                interface["query_status"], self.settings.fault_threshold,
            )
            metric = record_counter(
                self.database, interface_id,
                fields["in_octets"] if isinstance(fields["in_octets"], int) else None,
                fields["out_octets"] if isinstance(fields["out_octets"], int) else None,
                sampled_at, uptime if isinstance(uptime, int) else None,
                fields["discontinuity"] if isinstance(fields["discontinuity"], int) else None,
                query_status="ok" if isinstance(fields["in_octets"], int) and isinstance(fields["out_octets"], int) else "error",
                reference_speed_bps=speed,
            )
            record_utilization_risk(self.database, device_id, interface_id, metric["metric_id"])
        routes: list[dict] = []
        route_complete = True
        try:
            for item in snmp.walk(address, ROUTE_DEST, self.settings.max_routes):
                suffix = ".".join(str(x) for x in item.oid[len(tuple(int(x) for x in ROUTE_DEST.split("."))):])
                if not isinstance(item.value, str):
                    problems.append("route table contains invalid destination or index")
                    route_complete = False
                    continue
                try:
                    destination = str(ipaddress.IPv4Address(item.value))
                    indexed_destination = str(ipaddress.IPv4Address(suffix))
                except ipaddress.AddressValueError:
                    problems.append("route table contains invalid destination or index")
                    route_complete = False
                    continue
                if destination != indexed_destination:
                    problems.append("route table destination does not match its index")
                    route_complete = False
                    continue
                try:
                    mask = snmp.get(address, f"{ROUTE_MASK}.{suffix}").value
                    next_hop = snmp.get(address, f"{ROUTE_NEXT_HOP}.{suffix}").value
                    if_index = snmp.get(address, f"{ROUTE_IF_INDEX}.{suffix}").value
                except SnmpError:
                    problems.append(f"route {suffix}: fields unavailable")
                    route_complete = False
                    continue
                routes.append({"route_id": f"{device_id}:{destination}", "device_id": device_id,
                               "destination": destination, "mask": mask, "next_hop": next_hop,
                               "interface_id": f"{device_id}:{if_index}" if if_index else None,
                               "sampled_at": sampled_at, "data_source": "live"})
        except SnmpError:
            problems.append("route table unavailable")
            route_complete = False
        if route_complete and not routes:
            problems.append("route table returned no entries")
            route_complete = False
        if len(routes) >= self.settings.max_routes:
            problems.append("route walk reached configured limit")
            route_complete = False
        with self.database.connect() as db:
            db.execute("UPDATE devices SET data_source='live',note=? WHERE device_id=?",
                       ("地址有实时 SNMP 响应；物理设备身份仍未归并确认", device_id))
            if route_complete:
                for route in routes:
                    db.execute(
                        """INSERT INTO routes(route_id,device_id,destination,mask,next_hop,
                           interface_id,sampled_at,data_source)
                           VALUES (:route_id,:device_id,:destination,:mask,:next_hop,
                           :interface_id,:sampled_at,:data_source)
                           ON CONFLICT(route_id) DO UPDATE SET mask=excluded.mask,
                           next_hop=excluded.next_hop,interface_id=excluded.interface_id,
                           sampled_at=excluded.sampled_at,data_source=excluded.data_source""", route,
                    )
                keep = {route["route_id"] for route in routes}
                for old in db.execute("SELECT route_id FROM routes WHERE device_id=?", (device_id,)).fetchall():
                    if old["route_id"] not in keep:
                        db.execute("DELETE FROM routes WHERE route_id=?", (old["route_id"],))
        if len(index_values) >= self.settings.max_interfaces:
            problems.append("interface walk reached configured limit")
        discovery = read_inventory(snmp, self.database, device_id, address, sampled_at,
                                   {x["interface_id"] for x in interfaces}, self.settings.max_addresses)
        if not problems:
            save_snapshot(self.database, device_id, sampled_at, interfaces, routes,
                          addresses=discovery["addresses"] if discovery["query_status"] == "ok" else None)
        return {"status": "partial" if problems else "ok", "interfaces": len(interfaces),
                "routes": len(routes), "problems": problems,
                "network_discovery": {k: v for k, v in discovery.items() if k != "addresses"}}


def _status(value: object) -> str | None:
    return {1: "up", 2: "down", 3: "testing"}.get(value) if isinstance(value, int) else None
