"""Windows account encrypted credential store and loopback-only DEV gateway.

The browser never receives the DEV token. No third-party packages are needed.
"""
from __future__ import annotations

import argparse
import ctypes
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

AUTH_PATH = "/api/v1/auth/status"
WRITE_PATH = re.compile(
    r"/api/v1/(?:collect|service-baselines|security/probe|tasks/iperf3"
    r"|events/[0-9]+/acknowledge|risks/[0-9]+/acknowledge|tasks/[a-zA-Z0-9-]+/cancel)$"
)
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade"}


def credential_path() -> Path:
    if os.name != "nt":
        raise RuntimeError("Run this helper with Windows Python.")
    return Path(os.environ["LOCALAPPDATA"]) / "NMS-Team2" / "admin-token.dpapi"


def crypt(data: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI is required.")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    raw = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(raw, ctypes.POINTER(ctypes.c_ubyte)))
    entropy_raw = ctypes.create_string_buffer(b"NMS-Team2:DEV-admin:v1")
    entropy = Blob(len(entropy_raw.value), ctypes.cast(entropy_raw, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    function = library.CryptUnprotectData if decrypt else library.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p if decrypt else wintypes.LPCWSTR,
                         ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN, current-user scope (not LOCAL_MACHINE).
    if not function(ctypes.byref(source), None if decrypt else "NMS Team2 DEV admin token",
                    ctypes.byref(entropy), None, None, 1, ctypes.byref(output)):
        raise RuntimeError("Windows could not decrypt this account's credential." if decrypt
                           else "Windows could not encrypt the credential.")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.memset(output.data, 0, output.size)
        kernel.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))


def validate_token(token: str) -> str:
    if not 16 <= len(token) <= 1024 or not all(33 <= ord(c) <= 126 for c in token):
        raise ValueError("Expected a nonempty printable ASCII admin token (16-1024 characters).")
    return token


def store_token(token: str) -> Path:
    token = validate_token(token)
    path = credential_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # An explicit per-user DACL; remove inherited access before writing even encrypted bytes.
    script = r"""
$ErrorActionPreference='Stop'
$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User
$acl=New-Object System.Security.AccessControl.DirectorySecurity
$acl.SetOwner($sid)
$acl.SetAccessRuleProtection($true,$false)
$rule=New-Object System.Security.AccessControl.FileSystemAccessRule($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
$acl.AddAccessRule($rule)
Set-Acl -LiteralPath $env:NMS_CREDENTIAL_DIRECTORY -AclObject $acl
"""
    env = os.environ.copy()
    env["NMS_CREDENTIAL_DIRECTORY"] = str(path.parent)
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            env=env, capture_output=True)
    if result.returncode:
        raise RuntimeError("Could not restrict the credential directory to the current Windows user.")
    encrypted = crypt(token.encode("ascii"))
    temporary = path.with_suffix(".new")
    try:
        temporary.write_bytes(encrypted)
        if crypt(temporary.read_bytes(), decrypt=True) != token.encode("ascii"):
            raise RuntimeError("Encrypted credential verification failed.")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def load_token() -> str:
    return validate_token(crypt(credential_path().read_bytes(), decrypt=True).decode("ascii"))


class Gateway(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, upstream_port: int, token: str):
        self.upstream_port = upstream_port
        self.token = token
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass  # Do not record request headers, credentials or payloads.

    def reject(self, code: int, message: str):
        self.close_connection = True
        payload = json.dumps({"status": "error", "data": None,
                              "error": {"message": message}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-NMS-Local-Gateway", "1")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self.forward()

    def do_POST(self):
        self.forward()

    def do_OPTIONS(self):
        self.reject(403, "Cross-origin access is not enabled.")

    def forward(self):
        self.connection.settimeout(75)
        server = self.server
        host = server.origin.removeprefix("http://")
        if self.headers.get_all("Host", []) != [host]:
            return self.reject(403, "Use this gateway's 127.0.0.1 address.")
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or not self.path.startswith("/") or self.path.startswith("//"):
            return self.reject(400, "Invalid local path.")
        if self.headers.get("Origin") not in (None, server.origin):
            return self.reject(403, "Origin is not allowed.")
        if self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
            return self.reject(403, "Cross-origin access is not allowed.")
        if self.headers.get("Sec-Fetch-Dest") in ("iframe", "frame"):
            return self.reject(403, "Embedded access is not allowed.")
        if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) > 1:
            return self.reject(400, "Unsupported request framing.")
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self.reject(400, "Invalid body size.")
        if size < 0 or size > 65536 or (self.command == "GET" and size):
            return self.reject(413, "Request body is too large or unexpected.")
        authorize = self.command == "POST" or parsed.path == AUTH_PATH
        if authorize:
            if self.headers.get("X-NMS-Local-Auth") != "1":
                return self.reject(403, "Local authorization header is required.")
            if self.command == "POST":
                if self.headers.get("Origin") != server.origin:
                    return self.reject(403, "Same-origin browser request required.")
                if not WRITE_PATH.fullmatch(parsed.path):
                    return self.reject(403, "This path is not allowed for local administration.")
        body = self.rfile.read(size) if size else None
        # Forward only an explicit header allowlist. Never forward arbitrary credentials or cookies.
        headers = {name: self.headers[name] for name in ("Accept", "Content-Type")
                   if name in self.headers}
        if authorize:
            headers["X-Admin-Token"] = server.token
        upstream = http.client.HTTPConnection("127.0.0.1", server.upstream_port, timeout=70)
        try:
            upstream.request(self.command, self.path, body=body, headers=headers)
            response = upstream.getresponse()
            data = response.read()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in HOP_HEADERS | {"content-length", "server", "date", "set-cookie",
                                                     "cache-control", "x-nms-local-gateway"}:
                    self.send_header(key, value)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            if parsed.path == AUTH_PATH:
                self.send_header("X-NMS-Local-Gateway", "1")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (OSError, http.client.HTTPException):
            self.reject(502, "DEV connection unavailable; restart the local connection.")
        finally:
            upstream.close()


def serve(args):
    token = load_token()
    # Reserve the public listener first so a startup collision cannot leave an orphan SSH child.
    server = Gateway(args.port, args.upstream_port, token)
    tunnel = None
    try:
        tunnel = subprocess.Popen(args.ssh_command, stdin=subprocess.DEVNULL)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if tunnel.poll() is not None:
                raise RuntimeError("SSH connection exited before the gateway was ready.")
            connection = http.client.HTTPConnection("127.0.0.1", args.upstream_port, timeout=2)
            try:
                connection.request("GET", AUTH_PATH, headers={"X-Admin-Token": token})
                response = connection.getresponse()
                result = json.loads(response.read())
                if response.status == 200 and result.get("data", {}).get("authenticated"):
                    break
                if response.status in (401, 503):
                    raise RuntimeError("Stored credential is no longer valid; retrieve the current DEV token.")
            except (OSError, http.client.HTTPException, json.JSONDecodeError):
                time.sleep(0.3)
            finally:
                connection.close()
        else:
            raise RuntimeError("DEV authorization check timed out.")
        def watch_tunnel():
            tunnel.wait()
            server.shutdown()
        threading.Thread(target=watch_tunnel, daemon=True).start()
        print(f"Local encrypted authorization ready: {server.origin}/", flush=True)
        server.serve_forever()
    finally:
        server.server_close()
        if tunnel is not None and tunnel.poll() is None:
            tunnel.terminate()
            try:
                tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tunnel.kill()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("store", help="Encrypt the token supplied only through stdin.")
    commands.add_parser("check", help="Check decryptability without printing the token.")
    run = commands.add_parser("serve")
    run.add_argument("--port", type=int, default=8000)
    run.add_argument("--upstream-port", type=int, default=18080)
    run.add_argument("--ssh-command", nargs=argparse.REMAINDER, required=True)
    args = parser.parse_args()
    try:
        if args.command == "store":
            path = store_token(sys.stdin.read(2048).strip())
            print(json.dumps({"stored": True, "encryption": "DPAPI CurrentUser", "path": str(path)}))
        elif args.command == "check":
            load_token()
            print(json.dumps({"decryptable": True, "encryption": "DPAPI CurrentUser"}))
        else:
            if not args.ssh_command or args.port == args.upstream_port:
                raise ValueError("Expected a separate upstream port and an SSH command.")
            serve(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Local authorization: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
