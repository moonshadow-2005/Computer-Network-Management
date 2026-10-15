from __future__ import annotations

import ipaddress
import re
import subprocess


def parse_ping(output: str) -> dict:
    counts = re.search(r"(\d+) packets transmitted,\s*(\d+) (?:packets )?received", output)
    if counts is None:
        return {"query_status": "error", "packets_sent": None,
                "packets_received": None, "rtt_ms": None, "loss_pct": None,
                "error": "ping output could not be parsed"}
    sent, received = map(int, counts.groups())
    times = [float(x) for x in re.findall(r"\btime[=<]([\d.]+)\s*ms", output)]
    return {"query_status": "ok", "packets_sent": sent,
            "packets_received": received, "rtt_ms": sum(times) / len(times) if times else None,
            "loss_pct": (100 * (sent - received) / sent) if sent else None,
            "error": None}


def probe(address: str, timeout_s: int = 3) -> dict:
    ipaddress.IPv4Address(address)
    try:
        result = subprocess.run(
            ["ping", "-n", "-c", "2", "-W", "1", "-w", str(timeout_s), address],
            capture_output=True, text=True, timeout=timeout_s + 1,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"query_status": "timeout", "packets_sent": None,
                "packets_received": None, "rtt_ms": None, "loss_pct": None,
                "error": "ping command unavailable or timed out"}
    return parse_ping(result.stdout + "\n" + result.stderr)
