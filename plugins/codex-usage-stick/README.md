# Codex Usage Stick Plugin

This local Codex plugin starts a BLE bridge that sends Codex usage data to a
StickS3 running the matching Codex Usage Stick firmware.

The plugin is local-first:

- It reads local Codex usage files.
- It starts one background bridge process.
- It sends compact usage packets over BLE.
- It writes diagnostics under `~/.codex/codex-usage-bridge/`.
- It does not send data to an external server.

The background process automatically rescans and reconnects after transient
Bluetooth, GATT, Stick reboot, or Windows resume failures. BLE and approval
IPC resources are closed before each retry, and retries use a bounded
exponential backoff. Turning the StickS3 LCD off does not stop BLE; after the
LCD is turned back on, the latest usage display returns without another Codex
prompt.

## Usage Values

Codex rollout files report quota consumption as `used_percent`. The bridge
maps each quota by its actual `window_minutes`: 300 minutes goes to the 5h row
and 10,080 minutes goes to the 7d row. Matching firmware displays `100 - used`
as percentage remaining, consistent with the Codex UI's `left` convention.

Expired reset timestamps and stale cache entries are reported as unavailable;
the bridge does not fabricate a future reset time.

## Hooks

The plugin registers:

```text
SessionStart
UserPromptSubmit
PermissionRequest
```

The hooks run:

```sh
python3 "$PLUGIN_ROOT/scripts/hook_entry.py"
```

The startup hooks return quickly: `hook_entry.py` writes a log line and asks
`start_bridge.py` to start or reuse the background bridge. The
`PermissionRequest` hook is synchronous and waits briefly for A/B on the
StickS3 before falling back to Codex's normal approval UI.

## Install From Codex UI

Open:

```text
Settings -> Plugins -> Add plugin marketplace
```

Fill the dialog like this:

```text
Source:
openelab-commits/codex-buddy

Git ref:
main
```

If this lives in your own fork, use your fork's `owner/repo`.

## CLI Fallback

```bash
codex plugin marketplace add openelab-commits/codex-buddy --ref main
codex plugin add codex-usage-stick@codex-usage-stick-marketplace
codex plugin list
```

For local development:

```bash
codex plugin marketplace add /path/to/codex-buddy
```

## Enable Hooks

Check and enable the stable hooks feature if needed:

```bash
codex features list
codex features enable hooks
```

If needed, enable the plugin in `~/.codex/config.toml`:

```toml
[plugins."codex-usage-stick@codex-usage-stick-marketplace"]
enabled = true
```

Restart Codex after changing plugin settings. Approve the hook trust prompt
when Codex shows it.

## Dependency

```bash
python -m pip install bleak
```

## Runtime Files

```text
~/.codex/codex-usage-bridge/config.json
~/.codex/codex-usage-bridge/hook.log
~/.codex/codex-usage-bridge/bridge.log
~/.codex/codex-usage-bridge/bridge.pid
```

## Config

Default `config.json`:

```json
{
  "name": "Codex-",
  "address": null,
  "interval": 5.0,
  "scan_timeout": 8.0,
  "restart_delay": 5.0,
  "reconnect_max_delay": 10.0,
  "reconnect_reset_after": 30.0,
  "reconnect_attempts": 720,
  "notify_timeout": 10.0,
  "write_timeout": 10.0,
  "verbose": true,
  "no_approval_proxy": true,
  "no_appserver_usage": true
}
```

`restart_delay` is the initial reconnect delay. It doubles up to
`reconnect_max_delay`; a stable session resets the delay. The default
`reconnect_attempts` permits about two hours of consecutive failures while
keeping retries bounded. `notify_timeout` and `write_timeout` prevent Windows
BLE calls from hanging the bridge indefinitely.

Use `address` if BLE name caching makes name scanning unreliable.
`no_approval_proxy` only disables the older app-server proxy experiment.
StickS3 approve/deny uses the `PermissionRequest` hook plus the local
authenticated approval endpoint and works with this value set to `true`.
`no_appserver_usage` keeps usage collection on local Codex rollout files.

## Commands

Check status:

```bash
python plugins/codex-usage-stick/scripts/start_bridge.py --status
```

Start:

```bash
python plugins/codex-usage-stick/scripts/start_bridge.py
```

Stop:

```bash
python plugins/codex-usage-stick/scripts/start_bridge.py --stop
```

Run in foreground:

```bash
python plugins/codex-usage-stick/scripts/start_bridge.py --foreground
```

Manual hook test:

```bash
python plugins/codex-usage-stick/scripts/hook_entry.py --event ManualTest
```

## Verify

Make sure Bluetooth is enabled on the computer.

For the first BLE pairing on a new computer, start with a local-only foreground
`busy` test so the operating system can show the pairing prompt:

```bash
python plugins/codex-usage-stick/scripts/codex_usage_ble_bridge.py --verbose --state busy --no-appserver-usage
```

The StickS3 should show a pairing code. Enter that code on the computer to
finish the BLE pairing. Once the hardware starts showing usage information,
stop the foreground test with `Command-C` / `Ctrl-C`.

Then submit a Codex prompt in a project where the plugin hook is trusted. On
Windows, inspect the logs with PowerShell:

```powershell
Get-Content -Tail 20 $env:USERPROFILE\.codex\codex-usage-bridge\hook.log
```

Expected:

```text
"event": "UserPromptSubmit"
```

Then check BLE packets:

```powershell
Get-Content -Tail 40 $env:USERPROFILE\.codex\codex-usage-bridge\bridge.log
```

Expected:

```text
sent {"state":"busy","tokens":...,"primary":...,"secondary":...}
```

## Approve / Deny

When Codex asks for a permission approval, the bridge forwards the prompt to
the StickS3 through a local `PermissionRequest` hook. Press A to allow or B to
deny. If the StickS3 is not connected or no button is pressed before timeout,
the hook returns no decision and Codex falls back to its normal local approval
flow.
