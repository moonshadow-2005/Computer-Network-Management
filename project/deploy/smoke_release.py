#!/usr/bin/env python3
"""Extract and smoke-test a release bundle without contacting the DEV network."""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import importlib
import importlib.metadata
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from urllib.parse import urlencode

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from deploy.verify_release import extract_release  # noqa: E402


@dataclass
class ASGIResponse:
    status_code: int
    headers: dict[str, str]
    content: bytes

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    def json(self) -> object:
        return json.loads(self.content)


def is_within(path: pathlib.Path, parent: pathlib.Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def canonical_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def validate_runtime_lock(vendor: pathlib.Path, lock_path: pathlib.Path) -> int:
    expected: dict[str, str] = {}
    for line_number, raw_line in enumerate(lock_path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, version = line.partition("==")
        if not separator or not name.strip() or not version.strip():
            raise RuntimeError(f"运行依赖锁文件第 {line_number} 行格式错误。")
        key = canonical_distribution_name(name.strip())
        if key in expected:
            raise RuntimeError(f"运行依赖锁文件重复声明：{name.strip()}。")
        expected[key] = version.strip()

    actual: dict[str, str] = {}
    for distribution in importlib.metadata.distributions(path=[str(vendor)]):
        name = distribution.metadata.get("Name")
        if not name:
            continue
        key = canonical_distribution_name(name)
        version = distribution.version
        if key in actual:
            raise RuntimeError(f"vendor 中存在重复发行包：{name}。")
        actual[key] = version

    if expected != actual:
        missing = sorted(expected.keys() - actual.keys())
        extra = sorted(actual.keys() - expected.keys())
        mismatched = sorted(key for key in expected.keys() & actual.keys()
                            if expected[key] != actual[key])
        details = []
        if missing:
            details.append("缺少=" + ",".join(missing))
        if extra:
            details.append("未锁定=" + ",".join(extra))
        if mismatched:
            details.append("版本不符=" + ",".join(
                f"{key}:{actual[key]}!={expected[key]}" for key in mismatched
            ))
        raise RuntimeError("vendor 与 requirements-runtime.lock 不一致：" + "；".join(details))
    if not expected:
        raise RuntimeError("运行依赖锁文件为空。")
    return len(expected)


async def asgi_get(application: object, path: str, params: dict[str, str] | None = None,
                   *, method: str = "GET", headers: dict[str, str] | None = None) -> ASGIResponse:
    events: list[dict] = []
    request_delivered = False

    async def receive() -> dict:
        nonlocal request_delivered
        if request_delivered:
            return {"type": "http.disconnect"}
        request_delivered = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(event: dict) -> None:
        events.append(event)

    raw_path = path.encode("ascii")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": raw_path,
        "query_string": urlencode(params or {}).encode("ascii"),
        "root_path": "",
        "headers": [(b"host", b"release-smoke.test")]
                   + [(key.lower().encode("ascii"), value.encode("latin1"))
                      for key, value in (headers or {}).items()],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 80),
    }
    await application(scope, receive, send)
    starts = [event for event in events if event["type"] == "http.response.start"]
    require(len(starts) == 1, f"{path} 未生成唯一 ASGI response.start 事件。")
    body = b"".join(event.get("body", b"") for event in events
                    if event["type"] == "http.response.body")
    headers = {key.decode("latin1").lower(): value.decode("latin1")
               for key, value in starts[0].get("headers", [])}
    return ASGIResponse(starts[0]["status"], headers, body)


async def smoke(extracted: pathlib.Path) -> None:
    vendor = extracted / "vendor"
    dependency_count = validate_runtime_lock(vendor, extracted / "requirements-runtime.lock")
    sys.path.insert(0, str(extracted))
    sys.path.insert(0, str(vendor))

    import app

    require(is_within(pathlib.Path(app.__file__), extracted), "应用未从解包目录导入。")
    for name in ("fastapi", "pydantic", "starlette", "uvicorn"):
        module = importlib.import_module(name)
        module_path = pathlib.Path(module.__file__)
        require(is_within(module_path, vendor), f"运行依赖 {name} 未从发布包 vendor/ 导入。")

    runner_env = {key: value for key, value in os.environ.items()
                  if key not in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE")}
    runner_env["PYTHONPATH"] = os.pathsep.join((str(vendor), str(extracted)))
    runner_env["PYTHONDONTWRITEBYTECODE"] = "1"
    runner = subprocess.run(
        [sys.executable, "-S", "-m", "uvicorn", "--version"],
        cwd=extracted,
        env=runner_env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    require(runner.returncode == 0,
            f"发布包的 systemd Uvicorn 启动命令失败：{runner.stderr.strip()}")

    from app.core.config import Settings
    from app.demo import DEMO_DB, load_demo
    from app.main import create_app

    require(is_within(DEMO_DB, extracted), "样例数据库路径越出临时解包目录。")
    require(load_demo(), "临时目录中未能首次装载样例数据。")

    settings = Settings(
        db_path=DEMO_DB,
        enable_live=False,
        poll_interval_s=0,
        enable_service_probe=False,
        enable_iperf3=False,
    )
    application = create_app(settings)
    require(is_within(pathlib.Path(application.state.database.path), extracted),
            "应用数据库路径越出临时解包目录。")
    require(is_within(pathlib.Path(importlib.import_module("app.main").__file__), extracted),
            "API 模块未从解包目录导入。")

    async with application.router.lifespan_context(application):
        health = await asgi_get(application, "/api/v1/health")
        require(health.status_code == 200, "health 请求未返回 HTTP 200。")
        health_data = health.json()["data"]
        require(health_data["database"] == "ok", "health 未确认数据库正常。")
        require(not health_data["live_enabled"], "冒烟环境意外启用了实时采集。")
        require(not health_data["service_probe_enabled"], "冒烟环境意外启用了服务探测。")
        require(not health_data["iperf3_enabled"], "冒烟环境意外启用了 iperf3。")

        page = await asgi_get(application, "/")
        require(page.status_code == 200 and "text/html" in page.headers.get("content-type", ""),
                "首页未返回 HTML。")
        require("/static/style.css" in page.text and "/static/app.js" in page.text,
                "首页未引用发布包静态资源。")
        for path, media_type in (("/static/style.css", "text/css"),
                                 ("/static/app.js", "javascript")):
            asset = await asgi_get(application, path)
            require(asset.status_code == 200, f"静态资源 {path} 未返回 HTTP 200。")
            require(media_type in asset.headers.get("content-type", ""),
                    f"静态资源 {path} 内容类型错误。")
            require(bool(asset.content), f"静态资源 {path} 内容为空。")
            if path == "/static/app.js":
                require("取消任务" in asset.text
                        and "encodeURIComponent(x.task_id)" in asset.text
                        and "/cancel" in asset.text
                        and "Promise.allSettled" in asset.text
                        and "refreshRequestId" in asset.text
                        and "部分数据读取失败" in asset.text,
                        "发布包前端缺少任务取消、独立数据区块错误提示或过时刷新保护。")

        devices_response = await asgi_get(
            application, "/api/v1/devices", {"device_id": "sample-router"}
        )
        require(devices_response.status_code == 200, "设备 API 未返回 HTTP 200。")
        devices_payload = devices_response.json()
        require(devices_payload["data_source"] == "sample", "设备数据来源不是 sample。")
        devices = devices_payload["data"]["items"]
        require(len(devices) == 1 and devices[0]["device_id"] == "sample-router",
                "构造样例设备未能从 API 读取。")

        events_response = await asgi_get(
            application, "/api/v1/events", {"device_id": "sample-host-1"}
        )
        events_payload = events_response.json()
        events = events_payload["data"]["items"]
        require(events_response.status_code == 200 and events_payload["data_source"] == "sample",
                "故障事件 API 状态或来源错误。")
        require(len(events) == 1 and events[0]["state"] == "recovered",
                "构造故障恢复事件未能从 API 读取。")
        previous_token = os.environ.get("NMS_API_TOKEN")
        os.environ["NMS_API_TOKEN"] = "release-smoke-test-token"
        try:
            event_path = f"/api/v1/events/{events[0]['event_id']}/acknowledge"
            ack_response = await asgi_get(
                application, event_path, method="POST",
                headers={"X-Admin-Token":"release-smoke-test-token"},
            )
            acked = ack_response.json()["data"]
            require(ack_response.status_code == 200 and acked["acknowledged_at"]
                    and acked["state"] == "recovered",
                    "故障确认未保留恢复状态或未写入确认时间。")
            repeat_response = await asgi_get(
                application, event_path, method="POST",
                headers={"X-Admin-Token":"release-smoke-test-token"},
            )
            require(repeat_response.json()["data"]["acknowledged_at"] == acked["acknowledged_at"],
                    "重复确认改写了首次确认时间。")
        finally:
            if previous_token is None:
                os.environ.pop("NMS_API_TOKEN", None)
            else:
                os.environ["NMS_API_TOKEN"] = previous_token
        pings_response = await asgi_get(
            application, "/api/v1/ping-samples", {"device_id": "sample-host-1"}
        )
        pings_payload = pings_response.json()
        pings = pings_payload["data"]["items"]
        require(pings_response.status_code == 200 and len(pings) == 5
                and all(item["data_source"] == "sample" for item in pings),
                "构造 ping 历史未能从 API 读取。")

        snapshots_response = await asgi_get(
            application, "/api/v1/config-snapshots", {"device_id": "sample-router"}
        )
        snapshots_payload = snapshots_response.json()
        snapshots = snapshots_payload["data"]["items"]
        require(snapshots_response.status_code == 200 and len(snapshots) == 2,
                "构造配置快照未能从 API 读取。")
        before = snapshots[-1]
        after = snapshots[0]
        diff_response = await asgi_get(application, "/api/v1/config-diff", {
            "before_id": str(before["snapshot_id"]),
            "after_id": str(after["snapshot_id"]),
        })
        diff_payload = diff_response.json()
        changes = diff_payload["data"]["changes"]
        require(diff_response.status_code == 200 and diff_payload["data_source"] == "sample",
                "配置快照差异 API 状态或来源错误。")
        require(any(item["kind"] == "interfaces" and item["change"] == "changed" for item in changes)
                and any(item["kind"] == "routes" and item["change"] == "added" for item in changes),
                "配置快照差异未返回接口变化和新增路由。")

        metrics_response = await asgi_get(
            application, "/api/v1/metrics", {"interface_id": "sample-router:1"}
        )
        metrics_payload = metrics_response.json()
        metrics = metrics_payload["data"]["items"]
        require(metrics_response.status_code == 200 and len(metrics) == 2
                and sum(item["valid"] for item in metrics) == 1,
                "构造接口性能指标未能区分有效与无效样本。")
        valid_metric = next(item for item in metrics if item["valid"])
        require(valid_metric["in_delta_bytes"] == "125"
                and valid_metric["out_delta_bytes"] == "250",
                "有效接口性能样本的方向字节与预期不符。")

        baselines_response = await asgi_get(
            application, "/api/v1/service-baselines", {"device_id": "sample-host-1"}
        )
        checks_response = await asgi_get(
            application, "/api/v1/service-checks", {"device_id": "sample-host-1"}
        )
        risks_response = await asgi_get(
            application, "/api/v1/risks", {"device_id": "sample-host-1"}
        )
        baselines = baselines_response.json()["data"]["items"]
        checks = checks_response.json()["data"]["items"]
        risks = risks_response.json()["data"]["items"]
        require(len(baselines) == 1 and not baselines[0]["allowed"]
                and baselines[0]["data_source"] == "sample",
                "构造服务基线未能从 API 读取。")
        require(len(checks) == 1 and checks[0]["state"] == "open"
                and checks[0]["data_source"] == "sample",
                "构造端口检查未能从 API 读取。")
        require(len(risks) == 1 and risks[0]["state"] == "open"
                and risks[0]["data_source"] == "sample",
                "构造安全风险未能从 API 读取。")

        tasks_response = await asgi_get(application, "/api/v1/tasks")
        tasks_payload = tasks_response.json()
        tasks = tasks_payload["data"]["items"]
        require(tasks_response.status_code == 200 and len(tasks) == 1
                and tasks[0]["task_state"] == "succeeded"
                and tasks[0]["data_source"] == "sample",
                "构造 iperf3 历史任务未能从 API 读取。")

        params = {
            "interface_id": "sample-router:1",
            "from": "2026-10-13T09:00:00Z",
            "to": "2026-10-13T09:00:11Z",
            "direction": "both",
        }
        traffic_response = await asgi_get(application, "/api/v1/traffic", params)
        require(traffic_response.status_code == 200, "流量 JSON API 未返回 HTTP 200。")
        traffic_payload = traffic_response.json()
        report = traffic_payload["data"]
        require(traffic_payload["data_source"] == "sample", "流量 JSON 来源不是 sample。")
        require(report["in_bytes"] == "125" and report["out_bytes"] == "250",
                "流量 JSON 样例值与预期不符。")
        require(report["source_sample_ids"] == valid_metric["source_sample_ids"]
                and report["metric_sample_ids"] == [valid_metric["metric_id"]],
                "流量汇总的原始计数器样本与派生指标 ID 溯源不一致。")
        require(report["coverage_pct"] == 90.91 and not report["complete"],
                "流量 JSON 未显示窗口不完整和覆盖率。")

        csv_response = await asgi_get(application, "/api/v1/traffic/export.csv", params)
        require(csv_response.status_code == 200, "流量 CSV API 未返回 HTTP 200。")
        require("text/csv" in csv_response.headers.get("content-type", ""),
                "流量 CSV 内容类型错误。")
        rows = list(csv.DictReader(io.StringIO(csv_response.content.decode("utf-8-sig"))))
        require([(row["direction"], row["bytes"], row["data_source"]) for row in rows]
                == [("in", "125", "sample"), ("out", "250", "sample")],
                "流量 CSV 与预期的样例方向/数值/来源不一致。")

    print("解释器：", sys.version.split()[0],
          f"(isolated={sys.flags.isolated}, no_site={sys.flags.no_site})")
    print("应用来源：", pathlib.Path(app.__file__).resolve())
    print("依赖来源：", vendor.resolve())
    print(f"依赖锁：{dependency_count} 个发行包版本与 vendor 完全一致。")
    print("Uvicorn 启动器：", runner.stdout.strip())
    print("通过：health、首页、CSS/JS、sample 设备、故障/确认/恢复和 ping 历史。")
    print("通过：配置快照变化、有效/无效性能样本、服务基线/端口检查/安全风险、任务历史。")
    print("通过：流量 JSON 与 CSV 入/出字节均为 125/250，覆盖率 90.91%，窗口不完整，来源为 sample。")
    print("通过：live、service probe、iperf3 均关闭；未访问 DEV 或真实实验网络。")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "archive",
        nargs="?",
        type=pathlib.Path,
        default=REPO_ROOT / "data" / "nms-team2-release.tar.gz",
        help="发布包路径，默认 data/nms-team2-release.tar.gz",
    )
    args = parser.parse_args()
    archive = args.archive.resolve()
    if not archive.is_file():
        parser.error(f"发布包不存在：{archive}")

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    print(f"发布包：{archive}")
    print(f"SHA-256：{digest}")
    try:
        with tempfile.TemporaryDirectory(prefix="nms-team2-release-smoke-") as temp_dir:
            extracted = pathlib.Path(temp_dir) / "package"
            extracted.mkdir()
            extract_release(archive, extracted)
            print("通过：发布包路径、类型、大小和必要文件校验。")
            asyncio.run(smoke(extracted))
    except Exception as exc:
        print(f"失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
