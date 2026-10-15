"""Short, bounded iperf3 experiments between the three documented hosts."""

from __future__ import annotations

import ipaddress
import json
import re
import selectors
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass, field
from uuid import uuid4

from app.core.config import Settings
from app.core.db import Database, decode, encode, utc_now


class TaskConflict(Exception):
    pass


class TaskUnavailable(Exception):
    pass


class TaskCancelled(Exception):
    pass


@dataclass
class IperfTaskManager:
    database: Database
    settings: Settings
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _active: dict[str, threading.Event] = field(default_factory=dict)
    _endpoints: set[str] = field(default_factory=set)

    def _device(self, device_id: str) -> dict:
        device = self.database.row("SELECT * FROM devices WHERE device_id=? AND role='test-host'", (device_id,))
        if not device:
            raise ValueError("iperf3 target must be a documented test host")
        ipaddress.IPv4Address(device["address"])
        return device

    def create(self, client_device_id: str, server_device_id: str,
               duration_s: int = 2, target_bps: int = 10_000_000) -> dict:
        if not self.settings.enable_live or not self.settings.enable_iperf3:
            raise TaskUnavailable("iperf3 tasks are disabled on this server")
        if client_device_id == server_device_id:
            raise ValueError("client and server must differ")
        if not 1 <= duration_s <= 3 or not 1 <= target_bps <= 10_000_000:
            raise ValueError("allowed test range: 1–3 seconds, at most 10 Mbit/s")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]*", self.settings.iperf_ssh_user):
            raise TaskUnavailable("invalid SSH user configuration")
        client = self._device(client_device_id)
        server = self._device(server_device_id)
        task_id = str(uuid4())
        parameters = {"duration_s": duration_s, "target_bps": target_bps, "protocol": "tcp"}
        with self._lock:
            if client_device_id in self._endpoints or server_device_id in self._endpoints:
                raise TaskConflict("an iperf3 endpoint is already in use")
            self._endpoints.update((client_device_id, server_device_id))
            cancellation = threading.Event()
            self._active[task_id] = cancellation
            try:
                with self.database.connect() as db:
                    db.execute(
                        """INSERT INTO tasks(task_id,type,task_state,client_device_id,
                           server_device_id,params,created_at,data_source)
                           VALUES (?,'iperf3','pending',?,?,?,?,'live')""",
                        (task_id, client_device_id, server_device_id, encode(parameters), utc_now()),
                    )
                worker = threading.Thread(
                    target=self._worker, args=(task_id, client, server, parameters, cancellation),
                    name=f"iperf3-{task_id[:8]}", daemon=True,
                )
                worker.start()
            except BaseException:
                self._active.pop(task_id, None)
                self._endpoints.difference_update((client_device_id, server_device_id))
                raise
        return self.get(task_id)

    def get(self, task_id: str) -> dict | None:
        task = self.database.row("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        if task:
            for name in ("params", "result", "error"):
                task[name] = decode(task[name])
        return task

    def cancel(self, task_id: str) -> dict | None:
        task = self.get(task_id)
        if task is None:
            return None
        with self._lock:
            cancellation = self._active.get(task_id)
            if cancellation:
                cancellation.set()
        return self.get(task_id)

    def _state(self, task_id: str, state: str, result: dict | None = None,
               error: dict | None = None) -> None:
        with self.database.connect() as db:
            if state == "running":
                db.execute("UPDATE tasks SET task_state='running',started_at=? WHERE task_id=?",
                           (utc_now(), task_id))
            else:
                db.execute(
                    """UPDATE tasks SET task_state=?,finished_at=?,result=?,error=?
                       WHERE task_id=?""",
                    (state, utc_now(), encode(result) if result else None,
                     encode(error) if error else None, task_id),
                )

    def _worker(self, task_id: str, client: dict, server: dict,
                params: dict, cancellation: threading.Event) -> None:
        try:
            if cancellation.is_set():
                raise TaskCancelled()
            self._state(task_id, "running")
            result = self._execute(client, server, params, cancellation)
            if cancellation.is_set():
                raise TaskCancelled()
            self._state(task_id, "succeeded", result=result)
        except TaskCancelled:
            self._state(task_id, "cancelled", error={"code": "CANCELLED", "message": "任务已取消"})
        except Exception as exc:
            self._state(task_id, "failed", error={"code": "IPERF_FAILED", "message": str(exc)[:220]})
        finally:
            with self._lock:
                self._active.pop(task_id, None)
                self._endpoints.difference_update((client["device_id"], server["device_id"]))

    def _ssh(self, address: str, remote_command: str) -> list[str]:
        command = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                   "-o", "ConnectTimeout=5", "-o", "ConnectionAttempts=1"]
        if self.settings.iperf_ssh_key:
            command += ["-i", str(self.settings.iperf_ssh_key)]
        return command + [f"{self.settings.iperf_ssh_user}@{address}", remote_command]

    @staticmethod
    def _server_command(port: int) -> str:
        port_hex = f"{port:04X}"
        return (
            "server_pid=; "
            "cleanup_server() { "
            "if [ -n \"$server_pid\" ] && kill -0 \"$server_pid\" 2>/dev/null; then "
            "kill \"$server_pid\" 2>/dev/null || :; "
            "fi; "
            "if [ -n \"$server_pid\" ]; then wait \"$server_pid\" 2>/dev/null || :; fi; "
            "}; "
            "trap cleanup_server EXIT; trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 143' TERM; "
            f"timeout 15s iperf3 -s -1 -J -p {port} & server_pid=$!; "
            f"port_hex={port_hex}; attempt=0; "
            "while [ \"$attempt\" -lt 50 ]; do "
            "if ! kill -0 \"$server_pid\" 2>/dev/null; then wait \"$server_pid\"; exit 1; fi; "
            "if awk -v port=\"$port_hex\" '$2 ~ (\":\" port \"$\") && $4 == \"0A\" {found=1} END {exit !found}' /proc/net/tcp 2>/dev/null || "
            "{ [ -r /proc/net/tcp6 ] && awk -v port=\"$port_hex\" '$2 ~ (\":\" port \"$\") && $4 == \"0A\" {found=1} END {exit !found}' /proc/net/tcp6 2>/dev/null; }; then "
            "printf 'NMS_IPERF_READY\\n'; wait \"$server_pid\"; exit $?; "
            "fi; "
            "attempt=$((attempt + 1)); sleep 0.1; "
            "done; exit 1"
        )

    def _wait_for_server_ready(self, server_proc: subprocess.Popen,
                               cancellation: threading.Event, timeout_s: float = 5.0) -> None:
        if server_proc.stdout is None:
            raise RuntimeError("iperf3 server readiness channel is unavailable")
        deadline = time.monotonic() + timeout_s
        with selectors.DefaultSelector() as ready:
            ready.register(server_proc.stdout, selectors.EVENT_READ)
            while True:
                if cancellation.is_set():
                    raise TaskCancelled()
                if server_proc.poll() is not None:
                    raise RuntimeError("server SSH exited before iperf3 became ready")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("iperf3 server readiness timed out")
                if not ready.select(min(0.2, remaining)):
                    continue
                line = server_proc.stdout.readline()
                if not line:
                    raise RuntimeError("server SSH closed before iperf3 became ready")
                if line.strip() == "NMS_IPERF_READY":
                    return

    def _execute(self, client: dict, server: dict, params: dict,
                 cancellation: threading.Event) -> dict:
        port = 52000 + secrets.randbelow(1000)
        server_command = self._server_command(port)
        client_command = (f"iperf3 -c {server['address']} -J -t {params['duration_s']} "
                          f"-b {params['target_bps']} -p {port}")
        server_proc = subprocess.Popen(self._ssh(server["address"], server_command),
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        client_proc = None
        try:
            self._wait_for_server_ready(server_proc, cancellation)
            client_proc = subprocess.Popen(self._ssh(client["address"], client_command),
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            deadline = time.monotonic() + params["duration_s"] + 18
            while True:
                if cancellation.is_set():
                    raise TaskCancelled()
                if time.monotonic() > deadline:
                    raise TimeoutError("iperf3 client timed out")
                try:
                    stdout, stderr = client_proc.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if client_proc.returncode != 0:
                raise RuntimeError("iperf3 client exited unsuccessfully: " + stderr.strip()[:140])
            parsed = json.loads(stdout)
            if parsed.get("error"):
                raise RuntimeError("iperf3 reported a client error")
            end = parsed.get("end", {})
            return {
                "client_device_id": client["device_id"],
                "server_device_id": server["device_id"],
                "duration_s": params["duration_s"], "target_bps": params["target_bps"],
                "sent_bytes": end.get("sum_sent", {}).get("bytes"),
                "received_bytes": end.get("sum_received", {}).get("bytes"),
                "sent_bps": end.get("sum_sent", {}).get("bits_per_second"),
                "received_bps": end.get("sum_received", {}).get("bits_per_second"),
                "retransmits": end.get("sum_sent", {}).get("retransmits"),
                "note": "短时限速测试结果；不代表链路最大容量。",
            }
        finally:
            for proc in (client_proc, server_proc):
                if proc is None:
                    continue
                if proc.poll() is None:
                    proc.terminate()
                try:
                    proc.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
