#!/usr/bin/env python3
"""Codex hook entry point for the Codex Usage Stick plugin.

This wrapper writes a small diagnostic record before it starts the BLE bridge.
That makes hook loading problems distinguishable from bridge startup problems.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import select
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
START_BRIDGE = PLUGIN_ROOT / "scripts" / "start_bridge.py"
STATE_DIR = Path.home() / ".codex" / "codex-usage-bridge"
HOOK_LOG_PATH = STATE_DIR / "hook.log"
APPROVAL_SOCK_PATH = STATE_DIR / "approval.sock"
APPROVAL_ENDPOINT_PATH = STATE_DIR / "approval_endpoint.json"
APPROVAL_WAIT_SEC = 45.0
APPROVAL_CONNECT_SEC = 4.0


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def read_stdin_text() -> str:
    """Read one hook payload without using select() on Windows pipes."""
    try:
        if sys.stdin is None or sys.stdin.closed or sys.stdin.isatty():
            return ""
        if os.name == "nt":
            return sys.stdin.read(65536)
        ready, _, _ = select.select([sys.stdin], [], [], 0)
        if not ready:
            return ""
        return sys.stdin.read(65536)
    except Exception as exc:  # pragma: no cover - diagnostic best effort
        return f"<stdin unavailable: {exc}>"


def append_log(record: dict[str, Any]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with HOOK_LOG_PATH.open("a", encoding="utf-8") as log:
            log.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass


def env_snapshot() -> dict[str, str | None]:
    keys = [
        "PLUGIN_ROOT",
        "PLUGIN_DATA",
        "CLAUDE_PLUGIN_ROOT",
        "CLAUDE_PLUGIN_DATA",
        "CODEX_HOME",
        "PWD",
    ]
    return {key: os.environ.get(key) for key in keys}


def permission_output(behavior: str, message: str) -> dict[str, Any]:
    return {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": behavior,
                "message": message,
            },
        },
    }


def normalize_json_strings(value: Any) -> Any:
    """Recover surrogate-escaped UTF-8 and keep approval JSON encodable."""
    if isinstance(value, str):
        try:
            return value.encode("utf-8", errors="surrogateescape").decode(
                "utf-8", errors="replace"
            )
        except UnicodeEncodeError:
            return value.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(value, dict):
        return {
            normalize_json_strings(key): normalize_json_strings(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [normalize_json_strings(item) for item in value]
    return value


def request_hardware_permission(hook_payload: dict[str, Any]) -> dict[str, Any] | None:
    request = {
        "type": "permission_request",
        "hook": normalize_json_strings(hook_payload),
        "timeout": APPROVAL_WAIT_SEC,
    }
    connect_deadline = time.monotonic() + APPROVAL_CONNECT_SEC
    last_error = ""
    endpoint_description = str(APPROVAL_ENDPOINT_PATH if os.name == "nt" else APPROVAL_SOCK_PATH)

    while True:
        try:
            connect_timeout = max(0.2, min(1.0, connect_deadline - time.monotonic()))
            authenticated_request = dict(request)
            if os.name == "nt":
                endpoint = json.loads(APPROVAL_ENDPOINT_PATH.read_text(encoding="utf-8"))
                if not isinstance(endpoint, dict) or endpoint.get("version") != 1:
                    raise ValueError("invalid approval endpoint version")
                host = endpoint.get("host")
                port = endpoint.get("port")
                token = endpoint.get("token")
                if host != "127.0.0.1":
                    raise ValueError("approval endpoint must use IPv4 loopback")
                if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                    raise ValueError("invalid approval endpoint port")
                if not isinstance(token, str) or len(token) < 32:
                    raise ValueError("invalid approval endpoint token")
                authenticated_request["token"] = token
                approval_socket = socket.create_connection((host, port), timeout=connect_timeout)
            else:
                approval_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                approval_socket.settimeout(connect_timeout)
                approval_socket.connect(str(APPROVAL_SOCK_PATH))

            encoded = (
                json.dumps(authenticated_request, separators=(",", ":"), ensure_ascii=False) + "\n"
            ).encode("utf-8")
            with approval_socket as sock:
                sock.sendall(encoded)
                sock.settimeout(APPROVAL_WAIT_SEC + 2.0)
                raw = sock.makefile("rb").readline(4096)
            if not raw:
                append_log({"time": now_iso(), "event": "PermissionRequest", "phase": "approval_ipc_empty"})
                return None
            response = json.loads(raw.decode("utf-8", errors="replace"))
            append_log({
                "time": now_iso(),
                "event": "PermissionRequest",
                "phase": "approval_ipc_response",
                "response": response,
            })
            if not response.get("ok"):
                return None
            decision = response.get("decision")
            if decision == "allow":
                return permission_output("allow", "Approved from StickS3")
            if decision == "deny":
                return permission_output("deny", "Denied from StickS3")
            return None
        except (FileNotFoundError, ConnectionRefusedError, json.JSONDecodeError, socket.timeout, OSError, ValueError) as exc:
            last_error = type(exc).__name__
            if time.monotonic() >= connect_deadline:
                append_log({
                    "time": now_iso(),
                    "event": "PermissionRequest",
                    "phase": "approval_ipc_unavailable",
                    "endpoint": endpoint_description,
                    "error_type": last_error,
                })
                return None
            time.sleep(0.2)
        except Exception as exc:  # pragma: no cover - keep hook fail-open
            append_log({
                "time": now_iso(),
                "event": "PermissionRequest",
                "phase": "approval_ipc_error",
                "error_type": type(exc).__name__,
            })
            return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Codex Usage Stick hook entry point.")
    parser.add_argument("--event", default="unknown", help="Hook event name")
    args = parser.parse_args()

    stdin_text = read_stdin_text()
    append_log({
        "time": now_iso(),
        "event": args.event,
        "phase": "received",
        "argv": sys.argv,
        "cwd": os.getcwd(),
        "plugin_root": str(PLUGIN_ROOT),
        "env": env_snapshot(),
        "stdin_bytes": len(stdin_text.encode("utf-8", errors="replace")),
    })

    try:
        proc = subprocess.run(
            [sys.executable, str(START_BRIDGE)],
            cwd=str(PLUGIN_ROOT),
            capture_output=True,
            text=True,
            timeout=6,
            check=False,
        )
        append_log({
            "time": now_iso(),
            "event": args.event,
            "phase": "start_bridge",
            "returncode": proc.returncode,
            "stdout": proc.stdout[-2000:],
            "stderr": proc.stderr[-2000:],
        })
    except Exception as exc:  # pragma: no cover - hook must stay non-fatal
        append_log({
            "time": now_iso(),
            "event": args.event,
            "phase": "error",
            "error": repr(exc),
        })

    if args.event == "PermissionRequest":
        try:
            hook_payload = json.loads(stdin_text) if stdin_text else {}
        except json.JSONDecodeError as exc:
            append_log({
                "time": now_iso(),
                "event": args.event,
                "phase": "permission_json_error",
                "error": repr(exc),
            })
            return 0

        decision = request_hardware_permission(hook_payload)
        if decision:
            sys.stdout.write(json.dumps(decision, separators=(",", ":"), ensure_ascii=False))
            sys.stdout.write("\n")
            sys.stdout.flush()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
