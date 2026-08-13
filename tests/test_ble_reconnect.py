from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "plugins" / "codex-usage-stick" / "scripts"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class BleReconnectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bridge = load_module(
            "codex_usage_ble_bridge_test",
            SCRIPTS / "codex_usage_ble_bridge.py",
        )
        cls.starter = load_module(
            "codex_usage_start_bridge_test",
            SCRIPTS / "start_bridge.py",
        )
        cls.hook = load_module(
            "codex_usage_hook_entry_test",
            SCRIPTS / "hook_entry.py",
        )

    def test_hook_reads_complete_pipe_payload(self) -> None:
        hook_path = SCRIPTS / "hook_entry.py"
        code = (
            "import importlib.util,sys;"
            f"p={str(hook_path)!r};"
            "s=importlib.util.spec_from_file_location('pipe_hook',p);"
            "m=importlib.util.module_from_spec(s);"
            "s.loader.exec_module(m);"
            "sys.stdout.write(m.read_stdin_text())"
        )
        payload = '{"hook_event_name":"UserPromptSubmit","prompt":"private"}'
        result = subprocess.run(
            [sys.executable, "-c", code],
            input=payload,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, payload)

    def test_permission_hook_uses_local_authenticated_ipc(self) -> None:
        async def exercise() -> tuple[dict, dict | None, bool, bool]:
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_root = Path(temp_dir)
                endpoint_path = temp_root / "approval_endpoint.json"
                sock_path = temp_root / "approval.sock"
                args = types.SimpleNamespace(
                    hook_approval_endpoint=endpoint_path,
                    hook_approval_sock=sock_path,
                    hook_approval_timeout=1.0,
                    verbose=False,
                    no_approval_proxy=True,
                )
                proxy = self.bridge.CodexApprovalProxy(args, object())
                observed_payload: dict = {}

                async def approve(payload, _timeout):
                    observed_payload.update(payload)
                    return "allow"

                proxy.request_hook_permission = approve
                old_endpoint = self.hook.APPROVAL_ENDPOINT_PATH
                old_sock = self.hook.APPROVAL_SOCK_PATH
                old_wait = self.hook.APPROVAL_WAIT_SEC
                old_connect = self.hook.APPROVAL_CONNECT_SEC
                self.hook.APPROVAL_ENDPOINT_PATH = endpoint_path
                self.hook.APPROVAL_SOCK_PATH = sock_path
                self.hook.APPROVAL_WAIT_SEC = 1.0
                self.hook.APPROVAL_CONNECT_SEC = 1.0
                try:
                    await proxy.start_ipc_server()
                    if sys.platform == "win32":
                        endpoint = json.loads(endpoint_path.read_text(encoding="utf-8"))
                        self.assertEqual(endpoint["host"], "127.0.0.1")
                        self.assertGreaterEqual(len(endpoint["token"]), 32)
                    decision = await asyncio.to_thread(
                        self.hook.request_hardware_permission,
                        {"tool_name": "safe_test", "description": "local only"},
                    )
                finally:
                    await proxy.close_ipc_server()
                    self.hook.APPROVAL_ENDPOINT_PATH = old_endpoint
                    self.hook.APPROVAL_SOCK_PATH = old_sock
                    self.hook.APPROVAL_WAIT_SEC = old_wait
                    self.hook.APPROVAL_CONNECT_SEC = old_connect
                return observed_payload, decision, endpoint_path.exists(), sock_path.exists()

        observed, decision, endpoint_exists, sock_exists = asyncio.run(exercise())
        self.assertEqual(observed["tool_name"], "safe_test")
        self.assertEqual(
            decision,
            self.hook.permission_output("allow", "Approved from StickS3"),
        )
        self.assertFalse(endpoint_exists)
        self.assertFalse(sock_exists)

    def test_background_command_stays_local_only_and_configures_reconnect(self) -> None:
        command = self.starter.background_command(dict(self.starter.DEFAULT_CONFIG))
        self.assertEqual(command.count("--no-appserver-usage"), 1)
        self.assertEqual(command.count("--no-approval-proxy"), 1)
        expected_options = {
            "--notify-timeout": "10.0",
            "--write-timeout": "10.0",
            "--reconnect-delay": "5.0",
            "--reconnect-max-delay": "10.0",
            "--reconnect-reset-after": "30.0",
            "--reconnect-attempts": "720",
        }
        for option, expected_value in expected_options.items():
            option_index = command.index(option)
            self.assertEqual(command[option_index + 1], expected_value)

    def test_bridge_loop_retries_windows_gatt_abort(self) -> None:
        async def exercise() -> tuple[int, list[float], str]:
            args = self.bridge.build_parser().parse_args([])
            args.reconnect_delay = 0.1
            args.reconnect_max_delay = 0.2
            args.reconnect_attempts = 3
            calls = 0
            sleeps: list[float] = []

            async def fake_session(_args, _tracker) -> float:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise self.bridge.BleRetryableError(
                        "GATT write failed: [WinError -2147467260] operation aborted"
                    )
                return 1.0

            async def fake_sleep(delay: float) -> None:
                sleeps.append(delay)

            old_session = self.bridge.run_ble_session
            old_sleep = self.bridge.asyncio.sleep
            stderr = io.StringIO()
            self.bridge.run_ble_session = fake_session
            self.bridge.asyncio.sleep = fake_sleep
            try:
                with contextlib.redirect_stderr(stderr):
                    await self.bridge.bridge_loop(args)
            finally:
                self.bridge.run_ble_session = old_session
                self.bridge.asyncio.sleep = old_sleep
            return calls, sleeps, stderr.getvalue()

        calls, sleeps, stderr = asyncio.run(exercise())
        self.assertEqual(calls, 2)
        self.assertEqual(sleeps, [0.1])
        self.assertIn("reconnecting in 0.1s", stderr)

    def test_non_ble_os_error_is_not_retried(self) -> None:
        async def exercise() -> int:
            args = self.bridge.build_parser().parse_args([])
            calls = 0

            async def missing_rollout(_args, _tracker) -> float:
                nonlocal calls
                calls += 1
                raise FileNotFoundError("state_5.sqlite")

            old_session = self.bridge.run_ble_session
            self.bridge.run_ble_session = missing_rollout
            try:
                with self.assertRaises(FileNotFoundError):
                    await self.bridge.bridge_loop(args)
            finally:
                self.bridge.run_ble_session = old_session
            return calls

        self.assertEqual(asyncio.run(exercise()), 1)

    def test_gatt_timeout_covers_the_whole_packet(self) -> None:
        class SlowClient:
            def __init__(self) -> None:
                self.writes = 0

            async def write_gatt_char(self, *_args, **_kwargs) -> None:
                self.writes += 1
                await asyncio.sleep(0.006)

        async def exercise() -> int:
            client = SlowClient()
            args = types.SimpleNamespace(
                chunk_size=1,
                no_response=False,
                write_timeout=0.02,
                chunk_delay=0.0,
            )
            session = self.bridge.BleSession(args, client)
            with self.assertRaises(self.bridge.BleRetryableError):
                await session.write_json({"state": "idle"})
            return client.writes

        writes = asyncio.run(exercise())
        self.assertLess(writes, len('{"state":"idle"}\n'))

    def test_notification_setup_timeout_disconnects_for_retry(self) -> None:
        async def exercise() -> int:
            disconnects = 0

            class HangingNotifyClient:
                def __init__(self, _device, disconnected_callback, **_kwargs) -> None:
                    self.disconnected_callback = disconnected_callback

                async def connect(self) -> None:
                    return None

                async def disconnect(self) -> None:
                    nonlocal disconnects
                    disconnects += 1

                async def start_notify(self, _uuid, _callback) -> None:
                    await asyncio.Event().wait()

            async def fake_find_device(*_args):
                return object()

            args = self.bridge.build_parser().parse_args([])
            args.notify_timeout = 0.01
            old_client = self.bridge.BleakClient
            old_find = self.bridge.find_device
            self.bridge.BleakClient = HangingNotifyClient
            self.bridge.find_device = fake_find_device
            try:
                with self.assertRaisesRegex(
                    self.bridge.BleRetryableError,
                    "notification setup failed",
                ):
                    await self.bridge.run_ble_session(
                        args,
                        self.bridge.ActivityTracker(),
                    )
            finally:
                self.bridge.BleakClient = old_client
                self.bridge.find_device = old_find
            return disconnects

        self.assertEqual(asyncio.run(exercise()), 1)

    def test_real_gatt_abort_reconnects_and_sends_again(self) -> None:
        async def exercise() -> tuple[int, list[int], int]:
            success = asyncio.Event()
            disconnects: list[int] = []
            writes = 0

            class FakeClient:
                instances = 0

                def __init__(self, _device, disconnected_callback, **_kwargs) -> None:
                    type(self).instances += 1
                    self.number = type(self).instances
                    self.disconnected_callback = disconnected_callback

                async def connect(self) -> None:
                    return None

                async def disconnect(self) -> None:
                    disconnects.append(self.number)

                async def start_notify(self, _uuid, _callback) -> None:
                    return None

                async def write_gatt_char(self, *_args, **_kwargs) -> None:
                    nonlocal writes
                    if self.number == 1:
                        raise OSError(-2147467260, "operation aborted")
                    writes += 1
                    success.set()

            class FakeApprovals:
                def __init__(self, _args, _ble) -> None:
                    pass

                async def start_ipc_server(self) -> None:
                    return None

                async def start(self) -> None:
                    return None

                async def handle_device_message(self, _message) -> None:
                    return None

                async def close_ipc_server(self) -> None:
                    return None

                def has_pending(self) -> bool:
                    return False

            async def fake_find_device(*_args):
                return object()

            snapshot = self.bridge.UsageSnapshot(
                tokens=1,
                primary=2,
                secondary=3,
                primary_resets_at=0,
                secondary_resets_at=0,
                source=Path("test.jsonl"),
                event_ts=time.time(),
                limit_id="codex",
                limit_name=None,
            )

            args = self.bridge.build_parser().parse_args([])
            args.interval = 60.0
            args.chunk_delay = 0.0
            args.reconnect_delay = 0.01
            args.reconnect_max_delay = 0.01
            args.reconnect_attempts = 3

            old_client = self.bridge.BleakClient
            old_find = self.bridge.find_device
            old_approvals = self.bridge.CodexApprovalProxy
            old_read = self.bridge.read_usage
            self.bridge.BleakClient = FakeClient
            self.bridge.find_device = fake_find_device
            self.bridge.CodexApprovalProxy = FakeApprovals
            self.bridge.read_usage = lambda _args: snapshot
            task = None
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    task = asyncio.create_task(self.bridge.bridge_loop(args))
                    await asyncio.wait_for(success.wait(), timeout=1.0)
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            finally:
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                self.bridge.BleakClient = old_client
                self.bridge.find_device = old_find
                self.bridge.CodexApprovalProxy = old_approvals
                self.bridge.read_usage = old_read
            return FakeClient.instances, disconnects, writes

        instances, disconnects, writes = asyncio.run(exercise())
        self.assertEqual(instances, 2)
        self.assertEqual(disconnects, [1, 2])
        self.assertGreaterEqual(writes, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
