"""Linux local-host inbound SYN sensor. Run as a separate CAP_NET_RAW service."""
from __future__ import annotations

import hashlib
import ctypes
import ipaddress
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import time

from app.core.db import Database, CURRENT_SCHEMA_VERSION
from app.modules.scan_detection import ScanDetector, ScanPolicy


def attach_syn_filter(packet_socket):
    """Classic BPF on SOCK_DGRAM IP headers; discard bulk traffic before queuing.

    IPv6 extension chains go to the bounded userspace decoder. No external
    compiler, libpcap, kernel program installation or firewall rule is needed.
    """
    class Instruction(ctypes.Structure):
        _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                    ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32)]
    class Program(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ushort), ("instructions", ctypes.POINTER(Instruction))]
    instructions = [
        (0x30, 0, 0, 0),       # 0: IP version
        (0x54, 0, 0, 0xf0),
        (0x15, 1, 0, 0x40),    # IPv4 -> 4
        (0x15, 8, 17, 0x60),   # IPv6 -> 12; otherwise reject
        (0x30, 0, 0, 9),       # 4: IPv4 protocol
        (0x15, 0, 15, 6),      # TCP only
        (0x28, 0, 0, 6),
        (0x45, 13, 0, 0x3fff), # reject IPv4 fragments
        (0xb1, 0, 0, 0),       # X = IPv4 header length
        (0x50, 0, 0, 13),      # TCP flags at X + 13
        (0x54, 0, 0, 0x17),
        (0x15, 8, 9, 2),       # SYN without ACK/RST/FIN -> accept
        (0x30, 0, 0, 6),       # 12: IPv6 next header
        (0x15, 3, 0, 6),       # direct TCP -> 17
        (0x15, 5, 0, 0),       # extension chains parsed in userspace
        (0x15, 4, 0, 43),
        (0x15, 3, 4, 60),
        (0x30, 0, 0, 53),      # 17: TCP flags after IPv6 base header
        (0x54, 0, 0, 0x17),
        (0x15, 0, 1, 2),
        (0x06, 0, 0, 256),     # 20: accept bounded header bytes
        (0x06, 0, 0, 0),       # 21: reject
    ]
    array = (Instruction * len(instructions))(*(Instruction(*row) for row in instructions))
    program = Program(len(instructions), array)
    packet_socket.setsockopt(socket.SOL_SOCKET, 26, bytes(program))  # SO_ATTACH_FILTER
    packet_socket.setsockopt(socket.SOL_SOCKET, 44, 1)  # SO_LOCK_FILTER


def parse_syn(data: bytes, protocol: int) -> tuple[str, str, int] | None:
    """Decode bounded IP/TCP headers. Fragments and unsupported extensions are ignored."""
    # AF_PACKET/SOCK_DGRAM removes link-layer headers; no Ethernet offsets assumed.
    if protocol == 0x0800:
        if len(data) < 20 or data[0] >> 4 != 4:
            return None
        offset = (data[0] & 15) * 4
        total = int.from_bytes(data[2:4], "big")
        if offset < 20 or data[9] != 6 or int.from_bytes(data[6:8], "big") & 0x3fff:
            return None
        source, target = str(ipaddress.ip_address(data[12:16])), str(ipaddress.ip_address(data[16:20]))
    elif protocol == 0x86dd:
        if len(data) < 40 or data[0] >> 4 != 6:
            return None
        source, target = str(ipaddress.ip_address(data[8:24])), str(ipaddress.ip_address(data[24:40]))
        offset, next_header = 40, data[6]
        total = 40 + int.from_bytes(data[4:6], "big")
        for _ in range(8):
            if next_header == 6:
                break
            if next_header not in (0, 43, 60) or offset + 2 > min(len(data), total):
                return None  # includes fragments, ESP, AH and unsupported protocols
            next_header, length = data[offset], (data[offset + 1] + 1) * 8
            offset += length
        if next_header != 6:
            return None
    else:
        return None
    if offset + 20 > min(len(data), total):
        return None
    header_length = (data[offset + 12] >> 4) * 4
    flags = data[offset + 13]
    if header_length < 20 or offset + header_length > min(len(data), total):
        return None
    if not flags & 2 or flags & 0x15:  # SYN only; no ACK, RST or FIN
        return None
    port = int.from_bytes(data[offset + 2:offset + 4], "big")
    return (source, target, port) if port else None


def local_addresses(interface: str = "") -> set[str]:
    result = subprocess.run(["ip", "-j", "address", "show"], capture_output=True, text=True, timeout=5, check=True)
    return {str(ipaddress.ip_address(address["local"])) for link in json.loads(result.stdout)
            if not interface or link["ifname"] == interface
            for address in link.get("addr_info", []) if address.get("family") in {"inet", "inet6"}}


def policy_from_env() -> ScanPolicy:
    def values(name):
        return tuple(value.strip() for value in os.environ.get(name, "").split(",") if value.strip())
    return ScanPolicy(sensor_id=os.environ.get("NMS_SCAN_SENSOR_ID", "dev-inbound"),
                      device_id=os.environ.get("NMS_SCAN_DEVICE_ID", "dev"),
                      targets=values("NMS_SCAN_TARGETS"), internal_networks=values("NMS_SCAN_INTERNAL_CIDRS"),
                      interface=os.environ.get("NMS_SCAN_INTERFACE", ""),
                      fast_ports=int(os.environ.get("NMS_SCAN_FAST_PORTS", "8")),
                      slow_ports=int(os.environ.get("NMS_SCAN_SLOW_PORTS", "20")))


def main() -> int:
    import fcntl  # Linux-only capture process; parsing remains portable for tests.
    policy = policy_from_env()
    database = Database(Path(os.environ.get("NMS_DB", "/var/lib/nms-team2/nms.sqlite3")))
    if not database.path.is_file():
        raise RuntimeError("Start the web service and initialize its database first")
    version = database.row("PRAGMA user_version")["user_version"]
    if version != CURRENT_SCHEMA_VERSION:
        raise RuntimeError("Web service and sensor database versions must match")
    lock_path = database.path.parent / (".scan-" + hashlib.sha256(policy.sensor_id.encode()).hexdigest()[:16] + ".lock")
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        detector = ScanDetector(database, policy, time.time())
        stopped = False
        def stop(*_):
            nonlocal stopped
            stopped = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        packet_socket = None
        try:
            expected = {str(ipaddress.ip_address(value)) for value in policy.targets}
            if not expected.issubset(local_addresses(policy.interface)):
                raise ValueError("Configured capture targets must exist on this host/interface")
            packet_socket = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, socket.htons(3))
            attach_syn_filter(packet_socket)
            if policy.interface:
                packet_socket.bind((policy.interface, 0))
            packet_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            packet_socket.settimeout(1)
            # No promiscuous membership, firewall changes, connection attempts or packet files.
            last_flush = last_addresses = time.monotonic()
            detector.flush(time.time())
            while not stopped:
                try:
                    data, address = packet_socket.recvfrom(256)
                    if address[2] == 0:  # PACKET_HOST; outgoing copies never counted
                        observation = parse_syn(data, address[1])
                        if observation:
                            detector.observe(*observation, time.time())
                except socket.timeout:
                    pass
                current = time.monotonic()
                if current - last_flush >= 5:
                    # Linux packet statistics are reset on read; report cumulative loss ourselves.
                    _, dropped = struct.unpack("II", packet_socket.getsockopt(263, 6, 8))
                    if dropped:
                        detector.gap(time.time(), kernel_drops=dropped)
                    detector.flush(time.time())
                    last_flush = current
                if current - last_addresses >= 30:
                    if not expected.issubset(local_addresses(policy.interface)):
                        raise RuntimeError("Capture address disappeared; observation scope is unavailable")
                    last_addresses = current
            detector.flush(time.time(), state="stopped")
        except Exception as exc:
            detector.flush(time.time(), state="error", error=f"{type(exc).__name__}: {str(exc)[:200]}")
            raise
        finally:
            if packet_socket is not None:
                packet_socket.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
