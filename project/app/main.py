from __future__ import annotations

import asyncio
import csv
import hmac
import io
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from app.core.config import Settings
from app.core.db import Database, decode, utc_now
from app.modules.fault_config import acknowledge_event, diff_snapshots
from app.modules.iperf_tasks import IperfTaskManager, TaskConflict, TaskUnavailable
from app.modules.perf_accounting import pair_traffic_report, traffic_report
from app.modules.discovery import topology as discovered_topology
from app.modules.diagnostics import diagnose
from app.modules.security import check_tcp, record_service_check
from app.service import CollectorService


WEB = Path(__file__).parent / "web"


def _source(data: object) -> str:
    items = data.get("items", []) if isinstance(data, dict) else []
    sources = {item.get("data_source") for item in items if isinstance(item, dict) and item.get("data_source")}
    return next(iter(sources)) if len(sources) == 1 else "mixed" if sources else "none"


def _event_view(item: dict, evidence_limit: int | None = None, database: Database | None = None) -> dict:
    if database and item.get("interface_id"):
        interface = database.row("SELECT description,sampled_at,data_source FROM interfaces WHERE interface_id=?",
                                 (item["interface_id"],))
        item["interface_context"] = interface
        item["impact_note"] = "接口管理开启、运行关闭触发状态告警；业务用途和实际影响尚未确认，不能据此认定业务中断。"
    evidence = decode(item["evidence_sample_ids"], [])
    item["evidence_count"] = len(evidence)
    item["evidence_sample_ids"] = evidence if evidence_limit is None else evidence[:evidence_limit]
    item["evidence_truncated"] = len(item["evidence_sample_ids"]) < len(evidence)
    return item


def reply(data: object, *, data_source: str | None = None, status_code: int = 200) -> JSONResponse:
    return JSONResponse({"request_id": str(uuid4()), "status": "ok",
                         "data_source": data_source or _source(data),
                         "generated_at": utc_now(), "data": data, "error": None},
                        status_code=status_code)


def _list(database: Database, table: str, filters: dict[str, object],
          limit: int, offset: int, order_by: str, *,
          time_column: str | None = None, start: str | None = None,
          end: str | None = None) -> dict:
    # All table and column names come from fixed endpoint definitions, never user input.
    conditions = [f"{name}=?" for name, value in filters.items() if value is not None]
    params = [value for value in filters.values() if value is not None]
    if time_column and start is not None and end is not None:
        conditions.extend((f"{time_column}>=?", f"{time_column}<?"))
        params.extend((start, end))
    condition = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    total = database.row(f"SELECT COUNT(*) AS count FROM {table}{condition}", tuple(params))["count"]
    items = database.rows(
        f"SELECT * FROM {table}{condition} ORDER BY {order_by} LIMIT ? OFFSET ?",
        tuple(params) + (limit, offset),
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _utc(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except ValueError as exc:
        raise HTTPException(422, "time must be ISO 8601 with timezone") from exc


def _window(from_time: str | None, to_time: str | None) -> tuple[str | None, str | None]:
    if (from_time is None) != (to_time is None):
        raise HTTPException(422, "from and to must be supplied together")
    if from_time is None:
        return None, None
    start, end = _utc(from_time), _utc(to_time)
    if start >= end:
        raise HTTPException(422, "from must precede to")
    return start, end


class CollectRequest(BaseModel):
    device_id: str | None = None


class BaselineRequest(BaseModel):
    device_id: str
    port: int = Field(ge=1, le=65535)
    allowed: bool
    note: str = Field(default="", max_length=200)


class ProbeRequest(BaseModel):
    device_id: str
    port: int = Field(ge=1, le=65535)


class IperfRequest(BaseModel):
    client_device_id: str
    server_device_id: str
    duration_s: int = Field(default=2, ge=1, le=3)
    target_bps: int = Field(default=10_000_000, ge=1, le=10_000_000)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    database = Database(settings.db_path)
    service = CollectorService(database, settings)
    tasks = IperfTaskManager(database, settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        database.initialize()
        service.start()
        yield
        service.stop()

    app = FastAPI(title="第 2 组网络管理系统", version="0.3.0", lifespan=lifespan)
    app.state.database = database
    app.state.collector = service
    app.state.tasks = tasks

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        code = {401: "UNAUTHORIZED", 404: "NOT_FOUND", 409: "CONFLICT",
                422: "INVALID_REQUEST", 503: "NOT_READY"}.get(exc.status_code, "REQUEST_ERROR")
        return JSONResponse({"request_id": str(uuid4()), "status": "error", "data_source": "none",
                             "generated_at": utc_now(), "data": None,
                             "error": {"code": code, "message": str(exc.detail), "details": {}}},
                            status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, exc: RequestValidationError):
        return JSONResponse({"request_id": str(uuid4()), "status": "error", "data_source": "none",
                             "generated_at": utc_now(), "data": None,
                             "error": {"code": "INVALID_REQUEST", "message": "请求参数无效",
                                       "details": {"fields": [".".join(map(str, x["loc"])) for x in exc.errors()]}}},
                            status_code=422)

    async def require_write(x_admin_token: str | None = Header(default=None)) -> None:
        token = os.environ.get("NMS_API_TOKEN")
        if not token:
            raise HTTPException(503, "write operations require NMS_API_TOKEN")
        if not x_admin_token or not hmac.compare_digest(token, x_admin_token):
            raise HTTPException(401, "invalid admin token")

    @app.get("/api/v1/auth/status", dependencies=[Depends(require_write)])
    async def auth_status():
        return reply({"authenticated": True}, data_source="none")

    @app.get("/static/auth.js", include_in_schema=False)
    async def auth_javascript():
        return Response((WEB / "auth.js").read_text(encoding="utf-8"),
                        media_type="application/javascript; charset=utf-8")

    @app.get("/")
    async def index():
        return HTMLResponse((WEB / "index.html").read_text(encoding="utf-8"))

    @app.get("/static/style.css", include_in_schema=False)
    async def style():
        return Response((WEB / "style.css").read_text(encoding="utf-8"),
                        media_type="text/css; charset=utf-8")

    @app.get("/static/views.js", include_in_schema=False)
    async def views_javascript():
        return Response((WEB / "views.js").read_text(encoding="utf-8"),
                        media_type="application/javascript; charset=utf-8")

    @app.get("/static/app.js", include_in_schema=False)
    async def javascript():
        return Response((WEB / "app.js").read_text(encoding="utf-8"),
                        media_type="application/javascript; charset=utf-8")

    @app.get("/events/{event_id}", include_in_schema=False)
    async def event_page(event_id: int):
        exists = database.row("SELECT event_id FROM events WHERE event_id=?", (event_id,))
        return HTMLResponse((WEB / "alert-detail.html").read_text(encoding="utf-8"),
                            status_code=200 if exists else 404)

    @app.get("/risks/{risk_id}", include_in_schema=False)
    async def risk_page(risk_id: int):
        exists = database.row("SELECT risk_id FROM risks WHERE risk_id=?", (risk_id,))
        return HTMLResponse((WEB / "alert-detail.html").read_text(encoding="utf-8"),
                            status_code=200 if exists else 404)

    @app.get("/static/alert-detail.js", include_in_schema=False)
    async def alert_javascript():
        return Response((WEB / "alert-detail.js").read_text(encoding="utf-8"),
                        media_type="application/javascript; charset=utf-8")

    @app.get("/api/v1/health")
    async def health():
        return reply({"database": "ok", "live_enabled": settings.enable_live,
                      "poll_interval_s": settings.poll_interval_s,
                      "snmp_configured": bool(settings.snmp_community),
                      "service_probe_enabled": settings.enable_service_probe,
                      "iperf3_enabled": settings.enable_iperf3,
                      "iperf3_allowed_devices": list(settings.iperf_allowed_devices)},
                     data_source="none")

    @app.get("/api/v1/overview")
    async def overview():
        counts = {}
        for table in ("devices", "interfaces", "routes", "ping_samples", "metric_samples", "events", "risks"):
            counts[table] = database.row(f"SELECT COUNT(*) AS n FROM {table}")["n"]
        counts["active_events"] = database.row("SELECT COUNT(*) AS n FROM events WHERE state='active'")["n"]
        counts["open_risks"] = database.row("SELECT COUNT(*) AS n FROM risks WHERE state='open'")["n"]
        counts["unresolved_risks"] = database.row("SELECT COUNT(*) AS n FROM risks WHERE state IN ('open','acknowledged')")["n"]
        run = database.row("SELECT * FROM collector_runs ORDER BY run_id DESC LIMIT 1")
        if run:
            run["details"] = decode(run["details"], [])
        return reply({"counts": counts, "latest_collector_run": run,
                      "notice": "设备初始清单来自课程文档；实际采样需查看 data_source 与时间。"},
                     data_source="mixed")

    @app.get("/api/v1/devices")
    async def devices(device_id: str | None = None, limit: int = Query(100, ge=1, le=500),
                offset: int = Query(0, ge=0)):
        return reply(_list(database, "devices", {"device_id": device_id}, limit, offset, "device_id"))

    @app.get("/api/v1/interfaces")
    async def interfaces(device_id: str | None = None, limit: int = Query(100, ge=1, le=500),
                   offset: int = Query(0, ge=0)):
        return reply(_list(database, "interfaces", {"device_id": device_id}, limit, offset, "device_id,if_index"))

    @app.get("/api/v1/routes")
    async def routes(device_id: str | None = None, limit: int = Query(100, ge=1, le=500),
               offset: int = Query(0, ge=0)):
        return reply(_list(database, "routes", {"device_id": device_id}, limit, offset, "device_id,destination"))

    @app.get("/api/v1/topology")
    async def topology():
        result = discovered_topology(database)
        sources = {e["data_source"] for e in result["edges"]}
        return reply(result, data_source=next(iter(sources)) if len(sources) == 1 else "mixed")

    @app.get("/api/v1/network-discoveries")
    async def network_discoveries(device_id: str | None = None, limit: int = Query(100, ge=1, le=500),
                                  offset: int = Query(0, ge=0), latest: bool = False):
        if latest:
            where = "d.discovery_id=(SELECT n.discovery_id FROM network_discoveries n WHERE n.device_id=d.device_id ORDER BY n.sampled_at DESC,n.discovery_id DESC LIMIT 1)"
            params = ()
            if device_id is not None:
                where += " AND d.device_id=?"
                params = (device_id,)
            total = database.row(f"SELECT COUNT(*) AS n FROM network_discoveries d WHERE {where}", params)["n"]
            items = database.rows(f"SELECT d.* FROM network_discoveries d WHERE {where} ORDER BY d.device_id LIMIT ? OFFSET ?", params + (limit, offset))
            data = {"items": items, "total": total, "limit": limit, "offset": offset}
        else:
            data = _list(database, "network_discoveries", {"device_id": device_id}, limit, offset, "discovery_id DESC")
        for item in data["items"]:
            item["issues"] = decode(item["issues"], [])
        return reply(data)

    @app.get("/api/v1/interface-addresses")
    async def interface_addresses(device_id: str | None = None, discovery_id: int | None = None,
                                  limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), latest: bool = False):
        if latest:
            if discovery_id is not None:
                raise HTTPException(422, "latest and discovery_id cannot be combined")
            where = "a.discovery_id=(SELECT n.discovery_id FROM network_discoveries n WHERE n.device_id=a.device_id ORDER BY n.sampled_at DESC,n.discovery_id DESC LIMIT 1)"
            params = ()
            if device_id is not None:
                where += " AND a.device_id=?"
                params = (device_id,)
            total = database.row(f"SELECT COUNT(*) AS n FROM interface_addresses a WHERE {where}", params)["n"]
            items = database.rows(f"SELECT a.* FROM interface_addresses a WHERE {where} ORDER BY a.device_id,a.if_index,a.address LIMIT ? OFFSET ?", params + (limit, offset))
            return reply({"items": items, "total": total, "limit": limit, "offset": offset})
        return reply(_list(database, "interface_addresses", {"device_id": device_id, "discovery_id": discovery_id},
                           limit, offset, "address_id DESC"))

    @app.get("/api/v1/config-snapshots")
    async def config_snapshots(device_id: str | None = None,
                         from_time: str | None = Query(default=None, alias="from"),
                         to_time: str | None = Query(default=None, alias="to"),
                         limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
        start, end = _window(from_time, to_time)
        data = _list(database, "config_snapshots", {"device_id": device_id}, limit, offset,
                     "snapshot_id DESC", time_column="sampled_at", start=start, end=end)
        for item in data["items"]:
            item["content"] = decode(item["content"])
        return reply(data)

    @app.get("/api/v1/config-diff")
    async def config_diff(before_id: int, after_id: int):
        try:
            result = diff_snapshots(database, before_id, after_id)
            return reply(result, data_source=result["data_source"])
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/v1/events")
    async def events(device_id: str | None = None, state: str | None = None,
               from_time: str | None = Query(default=None, alias="from"),
               to_time: str | None = Query(default=None, alias="to"),
               limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
               evidence_limit: int | None = Query(None, ge=0, le=50)):
        if state not in (None, "active", "recovered"):
            raise HTTPException(422, "invalid event state")
        start, end = _window(from_time, to_time)
        data = _list(database, "events", {"device_id": device_id, "state": state}, limit, offset,
                     "event_id DESC", time_column="last_seen", start=start, end=end)
        for item in data["items"]:
            _event_view(item, evidence_limit, database)
        return reply(data)

    @app.get("/api/v1/events/{event_id}")
    async def event_detail(event_id: int, evidence_limit: int = Query(3, ge=0, le=50)):
        item = database.row("SELECT * FROM events WHERE event_id=?", (event_id,))
        if item is None:
            raise HTTPException(404, "event not found")
        return reply(_event_view(item, evidence_limit, database), data_source=item["data_source"])

    @app.get("/api/v1/events/{event_id}/evidence")
    async def event_evidence(event_id: int, limit: int = Query(20, ge=1, le=100),
                             offset: int = Query(0, ge=0)):
        event = database.row("SELECT * FROM events WHERE event_id=?", (event_id,))
        if event is None:
            raise HTTPException(404, "event not found")
        evidence = decode(event["evidence_sample_ids"], [])
        # Return newest references first without constructing an unbounded SQL IN clause.
        ids = list(reversed(evidence))[offset:offset + limit]
        samples = []
        if ids and event["type"] in {"ping_unreachable", "interface_oper_down"}:
            placeholders = ",".join("?" for _ in ids)
            if event["type"] == "ping_unreachable":
                samples = database.rows(
                    f"SELECT * FROM ping_samples WHERE sample_id IN ({placeholders}) "
                    "AND device_id=? AND data_source=?", tuple(ids) + (event["device_id"], event["data_source"]))
            else:
                samples = database.rows(
                    f"SELECT * FROM interface_status_samples WHERE sample_id IN ({placeholders}) "
                    "AND interface_id=? AND data_source=?", tuple(ids) + (event["interface_id"], event["data_source"]))
        by_id = {sample["sample_id"]: sample for sample in samples}
        items = [dict(by_id[sample_id], sample_available=True) if sample_id in by_id else
                 {"sample_id": sample_id, "sample_available": False,
                  "note": "原始样本缺失或不属于此告警；保留证据编号，不推断测量结果。"}
                 for sample_id in ids]
        return reply({"items": items, "total": len(evidence), "limit": limit, "offset": offset,
                      "event_id": event_id, "kind": event["type"], "order": "newest_first"},
                     data_source=event["data_source"])

    @app.get("/api/v1/diagnostics")
    async def diagnostics(device_id: str | None = None):
        if device_id and not database.row("SELECT device_id FROM devices WHERE device_id=?", (device_id,)):
            raise HTTPException(404, "device not found")
        return reply(diagnose(database, device_id))

    @app.post("/api/v1/events/{event_id}/acknowledge", dependencies=[Depends(require_write)])
    async def acknowledge_fault_event(event_id: int):
        result = acknowledge_event(database, event_id)
        if result is None:
            raise HTTPException(404, "event not found")
        result["evidence_sample_ids"] = decode(result["evidence_sample_ids"], [])
        return reply(result, data_source=result["data_source"])

    @app.get("/api/v1/ping-samples")
    async def ping_samples(device_id: str | None = None, limit: int = Query(100, ge=1, le=500),
                     offset: int = Query(0, ge=0),
                     from_time: str | None = Query(default=None, alias="from"),
                     to_time: str | None = Query(default=None, alias="to")):
        start, end = _window(from_time, to_time)
        return reply(_list(database, "ping_samples", {"device_id": device_id}, limit, offset,
                           "sample_id DESC", time_column="sampled_at", start=start, end=end))

    @app.get("/api/v1/counter-samples")
    async def counter_samples(interface_id: str | None = None, limit: int = Query(100, ge=1, le=500),
                        offset: int = Query(0, ge=0),
                        from_time: str | None = Query(default=None, alias="from"),
                        to_time: str | None = Query(default=None, alias="to")):
        start, end = _window(from_time, to_time)
        return reply(_list(database, "counter_samples", {"interface_id": interface_id}, limit, offset,
                           "sample_id DESC", time_column="sampled_at", start=start, end=end))

    @app.get("/api/v1/interface-status-samples")
    async def interface_status_samples(interface_id: str | None = None,
                                       limit: int = Query(100, ge=1, le=500),
                                       offset: int = Query(0, ge=0),
                                       from_time: str | None = Query(default=None, alias="from"),
                                       to_time: str | None = Query(default=None, alias="to")):
        start, end = _window(from_time, to_time)
        return reply(_list(database, "interface_status_samples", {"interface_id": interface_id},
                           limit, offset, "sample_id DESC", time_column="sampled_at",
                           start=start, end=end))

    @app.get("/api/v1/metrics")
    async def metrics(interface_id: str | None = None, limit: int = Query(100, ge=1, le=500),
                offset: int = Query(0, ge=0),
                from_time: str | None = Query(default=None, alias="from"),
                to_time: str | None = Query(default=None, alias="to"),
                kind: str | None = None):
        if kind not in (None, "interface_rate"):
            raise HTTPException(422, "metric kind must be interface_rate")
        start, end = _window(from_time, to_time)
        data = _list(database, "metric_samples", {"interface_id": interface_id}, limit, offset,
                     "metric_id DESC", time_column="sampled_at", start=start, end=end)
        for item in data["items"]:
            item["valid"] = bool(item["valid"])
            item["source_sample_ids"] = decode(item["source_sample_ids"], [])
        return reply(data)

    @app.get("/api/v1/traffic")
    async def traffic(interface_id: str, from_time: str = Query(alias="from"),
                to_time: str = Query(alias="to"), direction: str = "both"):
        start, end = _window(from_time, to_time)
        if direction not in ("in", "out", "both"):
            raise HTTPException(422, "direction must be in, out or both")
        if not database.row("SELECT interface_id FROM interfaces WHERE interface_id=?", (interface_id,)):
            raise HTTPException(404, "interface not found")
        report = traffic_report(database, interface_id, start, end)
        report["direction"] = direction
        if direction == "in":
            report["out_bytes"] = None
        if direction == "out":
            report["in_bytes"] = None
        return reply(report, data_source=report["data_source"])

    @app.get("/api/v1/traffic/export.csv")
    async def traffic_csv(interface_id: str, from_time: str = Query(alias="from"),
                    to_time: str = Query(alias="to"), direction: str = "both"):
        start, end = _window(from_time, to_time)
        if direction not in ("in", "out", "both"):
            raise HTTPException(422, "direction must be in, out or both")
        if not database.row("SELECT interface_id FROM interfaces WHERE interface_id=?", (interface_id,)):
            raise HTTPException(404, "interface not found")
        report = traffic_report(database, interface_id, start, end)
        file = io.StringIO()
        writer = csv.writer(file)
        attribution = report["attribution"]
        writer.writerow(["interface_id", "from", "to", "direction", "bytes", "coverage_pct", "complete", "data_source",
                         "device_id", "networks", "mapping_status", "exact_host_pair"])
        for selected in (("in", "out") if direction == "both" else (direction,)):
            writer.writerow([interface_id, start, end, selected, report[f"{selected}_bytes"],
                             report["coverage_pct"], report["complete"], report["data_source"],
                             attribution["device_id"], ";".join(attribution["networks"]),
                             attribution["mapping_status"], attribution["exact_host_pair"]])
        return Response("\ufeff" + file.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=traffic-report.csv"})

    @app.get("/api/v1/traffic/pairs")
    async def traffic_pairs(from_time: str = Query(alias="from"), to_time: str = Query(alias="to")):
        start, end = _window(from_time, to_time)
        return reply(pair_traffic_report(database, start, end))

    @app.get("/api/v1/traffic/pairs/export.csv")
    async def traffic_pairs_csv(from_time: str = Query(alias="from"), to_time: str = Query(alias="to")):
        start, end = _window(from_time, to_time)
        report = pair_traffic_report(database, start, end)
        file = io.StringIO()
        writer = csv.writer(file)
        writer.writerow(["client_device_id", "server_device_id", "from", "to", "received_bytes",
                         "task_count", "data_source", "task_ids", "scope"])
        for item in report["items"]:
            writer.writerow([item["client_device_id"], item["server_device_id"], start, end, item["received_bytes"],
                             item["task_count"], item["data_source"], ";".join(item["task_ids"]), report["scope"]])
        return Response("\ufeff" + file.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=pair-traffic-report.csv"})

    @app.get("/api/v1/service-baselines")
    async def service_baselines(device_id: str | None = None, limit: int = Query(100, ge=1, le=500),
                          offset: int = Query(0, ge=0)):
        data = _list(database, "service_baselines", {"device_id": device_id}, limit, offset, "device_id,port")
        for item in data["items"]:
            item["allowed"] = bool(item["allowed"])
        return reply(data)

    @app.get("/api/v1/tasks")
    async def list_tasks(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
        data = _list(database, "tasks", {}, limit, offset, "created_at DESC")
        for item in data["items"]:
            for key in ("params", "result", "error"):
                item[key] = decode(item[key])
        return reply(data)

    @app.post("/api/v1/tasks/iperf3", dependencies=[Depends(require_write)])
    async def start_iperf3(body: IperfRequest):
        try:
            task = tasks.create(body.client_device_id, body.server_device_id,
                                body.duration_s, body.target_bps)
        except TaskUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        except TaskConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return reply(task, data_source="live", status_code=202)

    @app.get("/api/v1/tasks/{task_id}")
    async def get_task(task_id: str):
        task = tasks.get(task_id)
        if not task:
            raise HTTPException(404, "task not found")
        return reply(task, data_source=task["data_source"])

    @app.post("/api/v1/tasks/{task_id}/cancel", dependencies=[Depends(require_write)])
    async def cancel_task(task_id: str):
        task = tasks.cancel(task_id)
        if not task:
            raise HTTPException(404, "task not found")
        return reply(task, data_source=task["data_source"])

    @app.post("/api/v1/service-baselines", dependencies=[Depends(require_write)])
    async def put_baseline(body: BaselineRequest):
        if not database.row("SELECT device_id FROM devices WHERE device_id=?", (body.device_id,)):
            raise HTTPException(404, "device not found")
        with database.connect() as db:
            db.execute(
                """INSERT INTO service_baselines(device_id,port,allowed,note,data_source)
                   VALUES (?,?,?,?,'configured')
                   ON CONFLICT(device_id,port) DO UPDATE SET allowed=excluded.allowed,
                   note=excluded.note,data_source=excluded.data_source""",
                (body.device_id, body.port, int(body.allowed), body.note),
            )
        return reply(body.model_dump(), data_source="configured")

    @app.get("/api/v1/service-checks")
    async def service_checks(device_id: str | None = None, limit: int = Query(100, ge=1, le=500),
                       offset: int = Query(0, ge=0)):
        return reply(_list(database, "service_checks", {"device_id": device_id}, limit, offset, "check_id DESC"))

    @app.get("/api/v1/risks")
    async def risks(device_id: str | None = None, state: str | None = None,
              from_time: str | None = Query(default=None, alias="from"),
              to_time: str | None = Query(default=None, alias="to"),
              limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
        if state not in (None, "open", "acknowledged", "resolved"):
            raise HTTPException(422, "invalid risk state")
        start, end = _window(from_time, to_time)
        data = _list(database, "risks", {"device_id": device_id, "state": state}, limit, offset,
                     "risk_id DESC", time_column="last_seen", start=start, end=end)
        for item in data["items"]:
            item["evidence"] = decode(item["evidence"], {})
        return reply(data)

    @app.get("/api/v1/risks/{risk_id}")
    async def risk_detail(risk_id: int):
        item = database.row("SELECT * FROM risks WHERE risk_id=?", (risk_id,))
        if item is None:
            raise HTTPException(404, "risk not found")
        item["evidence"] = decode(item["evidence"], {})
        return reply(item, data_source=item["data_source"])

    @app.post("/api/v1/risks/{risk_id}/acknowledge", dependencies=[Depends(require_write)])
    async def acknowledge_risk(risk_id: int):
        with database.connect() as db:
            found = db.execute("SELECT * FROM risks WHERE risk_id=?", (risk_id,)).fetchone()
            if found is None:
                raise HTTPException(404, "risk not found")
            if found["state"] == "open":
                db.execute("UPDATE risks SET state='acknowledged' WHERE risk_id=?", (risk_id,))
        result = database.row("SELECT * FROM risks WHERE risk_id=?", (risk_id,))
        result["evidence"] = decode(result["evidence"], {})
        return reply(result, data_source=result["data_source"])

    @app.post("/api/v1/security/probe", dependencies=[Depends(require_write)])
    async def security_probe(body: ProbeRequest):
        if not settings.enable_live or not settings.enable_service_probe:
            raise HTTPException(503, "service probe is disabled")
        device = database.row("SELECT * FROM devices WHERE device_id=?", (body.device_id,))
        if not device or device["role"] not in ("router-candidate", "test-host"):
            raise HTTPException(404, "managed target not found")
        if not database.row("SELECT port FROM service_baselines WHERE device_id=? AND port=?",
                            (body.device_id, body.port)):
            raise HTTPException(422, "configure a service baseline before probing")
        state, error = await asyncio.to_thread(check_tcp, device["address"], body.port)
        return reply(record_service_check(database, body.device_id, body.port, state, error),
                     data_source="live")

    @app.get("/api/v1/collector-runs")
    async def collector_runs(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
        data = _list(database, "collector_runs", {}, limit, offset, "run_id DESC")
        for item in data["items"]:
            item["details"] = decode(item["details"], [])
        return reply(data, data_source="live")

    @app.post("/api/v1/collect", dependencies=[Depends(require_write)])
    async def collect(body: CollectRequest):
        try:
            result = await asyncio.to_thread(service.run_once, body.device_id)
        except RuntimeError as exc:
            raise HTTPException(503 if "disabled" in str(exc) else 409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return reply(result, data_source="live")

    return app


app = create_app()
