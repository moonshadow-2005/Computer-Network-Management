from __future__ import annotations

import json
import sqlite3
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def decode(value: str | None, default: object = None) -> object:
    return json.loads(value) if value is not None else default


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS devices (
  device_id TEXT PRIMARY KEY, label TEXT NOT NULL, address TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL, identity_status TEXT NOT NULL, data_source TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS interfaces (
  interface_id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(device_id),
  if_index INTEGER NOT NULL, description TEXT, admin_status TEXT, oper_status TEXT,
  reference_speed_bps INTEGER, query_status TEXT NOT NULL, sampled_at TEXT,
  data_source TEXT NOT NULL, UNIQUE(device_id, if_index)
);
CREATE TABLE IF NOT EXISTS routes (
  route_id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(device_id),
  destination TEXT NOT NULL, mask TEXT, next_hop TEXT, interface_id TEXT,
  sampled_at TEXT NOT NULL, data_source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ping_samples (
  sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT NOT NULL REFERENCES devices(device_id), target TEXT NOT NULL,
  sampled_at TEXT NOT NULL, query_status TEXT NOT NULL,
  packets_sent INTEGER, packets_received INTEGER, rtt_ms REAL, loss_pct REAL,
  error TEXT, data_source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS interface_status_samples (
  sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
  interface_id TEXT NOT NULL REFERENCES interfaces(interface_id),
  sampled_at TEXT NOT NULL, admin_status TEXT, oper_status TEXT,
  query_status TEXT NOT NULL, data_source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS counter_samples (
  sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
  interface_id TEXT NOT NULL REFERENCES interfaces(interface_id),
  sampled_at TEXT NOT NULL, in_octets TEXT, out_octets TEXT,
  sys_uptime_ticks TEXT, discontinuity_ticks TEXT,
  query_status TEXT NOT NULL, invalid_reason TEXT, data_source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metric_samples (
  metric_id INTEGER PRIMARY KEY AUTOINCREMENT,
  interface_id TEXT NOT NULL REFERENCES interfaces(interface_id),
  sampled_at TEXT NOT NULL, interval_start TEXT,
  in_delta_bytes TEXT, out_delta_bytes TEXT,
  in_rate_bps REAL, out_rate_bps REAL,
  in_util_pct REAL, out_util_pct REAL,
  valid INTEGER NOT NULL, invalid_reason TEXT,
  source_sample_ids TEXT NOT NULL, data_source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fault_state (
  device_id TEXT PRIMARY KEY REFERENCES devices(device_id), failures INTEGER NOT NULL DEFAULT 0,
  active_event_id INTEGER
);
CREATE TABLE IF NOT EXISTS interface_fault_state (
  interface_id TEXT PRIMARY KEY REFERENCES interfaces(interface_id),
  failures INTEGER NOT NULL DEFAULT 0, active_event_id INTEGER
);
CREATE TABLE IF NOT EXISTS events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT NOT NULL REFERENCES devices(device_id), interface_id TEXT,
  type TEXT NOT NULL, state TEXT NOT NULL, first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL, recovered_at TEXT, evidence_sample_ids TEXT NOT NULL,
  data_source TEXT NOT NULL, acknowledged_at TEXT
);
CREATE TABLE IF NOT EXISTS config_snapshots (
  snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT NOT NULL REFERENCES devices(device_id), sampled_at TEXT NOT NULL,
  content TEXT NOT NULL, data_source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS collector_runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
  finished_at TEXT, state TEXT NOT NULL, details TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS service_baselines (
  device_id TEXT NOT NULL REFERENCES devices(device_id), port INTEGER NOT NULL,
  allowed INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '',
  data_source TEXT NOT NULL DEFAULT 'configured',
  PRIMARY KEY(device_id,port)
);
CREATE TABLE IF NOT EXISTS service_checks (
  check_id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT NOT NULL REFERENCES devices(device_id), port INTEGER NOT NULL,
  observed_at TEXT NOT NULL, state TEXT NOT NULL, error TEXT,
  data_source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risks (
  risk_id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT NOT NULL REFERENCES devices(device_id), rule_id TEXT NOT NULL,
  level TEXT NOT NULL, state TEXT NOT NULL, first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL, resolved_at TEXT, evidence TEXT NOT NULL,
  data_source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY, type TEXT NOT NULL, task_state TEXT NOT NULL,
  client_device_id TEXT NOT NULL REFERENCES devices(device_id),
  server_device_id TEXT NOT NULL REFERENCES devices(device_id),
  params TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT,
  finished_at TEXT, result TEXT, error TEXT, data_source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ping_device_time ON ping_samples(device_id,sampled_at);
CREATE INDEX IF NOT EXISTS idx_interface_status_time ON interface_status_samples(interface_id,sampled_at);
CREATE INDEX IF NOT EXISTS idx_counter_interface_time ON counter_samples(interface_id,sampled_at);
CREATE INDEX IF NOT EXISTS idx_metric_interface_time ON metric_samples(interface_id,sampled_at);
CREATE INDEX IF NOT EXISTS idx_event_device_time ON events(device_id,last_seen);
CREATE INDEX IF NOT EXISTS idx_risk_device_time ON risks(device_id,last_seen);
"""


NETWORK_TABLES = (
    """CREATE TABLE IF NOT EXISTS network_discoveries (
      discovery_id INTEGER PRIMARY KEY AUTOINCREMENT,
      device_id TEXT NOT NULL REFERENCES devices(device_id), sampled_at TEXT NOT NULL,
      query_status TEXT NOT NULL, sys_name TEXT, sys_object_id TEXT,
      issues TEXT NOT NULL, data_source TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS interface_addresses (
      address_id INTEGER PRIMARY KEY AUTOINCREMENT,
      discovery_id INTEGER NOT NULL REFERENCES network_discoveries(discovery_id),
      device_id TEXT NOT NULL REFERENCES devices(device_id),
      interface_id TEXT NOT NULL REFERENCES interfaces(interface_id), if_index INTEGER NOT NULL,
      address TEXT NOT NULL, mask TEXT NOT NULL, network TEXT NOT NULL,
      sampled_at TEXT NOT NULL, data_source TEXT NOT NULL,
      UNIQUE(discovery_id,address)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_discovery_device_time ON network_discoveries(device_id,sampled_at)",
    "CREATE INDEX IF NOT EXISTS idx_address_discovery ON interface_addresses(discovery_id)",
)
SCHEMA += "\n" + ";\n".join(NETWORK_TABLES) + ";\n"

CURRENT_SCHEMA_VERSION = 3


DOCUMENT_DEVICES = (
    ("dev", "DEV 开发主机", "192.168.0.100", "dev"),
    ("r-0-1", "连接地址 192.168.0.1", "192.168.0.1", "router-candidate"),
    ("r-0-2", "连接地址 192.168.0.2", "192.168.0.2", "router-candidate"),
    ("r-0-5", "连接地址 192.168.0.5", "192.168.0.5", "router-candidate"),
    ("r-0-8", "连接地址 192.168.0.8", "192.168.0.8", "router-candidate"),
    ("host-1", "子网 1 测试主机", "192.168.1.1", "test-host"),
    ("host-2", "子网 2 测试主机", "192.168.2.1", "test-host"),
    ("host-3", "子网 3 测试主机", "192.168.3.1", "test-host"),
)


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self) -> None:
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > CURRENT_SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {version} is newer than supported {CURRENT_SCHEMA_VERSION}"
                )
            existing_tables = {
                row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if version < CURRENT_SCHEMA_VERSION and existing_tables:
                self._backup_before_migration(db, version)
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            self._migrate(db, version)
            db.execute(f"PRAGMA user_version={CURRENT_SCHEMA_VERSION}")
            db.execute("""UPDATE collector_runs SET state='failed',finished_at=?,details=?
                       WHERE state='running'""",
                       (utc_now(), encode([{"status":"error", "reason":"采集被服务重启中断，未自动重放"}])))
            db.execute("""UPDATE tasks SET task_state='failed',finished_at=?,
                       error=? WHERE task_state IN ('pending','running')""",
                       (utc_now(), encode({"code": "INTERRUPTED", "message": "服务重启，任务未自动重放"})))
            for device_id, label, address, role in DOCUMENT_DEVICES:
                db.execute(
                    """INSERT OR IGNORE INTO devices
                    (device_id,label,address,role,identity_status,data_source,note)
                    VALUES (?,?,?,?,?,?,?)""",
                    (device_id, label, address, role, "candidate", "document",
                     "地址来自课程说明；设备身份尚未归并确认"),
                )

    def _backup_before_migration(self, db: sqlite3.Connection, from_version: int) -> Path:
        backup_dir = self.path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = backup_dir / (
            f"{self.path.stem}.pre-v{from_version}-to-v{CURRENT_SCHEMA_VERSION}-{stamp}.sqlite3"
        )
        target = sqlite3.connect(backup_path)
        try:
            db.backup(target)
        finally:
            target.close()
        backup_path.chmod(0o600)
        if stat.S_IMODE(backup_path.stat().st_mode) != 0o600:
            backup_path.unlink(missing_ok=True)
            raise PermissionError(
                "filesystem does not enforce private backup permissions; "
                "move the database to a private Linux filesystem before migrating"
            )
        return backup_path

    @staticmethod
    def _migrate(db: sqlite3.Connection, version: int) -> None:
        if version < 1:
            tables = {
                row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if "service_baselines" in tables:
                columns = {row[1] for row in db.execute("PRAGMA table_info(service_baselines)")}
                if "data_source" not in columns:
                    db.execute(
                        "ALTER TABLE service_baselines ADD COLUMN data_source "
                        "TEXT NOT NULL DEFAULT 'configured'"
                    )
            version = 1
        if version < 2:
            tables = {
                row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if "events" in tables:
                columns = {row[1] for row in db.execute("PRAGMA table_info(events)")}
                if "acknowledged_at" not in columns:
                    db.execute("ALTER TABLE events ADD COLUMN acknowledged_at TEXT")
            version = 2
        if version < 3:
            for statement in NETWORK_TABLES:
                db.execute(statement)
            version = 3
        if version != CURRENT_SCHEMA_VERSION:
            raise RuntimeError(f"no migration path from database schema {version}")

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, params)]

    def row(self, sql: str, params: tuple = ()) -> dict | None:
        with self.connect() as db:
            result = db.execute(sql, params).fetchone()
            return dict(result) if result else None
