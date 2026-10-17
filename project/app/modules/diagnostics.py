"""Explain observations and possible causes without claiming proven root causes."""
from __future__ import annotations

import ipaddress
from datetime import datetime

from app.core.db import Database, utc_now


def _distance(a: str, b: str) -> float:
    return abs((datetime.fromisoformat(a.replace('Z','+00:00')) -
                datetime.fromisoformat(b.replace('Z','+00:00'))).total_seconds())


def diagnose(database: Database, device_id: str | None = None) -> dict:
    devices = database.rows("SELECT * FROM devices WHERE (? IS NULL OR device_id=?) ORDER BY device_id",
                            (device_id, device_id))
    items = []
    for device in devices:
        findings = []
        ping = database.row("SELECT * FROM ping_samples WHERE device_id=? ORDER BY sampled_at DESC,sample_id DESC LIMIT 1",
                            (device["device_id"],))
        interfaces = database.rows("SELECT * FROM interfaces WHERE device_id=?", (device["device_id"],))
        if ping:
            evidence = {"ping_sample_id": ping["sample_id"], "sampled_at": ping["sampled_at"],
                        "data_source": ping["data_source"]}
            if ping["query_status"] != "ok" or ping["loss_pct"] is None:
                findings.append({"code":"measurement_unknown", "level":"info", **evidence,
                    "summary":"ping 测量未能确认连通性", "next_steps":["检查测量工具、命令错误与 DEV 路由；保留未知状态"]})
            elif ping["loss_pct"] == 100:
                related = []
                rows = database.rows("""SELECT a.*,i.admin_status,i.oper_status,i.query_status,
                    i.sampled_at AS status_at,i.data_source AS status_source FROM interface_addresses a
                    JOIN interfaces i ON i.interface_id=a.interface_id
                    JOIN network_discoveries d ON d.discovery_id=a.discovery_id
                    WHERE d.query_status='ok' AND d.discovery_id=(SELECT n.discovery_id
                      FROM network_discoveries n WHERE n.device_id=d.device_id
                      ORDER BY n.sampled_at DESC,n.discovery_id DESC LIMIT 1)""")
                for row in rows:
                    if (row["data_source"] == row["status_source"] == ping["data_source"] and
                        row["query_status"] == "ok" and row["admin_status"] == "up" and row["oper_status"] == "down"
                        and _distance(row["sampled_at"], ping["sampled_at"]) <= 60
                        and _distance(row["status_at"], ping["sampled_at"]) <= 60
                        and ipaddress.IPv4Address(ping["target"]) in ipaddress.IPv4Network(row["network"])):
                        related.append({"interface_id":row["interface_id"], "address_id":row["address_id"]})
                findings.append({"code":"ping_unreachable", "level":"warning", **evidence,
                    "summary":"本次 ping 全部丢包；可能涉及目标状态、路径或 ICMP 策略",
                    "related_down_interfaces":related, "root_cause_confirmed":False,
                    "next_steps":["核对同网段接口及目标状态", "逐段检查路由/可达性；与相近时刻样本交叉验证"]})
            else:
                findings.append({"code":"ping_replied", "level":"info", **evidence,
                    "summary":f"本次收到 ICMP 响应，丢包率 {ping['loss_pct']}%",
                    "next_steps":["结合连续采样判断持续性，ICMP 结果不覆盖所有应用服务"]})
        for interface in interfaces:
            if interface["query_status"] == "ok" and interface["admin_status"] == "up" and interface["oper_status"] == "down":
                findings.append({"code":"interface_oper_down", "level":"warning",
                    "interface_id":interface["interface_id"], "sampled_at":interface["sampled_at"],
                    "data_source":interface["data_source"], "summary":"接口期望启用，但运行状态为 down",
                    "root_cause_confirmed":False, "next_steps":["检查接口、对端及连接状态，并核对连续异常次数"]})
        times = [x["sampled_at"] for x in findings if x.get("sampled_at")]
        latest = max(times) if times else None
        items.append({"device_id":device["device_id"], "address":device["address"],
                      "findings":findings, "sampled_at":latest,
                      "stale":latest is None or _distance(utc_now(), latest) > 300,
                      "data_source":next(iter({x['data_source'] for x in findings})) if findings and len({x['data_source'] for x in findings})==1 else 'mixed' if findings else 'none'})
    return {"items":items, "total":len(items),
            "note":"诊断解释最近已有样本；超过 5 分钟标为历史。相关接口仅提供排查线索，不确认根因。"}
