from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    db_path: Path = ROOT / "data" / "nms.sqlite3"
    snmp_community: str | None = None
    poll_interval_s: float = 0
    snmp_timeout_s: float = 1.5
    ping_timeout_s: int = 3
    max_interfaces: int = 16
    max_routes: int = 64
    fault_threshold: int = 3
    enable_live: bool = False
    enable_service_probe: bool = False
    enable_iperf3: bool = False
    iperf_ssh_user: str = "test-user"
    iperf_ssh_key: Path | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=Path(os.environ.get("NMS_DB", str(ROOT / "data" / "nms.sqlite3"))),
            snmp_community=os.environ.get("NMS_SNMP_COMMUNITY") or None,
            poll_interval_s=max(0.0, float(os.environ.get("NMS_POLL_INTERVAL", "0"))),
            snmp_timeout_s=max(0.2, float(os.environ.get("NMS_SNMP_TIMEOUT", "1.5"))),
            ping_timeout_s=max(1, int(os.environ.get("NMS_PING_TIMEOUT", "3"))),
            max_interfaces=max(1, min(64, int(os.environ.get("NMS_MAX_INTERFACES", "16")))),
            max_routes=max(1, min(128, int(os.environ.get("NMS_MAX_ROUTES", "64")))),
            fault_threshold=max(1, int(os.environ.get("NMS_FAULT_THRESHOLD", "3"))),
            enable_live=os.environ.get("NMS_ENABLE_LIVE") == "1",
            enable_service_probe=os.environ.get("NMS_ENABLE_SERVICE_PROBE") == "1",
            enable_iperf3=os.environ.get("NMS_ENABLE_IPERF3") == "1",
            iperf_ssh_user=os.environ.get("NMS_IPERF_SSH_USER", "test-user"),
            iperf_ssh_key=Path(os.environ["NMS_IPERF_SSH_KEY"]) if os.environ.get("NMS_IPERF_SSH_KEY") else None,
        )
