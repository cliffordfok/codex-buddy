#!/usr/bin/env python3
"""Start the Codex Usage Stick BLE bridge once per user session."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
BRIDGE_SCRIPT = PLUGIN_ROOT / "scripts" / "codex_usage_ble_bridge.py"
STATE_DIR = Path.home() / ".codex" / "codex-usage-bridge"
CONFIG_PATH = STATE_DIR / "config.json"
PID_PATH = STATE_DIR / "bridge.pid"
START_LOCK_PATH = STATE_DIR / "bridge-start.lock"
LOG_PATH = STATE_DIR / "bridge.log"
HOOK_LOG_PATH = STATE_DIR / "hook.log"
START_LOCK_TIMEOUT_SEC = 5.0
START_LOCK_STALE_SEC = 30.0

DEFAULT_CONFIG: dict[str, Any] = {
    "name": "Codex-",
    "address": None,
    "interval": 5.0,
    "scan_timeout": 8.0,
    "restart_delay": 5.0,
    "reconnect_max_delay": 10.0,
    "reconnect_reset_after": 30.0,
    "reconnect_attempts": 720,
    "notify_timeout": 10.0,
    "write_timeout": 10.0,
    "verbose": True,
    "no_approval_proxy": True,
    "no_appserver_usage": True,
}

SHUTDOWN = False


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    ensure_state_dir()
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
        return dict(DEFAULT_CONFIG)
    try:
        loaded = json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError:
        loaded = {}
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(loaded, dict):
        cfg.update(loaded)
    return cfg


def windows_process_alive(pid: int) -> bool:
    """Query a Windows process without using os.kill(pid, 0), which terminates it."""
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return windows_process_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def running_pid() -> int | None:
    try:
        pid = int(PID_PATH.read_text().strip())
    except (OSError, ValueError):
        return None
    if process_alive(pid):
        return pid
    try:
        PID_PATH.unlink()
    except OSError:
        pass
    return None


def _start_lock_is_stale() -> bool:
    try:
        stat = START_LOCK_PATH.stat()
        text = START_LOCK_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return False

    age = max(0.0, time.time() - stat.st_mtime)
    if age >= START_LOCK_STALE_SEC:
        return True
    try:
        owner_pid = int(text)
    except ValueError:
        return False
    return not process_alive(owner_pid)


@contextmanager
def bridge_start_lock(timeout: float = START_LOCK_TIMEOUT_SEC):
    """Serialize hook-driven starts so only one process can claim BLE."""
    ensure_state_dir()
    deadline = time.monotonic() + max(0.0, timeout)
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(
                START_LOCK_PATH,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            if _start_lock_is_stale():
                try:
                    START_LOCK_PATH.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for bridge start lock")
            time.sleep(0.05)

    try:
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
    finally:
        os.close(fd)

    try:
        yield
    finally:
        try:
            START_LOCK_PATH.unlink()
        except OSError:
            pass


def bridge_command(cfg: dict[str, Any]) -> list[str]:
    cmd = [sys.executable, str(BRIDGE_SCRIPT)]
    name = cfg.get("name")
    if name:
        cmd.extend(["--name", str(name)])
    address = cfg.get("address")
    if address:
        cmd.extend(["--address", str(address)])
    if cfg.get("interval") is not None:
        cmd.extend(["--interval", str(cfg["interval"])])
    if cfg.get("scan_timeout") is not None:
        cmd.extend(["--scan-timeout", str(cfg["scan_timeout"])])
    if cfg.get("notify_timeout") is not None:
        cmd.extend(["--notify-timeout", str(cfg["notify_timeout"])])
    if cfg.get("write_timeout") is not None:
        cmd.extend(["--write-timeout", str(cfg["write_timeout"])])
    if cfg.get("restart_delay") is not None:
        cmd.extend(["--reconnect-delay", str(cfg["restart_delay"])])
    if cfg.get("reconnect_max_delay") is not None:
        cmd.extend(["--reconnect-max-delay", str(cfg["reconnect_max_delay"])])
    if cfg.get("reconnect_reset_after") is not None:
        cmd.extend(["--reconnect-reset-after", str(cfg["reconnect_reset_after"])])
    if cfg.get("reconnect_attempts") is not None:
        cmd.extend(["--reconnect-attempts", str(cfg["reconnect_attempts"])])
    if cfg.get("verbose", True):
        cmd.append("--verbose")
    # The installed Codex Usage Stick bridge is deliberately local-only.
    cmd.extend(["--no-approval-proxy", "--no-appserver-usage"])
    return cmd


def supervisor_command() -> list[str]:
    return [sys.executable, str(Path(__file__).resolve()), "--supervise"]


def background_command(cfg: dict[str, Any]) -> list[str]:
    # Windows runs the bridge directly so the PID always identifies the process
    # that owns BLE and can be stopped without leaving a child process behind.
    if os.name == "nt":
        return bridge_command(cfg)
    return supervisor_command()


def request_shutdown(_signum: int, _frame: object) -> None:
    global SHUTDOWN
    SHUTDOWN = True


def supervise_bridge() -> int:
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    while not SHUTDOWN:
        cfg = load_config()
        proc = subprocess.Popen(bridge_command(cfg), cwd=str(PLUGIN_ROOT))
        while proc.poll() is None:
            if SHUTDOWN:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break
            time.sleep(1)
        if not SHUTDOWN:
            delay = float(cfg.get("restart_delay", 5.0) or 5.0)
            time.sleep(max(1.0, delay))
    return 0


def start_bridge(foreground: bool = False) -> int:
    cfg = load_config()
    if not BRIDGE_SCRIPT.exists():
        return 2

    if foreground:
        return subprocess.call(bridge_command(cfg), cwd=str(PLUGIN_ROOT))

    try:
        with bridge_start_lock():
            pid = running_pid()
            if pid is not None:
                return 0

            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            with LOG_PATH.open("ab") as log:
                proc = subprocess.Popen(
                    background_command(cfg),
                    cwd=str(PLUGIN_ROOT),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    env=env,
                )
            PID_PATH.write_text(f"{proc.pid}\n")
            return 0
    except TimeoutError:
        return 1


def stop_bridge() -> int:
    pid = running_pid()
    if pid is None:
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        PID_PATH.unlink()
    except OSError:
        pass
    return 0


def status() -> int:
    cfg = load_config()
    pid = running_pid()
    state = "running" if pid is not None else "stopped"
    print(json.dumps({
        "state": state,
        "pid": pid,
        "config": str(CONFIG_PATH),
        "log": str(LOG_PATH),
        "hook_log": str(HOOK_LOG_PATH),
        "command": background_command(cfg),
        "bridge_command": bridge_command(cfg),
    }, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Start/stop the Codex Usage Stick BLE bridge.")
    parser.add_argument("--foreground", action="store_true", help="Run the bridge in the foreground")
    parser.add_argument("--status", action="store_true", help="Print bridge status")
    parser.add_argument("--stop", action="store_true", help="Stop the bridge")
    parser.add_argument("--supervise", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.supervise:
        return supervise_bridge()
    if args.status:
        return status()
    if args.stop:
        return stop_bridge()
    return start_bridge(foreground=args.foreground)


if __name__ == "__main__":
    raise SystemExit(main())
