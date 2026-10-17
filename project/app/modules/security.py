from __future__ import annotations

import socket

from app.core.db import Database, encode, utc_now


def _policy_risk(db, device_id: str, rule: str, observed_at: str, present: bool,
                 evidence: dict, data_source: str) -> int | None:
    previous = db.execute("SELECT risk_id FROM risks WHERE device_id=? AND rule_id=? AND state IN ('open','acknowledged')",
                          (device_id, rule)).fetchone()
    if present:
        if previous:
            db.execute("UPDATE risks SET last_seen=?,evidence=? WHERE risk_id=?",
                       (observed_at, encode(evidence), previous["risk_id"]))
            return previous["risk_id"]
        return db.execute("""INSERT INTO risks(device_id,rule_id,level,state,first_seen,last_seen,evidence,data_source)
                            VALUES (?,?,'warning','open',?,?,?,?)""",
                          (device_id, rule, observed_at, observed_at, encode(evidence), data_source)).lastrowid
    if previous:
        db.execute("UPDATE risks SET state='resolved',last_seen=?,resolved_at=?,evidence=? WHERE risk_id=?",
                   (observed_at, observed_at, encode(evidence), previous["risk_id"]))
        return previous["risk_id"]
    return None


def record_snmp_access_risk(database: Database, device_id: str, sampled_at: str,
                            common_default: bool) -> int | None:
    """A different working credential does not prove the old default was revoked."""
    evidence = {"basis":"已成功执行 SNMPv2c 只读查询", "common_default_credential":common_default,
                "observed_from":"DEV 管理端", "other_network_exposure_tested":False,
                "summary":"常见默认 SNMP 团体字可读取管理信息" if common_default else "本次成功查询未命中默认凭据规则",
                "recommendation":"核实管理访问 ACL 与凭据策略；课程环境内的正式配置由维护方确认",
                "vulnerability_confirmed":False, "attack_source_identified":False}
    with database.connect() as db:
        if not common_default:
            previous = db.execute("""SELECT risk_id FROM risks WHERE device_id=? AND
                rule_id='snmp_common_default_read_access' AND state IN ('open','acknowledged')""",
                (device_id,)).fetchone()
            return previous["risk_id"] if previous else None
        return _policy_risk(db, device_id, "snmp_common_default_read_access", sampled_at,
                            True, evidence, "live")


def check_tcp(address: str, port: int, timeout_s: float = 1.0) -> tuple[str, str | None]:
    try:
        with socket.create_connection((address, port), timeout=timeout_s):
            return "open", None
    except ConnectionRefusedError:
        return "closed", None
    except (TimeoutError, OSError):
        return "unavailable", "TCP connect did not confirm port state"


def record_service_check(database: Database, device_id: str, port: int,
                         state: str, error: str | None = None,
                         data_source: str = "live", observed_at: str | None = None) -> dict:
    if state not in {"open", "closed", "unavailable"}:
        raise ValueError("invalid service state")
    observed_at = observed_at or utc_now()
    with database.connect() as db:
        baseline = db.execute(
            "SELECT allowed FROM service_baselines WHERE device_id=? AND port=?",
            (device_id, port),
        ).fetchone()
        if baseline is None:
            raise LookupError("service baseline is required before probing")
        check_id = db.execute(
            """INSERT INTO service_checks(device_id,port,observed_at,state,error,data_source)
               VALUES (?,?,?,?,?,?)""",
            (device_id, port, observed_at, state, error, data_source),
        ).lastrowid
        rule = f"unexpected_open_tcp_{port}"
        open_risk = db.execute(
            "SELECT risk_id FROM risks WHERE device_id=? AND rule_id=? AND state IN ('open','acknowledged')",
            (device_id, rule),
        ).fetchone()
        evidence = {"check_id": check_id, "port": port, "observed_state": state,
                    "basis": "configured service baseline"}
        risk_id = open_risk["risk_id"] if open_risk else None
        if not baseline["allowed"] and state == "open":
            if open_risk:
                db.execute("UPDATE risks SET last_seen=?,evidence=? WHERE risk_id=?",
                           (observed_at, encode(evidence), risk_id))
            else:
                risk_id = db.execute(
                    """INSERT INTO risks(device_id,rule_id,level,state,first_seen,last_seen,
                       resolved_at,evidence,data_source) VALUES (?,?,'warning','open',?,?,NULL,?,?)""",
                    (device_id, rule, observed_at, observed_at, encode(evidence), data_source),
                ).lastrowid
        elif open_risk and (state == "closed" or baseline["allowed"]):
            db.execute("UPDATE risks SET state='resolved',last_seen=?,resolved_at=?,evidence=? WHERE risk_id=?",
                       (observed_at, observed_at, encode(evidence), risk_id))
        if port == 23 and state in {"open", "closed"}:
            _policy_risk(db, device_id, "possible_telnet_service_tcp_23", observed_at, state == "open",
                         {**evidence, "summary":"TCP/23 开放，需核实是否提供明文 Telnet 管理服务",
                          "basis":"端口号和连接结果的启发式线索，未识别实际协议或验证漏洞",
                          "vulnerability_confirmed":False}, data_source)
        return {"check_id": check_id, "state": state, "risk_id": risk_id}


def record_utilization_risk(database: Database, device_id: str, interface_id: str,
                            metric_id: int, threshold_pct: float = 80.0,
                            consecutive: int = 3) -> int | None:
    """Flag sustained interface saturation; unknown samples never imply safety or attack."""
    if not 0 < threshold_pct <= 100 or consecutive < 2:
        raise ValueError("invalid utilization rule configuration")
    rule = f"high_utilization_{interface_id}"
    with database.connect() as db:
        current = db.execute("SELECT * FROM metric_samples WHERE metric_id=? AND interface_id=?",
                             (metric_id, interface_id)).fetchone()
        if current is None:
            raise LookupError("metric not found")
        opened = db.execute(
            "SELECT risk_id FROM risks WHERE device_id=? AND rule_id=? AND state IN ('open','acknowledged')",
            (device_id, rule),
        ).fetchone()
        if not current["valid"] or current["data_source"] != "live" or (
            current["in_util_pct"] is None and current["out_util_pct"] is None
        ):
            return opened["risk_id"] if opened else None
        high = max(x for x in (current["in_util_pct"], current["out_util_pct"]) if x is not None) >= threshold_pct
        evidence = {"metric_id": metric_id, "interface_id": interface_id,
                    "threshold_pct": threshold_pct, "in_util_pct": current["in_util_pct"],
                    "out_util_pct": current["out_util_pct"],
                    "basis": "interface utilization, not attribution of attack source"}
        if not high:
            if opened:
                db.execute("UPDATE risks SET state='resolved',last_seen=?,resolved_at=?,evidence=? WHERE risk_id=?",
                           (current["sampled_at"], current["sampled_at"], encode(evidence), opened["risk_id"]))
            return opened["risk_id"] if opened else None
        recent = db.execute(
            """SELECT * FROM metric_samples WHERE interface_id=? AND metric_id<=?
               ORDER BY metric_id DESC LIMIT ?""", (interface_id, metric_id, consecutive),
        ).fetchall()
        sustained = len(recent) == consecutive and all(
            row["valid"] and row["data_source"] == "live" and
            any(value is not None and value >= threshold_pct
                for value in (row["in_util_pct"], row["out_util_pct"]))
            for row in recent
        )
        if not sustained:
            return opened["risk_id"] if opened else None
        evidence["consecutive_metric_ids"] = [row["metric_id"] for row in reversed(recent)]
        if opened:
            db.execute("UPDATE risks SET last_seen=?,evidence=? WHERE risk_id=?",
                       (current["sampled_at"], encode(evidence), opened["risk_id"]))
            return opened["risk_id"]
        return db.execute(
            """INSERT INTO risks(device_id,rule_id,level,state,first_seen,last_seen,
               resolved_at,evidence,data_source) VALUES (?,?,'warning','open',?,?,NULL,?,'live')""",
            (device_id, rule, current["sampled_at"], current["sampled_at"], encode(evidence)),
        ).lastrowid
