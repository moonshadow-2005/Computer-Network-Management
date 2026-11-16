"""Passive TCP port-probing evidence. No network sends or packet payload storage."""
from __future__ import annotations

import hashlib
import ipaddress
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from app.core.db import Database, decode, encode


def stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


@dataclass(frozen=True)
class ScanPolicy:
    sensor_id: str = "dev-inbound"
    device_id: str = "dev"
    targets: tuple[str, ...] = ()
    internal_networks: tuple[str, ...] = ()
    interface: str = ""
    fast_ports: int = 8
    slow_ports: int = 20
    slow_minutes: int = 3
    quiet_seconds: int = 300
    max_groups: int = 4096
    max_ports: int = 256

    def __post_init__(self):
        if not self.sensor_id or len(self.sensor_id) > 64:
            raise ValueError("sensor_id must contain 1..64 characters")
        if not self.targets:
            raise ValueError("explicit local target addresses are required")
        for address in self.targets:
            ipaddress.ip_address(address)
        for network in self.internal_networks:
            ipaddress.ip_network(network, strict=True)
        if not 2 <= self.fast_ports <= self.max_ports or not 2 <= self.slow_ports <= self.max_ports:
            raise ValueError("port thresholds must be between 2 and max_ports")
        if not 2 <= self.slow_minutes <= 15 or self.quiet_seconds < 300 or self.max_groups < 2:
            raise ValueError("invalid scan window limits")

    def public(self) -> dict:
        return {"sensor_id": self.sensor_id, "device_id": self.device_id,
                "target_addresses": list(self.targets), "interface": self.interface or "本机全部接口",
                "internal_networks": list(self.internal_networks),
                "fast_window_seconds": 60, "fast_port_threshold": self.fast_ports,
                "slow_window_seconds": 900, "slow_port_threshold": self.slow_ports,
                "slow_active_minutes": self.slow_minutes, "quiet_seconds": self.quiet_seconds,
                "coverage": "仅配置的本机目标 IP 入站 TCP SYN；不代表整个实验网络。",
                "scope_note": "外部仅指配置的内部网段之外；不等于公网攻击者，来源 IP 也可能被伪造。",
                "limitations": "未检测 UDP、无 SYN 扫描、分布式扫描或低于阈值的探测；不确认漏洞和入侵。",
                "window_mode": "UTC 对齐的固定 60 秒 / 15 分钟窗口，边界拆分可能漏检。"}


@dataclass
class Window:
    source: str
    target: str
    scope: str
    start: int
    seconds: int
    first: float
    last: float
    packets: int = 0
    ports: set[int] = field(default_factory=set)
    minutes: set[int] = field(default_factory=set)
    truncated: bool = False
    dirty: bool = False


class ScanDetector:
    def __init__(self, database: Database, policy: ScanPolicy, now: float, *, data_source: str = "live"):
        if data_source not in {"live", "sample"}:
            raise ValueError("invalid data source")
        self.database, self.policy, self.data_source = database, policy, data_source
        self.session = str(uuid4())
        self.started = self.healthy_since = self.last_tick = now
        self.last_packet: float | None = None
        self.targets = {str(ipaddress.ip_address(x)) for x in policy.targets}
        self.networks = [ipaddress.ip_network(x) for x in policy.internal_networks]
        self.windows: dict[tuple, Window] = {}
        self.counters = {"syn_packets": 0, "kernel_drops": 0, "capacity_drops": 0, "clock_gaps": 0,
                         "persistence_retries": 0}
        self.quality_gap: float | None = None
        self._register(now)

    def _register(self, now: float):
        with self.database.connect() as db:
            if not db.execute("SELECT 1 FROM devices WHERE device_id=?", (self.policy.device_id,)).fetchone():
                raise ValueError("sensor device is not registered")
            db.execute("""INSERT INTO scan_sensors VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(sensor_id) DO UPDATE SET device_id=excluded.device_id,
                session_id=excluded.session_id,state=excluded.state,started_at=excluded.started_at,
                updated_at=excluded.updated_at,last_packet_at=NULL,config=excluded.config,
                counters=excluded.counters,error=NULL,data_source=excluded.data_source""",
                (self.policy.sensor_id, self.policy.device_id, self.session, "starting", stamp(now),
                 stamp(now), None, encode(self.policy.public()), encode(self.counters), None, self.data_source))

    def classify(self, address: str) -> str:
        value = ipaddress.ip_address(address)
        if value.is_loopback:
            return "internal"
        if not self.networks:
            return "unknown"
        return "internal" if any(value in network for network in self.networks) else "external"

    def gap(self, now: float, *, kernel_drops: int = 0):
        self.counters["kernel_drops"] += kernel_drops
        self.quality_gap = self.healthy_since = now

    def observe(self, source: str, target: str, port: int, now: float):
        source, target = str(ipaddress.ip_address(source)), str(ipaddress.ip_address(target))
        if target not in self.targets or not 1 <= port <= 65535:
            return
        # Clock discontinuities must not mix observation windows or imply recovery.
        if now < self.last_tick:
            self.windows.clear()
            self.gap(now)
            self.counters["clock_gaps"] += 1
        self.last_packet = now
        self.counters["syn_packets"] += 1
        for seconds in (60, 900):
            start = int(now // seconds) * seconds
            key = (source, target, seconds, start)
            window = self.windows.get(key)
            if window is None:
                if len(self.windows) >= self.policy.max_groups:
                    self.counters["capacity_drops"] += 1
                    self.gap(now)
                    continue
                window = Window(source, target, self.classify(source), start, seconds, now, now)
                self.windows[key] = window
            window.last, window.dirty = now, True
            window.packets += 1  # SYN retransmissions are included, not unique connection counts.
            if len(window.ports) < self.policy.max_ports or port in window.ports:
                window.ports.add(port)
            else:
                window.truncated = True
            window.minutes.add(int(now // 60))

    def matches(self, window: Window) -> bool:
        if window.seconds == 60:
            return len(window.ports) >= self.policy.fast_ports
        return len(window.ports) >= self.policy.slow_ports and len(window.minutes) >= self.policy.slow_minutes

    def _risk(self, db, window: Window):
        digest = hashlib.sha256(f"{self.policy.sensor_id}|{window.source}|{window.target}|{self.data_source}".encode()).hexdigest()[:24]
        rule = "tcp_port_scan_" + digest
        previous = db.execute("""SELECT * FROM risks WHERE device_id=? AND rule_id=?
             AND state IN ('open','acknowledged') ORDER BY risk_id DESC LIMIT 1""",
             (self.policy.device_id, rule)).fetchone()
        old = decode(previous["evidence"], {}) if previous else {}
        same_session = old.get("session_id") == self.session and old.get("quality_gap_at") == self.quality_gap
        previous_fast = old.get("last_fast_window") if same_session else None
        streak = old.get("consecutive_fast_windows", 0) if same_session else 0
        if window.seconds == 60:
            if previous_fast != window.start:
                streak = streak + 1 if previous_fast == window.start - 60 else 1
            previous_fast = window.start
        sustained = window.seconds == 900 or streak >= 3
        scope_label = {"external": "配置网段外来源", "internal": "内部来源", "unknown": "归属未知来源"}[window.scope]
        summary = (f"{scope_label} {window.source} → {window.target}：{window.seconds} 秒窗口内"
                   f"探测至少 {len(window.ports)} 个 TCP 端口" + ("；持续探测线索" if sustained else "；疑似端口扫描"))
        evidence = {"summary": summary, "basis": "入站 TCP SYN（不含 ACK/RST/FIN）；按来源与目标聚合不同目的端口",
                    "sensor_id": self.policy.sensor_id, "session_id": self.session,
                    "quality_gap_at": self.quality_gap,
                    "source_ip": window.source, "target_ip": window.target, "source_scope": window.scope,
                    "window_start": stamp(window.start), "window_seconds": window.seconds,
                    "distinct_ports": len(window.ports), "ports": sorted(window.ports)[:32],
                    "ports_truncated": window.truncated or len(window.ports) > 32,
                    "syn_packets": window.packets, "active_minutes": len(window.minutes),
                    "last_fast_window": previous_fast, "consecutive_fast_windows": streak,
                    "sustained": sustained, "coverage": self.policy.public()["coverage"],
                    "source_identity_confirmed": False, "vulnerability_confirmed": False,
                    "recommendation": "核对授权扫描、运维活动、NAT 与防火墙日志；此记录不证明入侵，不自动封禁。"}
        if previous:
            db.execute("UPDATE risks SET last_seen=?,evidence=? WHERE risk_id=?",
                       (stamp(window.last), encode(evidence), previous["risk_id"]))
        else:
            db.execute("""INSERT INTO risks(device_id,rule_id,level,state,first_seen,last_seen,evidence,data_source)
                          VALUES (?,?,'warning','open',?,?,?,?)""",
                       (self.policy.device_id, rule, stamp(window.first), stamp(window.last), encode(evidence), self.data_source))

    def flush(self, now: float, *, state: str = "running", error: str | None = None):
        if now < self.last_tick or now - self.last_tick > 20:
            self.gap(now)
            self.counters["clock_gaps"] += 1
        self.last_tick = now
        if state != "running":
            self.gap(now)
        if state == "running" and self.quality_gap is not None and now - self.quality_gap < self.policy.quiet_seconds:
            state = "degraded"
        with self.database.connect() as db:
            # The original collector may hold a write transaction across SNMP
            # requests. Never block capture behind it or discard dirty windows.
            db.execute("PRAGMA busy_timeout=0")
            try:
                db.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                self.counters["persistence_retries"] += 1
                return False
            for window in sorted(self.windows.values(), key=lambda x: (x.last, x.seconds)):
                if not window.dirty:
                    continue
                matched = self.matches(window)
                db.execute("""INSERT INTO scan_windows(sensor_id,session_id,source_ip,target_ip,source_scope,
                    window_start,window_seconds,first_seen,last_seen,syn_packets,port_count,ports,
                    ports_truncated,active_minutes,matched,data_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(sensor_id,session_id,source_ip,target_ip,window_start,window_seconds)
                    DO UPDATE SET last_seen=excluded.last_seen,syn_packets=excluded.syn_packets,
                    port_count=excluded.port_count,ports=excluded.ports,ports_truncated=excluded.ports_truncated,
                    active_minutes=excluded.active_minutes,matched=excluded.matched""",
                    (self.policy.sensor_id, self.session, window.source, window.target, window.scope,
                     stamp(window.start), window.seconds, stamp(window.first), stamp(window.last), window.packets,
                     len(window.ports), encode(sorted(window.ports)[:32]), int(window.truncated or len(window.ports) > 32),
                     len(window.minutes), int(matched), self.data_source))
                if matched:
                    self._risk(db, window)
            # Unknown capture state, restart warm-up, drops or timing gaps never clear alarms.
            if state == "running" and now - self.healthy_since >= self.policy.quiet_seconds:
                for risk in db.execute("""SELECT * FROM risks WHERE device_id=? AND rule_id LIKE 'tcp_port_scan_%'
                        AND state IN ('open','acknowledged') AND last_seen<=? AND data_source=?""",
                        (self.policy.device_id, stamp(now - self.policy.quiet_seconds), self.data_source)).fetchall():
                    evidence = decode(risk["evidence"], {})
                    if evidence.get("sensor_id") != self.policy.sensor_id:
                        continue
                    evidence["resolution"] = "采集持续可用期间，至少 300 秒未再命中扫描阈值；不代表漏洞修复或来源安全。"
                    db.execute("UPDATE risks SET state='resolved',resolved_at=?,evidence=? WHERE risk_id=?",
                               (stamp(now), encode(evidence), risk["risk_id"]))
            db.execute("""UPDATE scan_sensors SET state=?,updated_at=?,last_packet_at=?,counters=?,error=?
                          WHERE sensor_id=? AND session_id=?""",
                       (state, stamp(now), stamp(self.last_packet) if self.last_packet is not None else None,
                        encode(self.counters), error, self.policy.sensor_id, self.session))
            # Bound rolling observation history; triggered risk evidence is retained separately.
            db.execute("DELETE FROM scan_windows WHERE last_seen<?", (stamp(now - 86400),))
            db.execute("""DELETE FROM scan_windows WHERE window_id IN
                (SELECT window_id FROM scan_windows ORDER BY last_seen DESC,window_id DESC LIMIT -1 OFFSET 5000)""")
        self.windows = {key: window for key, window in self.windows.items() if window.start + window.seconds > now}
        for window in self.windows.values():
            window.dirty = False
        return True


def scan_status(database: Database, now: float) -> dict:
    sensors = database.rows("SELECT * FROM scan_sensors ORDER BY sensor_id")
    for sensor in sensors:
        sensor["config"] = decode(sensor["config"], {})
        sensor["counters"] = decode(sensor["counters"], {})
        age = now - epoch(sensor["updated_at"])
        sensor["heartbeat_age_s"] = max(0, round(age, 1))
        sensor["stale"] = age > 30 or age < -5
        sensor["effective_state"] = "stale" if sensor["stale"] else sensor["state"]
    return {"items": sensors, "note": "未接入、离线或采集有丢失时不能判断是否被扫描；没有告警也不等于没有扫描。"}
