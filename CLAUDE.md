# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A standalone Python integration tool that syncs biometric attendance data into ERPNext's Employee Checkin doctype. Despite living under `frappe-bench15/apps/`, it is **not a Frappe app** — it has no `hooks.py`, no `modules.txt`, is not installed via `bench install-app`, and is never migrated. It runs as an independent script (via cron/systemd on Linux, or as a Windows service) that talks to ERPNext purely over its REST API.

It supports two attendance sources, run per cycle from `main()`:
- **ZKBioTime** (`biotime_url` config, `config.ERPNEXT_VERSION`-aware) — pulls punches from a ZKBioTime server's HTTP API (`/iclock/api/...`), the primary/current path.
- **Direct ZK device polling** (`devices` config, via `pyzk`) — connects directly to biometric device IP:port and pulls punches with the `zk` library; this is the original/legacy path (based on `frappe/biometric-attendance-sync-tool`).

A PyQt5 GUI (`gui.py`) exists for configuring `local_config.py` interactively instead of hand-editing it.

## Commands

```bash
# Install dependencies
python -m pip install -r requirements.txt

# Run one sync cycle manually (respects PULL_FREQUENCY internally — see infinite_loop)
python erpnext_sync.py

# Launch the config GUI (writes local_config.py)
python install.py
# or directly:
python gui.py

# Windows: install/run as a Windows service (requires pywin32)
python erpnext_sync_win.py install
python erpnext_sync_win.py start
```

There is no test suite, linter, or build step in this repo.

## Configuration

All runtime configuration lives in `local_config.py` (gitignored-optional; a template is provided at `local_config.py.template` — copy it to `local_config.py` and fill in real values). Key settings:

- `ERPNEXT_URL`, `ERPNEXT_API_KEY`, `ERPNEXT_API_SECRET`, `ERPNEXT_VERSION` — target ERPNext site and auth.
- `biotime_url` — list of ZKBioTime server(s): `BASE_URL`, `USERNAME`, `PASSWORD`, `device_id`.
- `devices` — list of direct ZK devices (legacy path): `device_id`, `ip`, `punch_direction`, `clear_from_device_on_fetch`.
- `PULL_FREQUENCY` — minutes between sync cycles.
- `shift_type_device_mapping` — maps ERPNext Shift Type(s) to device/terminal IDs, used to update each Shift Type's "last sync of checkin" timestamp (see inline comment in the template linking to the ERPNext forum thread explaining why this matters for auto-attendance).
- `TERMINAL_TIMEOUT`, `expected_terminals`, `require_all_terminals` (per shift mapping entry) — multi-terminal connectivity gating (see Architecture below).
- `allowed_exceptions` — which of 3 known ERPNext error types (employee not found, inactive employee, duplicate checkin) are swallowed vs. halt the import; see the numbered list in `local_config.py.template`.

**`local_config.py` in this checkout currently contains live ERPNext API credentials and a BioTime password.** Treat it as a secret — never print its contents, never include it in commits/diffs/PRs, and avoid pasting it into logs or output.

## Architecture

### Sync cycle (`main()` in `erpnext_sync.py`)
Gated by `PULL_FREQUENCY` via a `lift_off_timestamp` stored in `status.json` (a flat JSON key-value store managed by the local `SimpleDB` class — not a real database). Each cycle, for every configured BioTime device:
1. Check for a leftover crash-recovery dump file (`get_dump_file_name_and_directory`) and reload undelivered records if present.
2. `pull_process_and_push_data(device, ...)` — pulls attendance, pushes each record to ERPNext, dumps unpushed records to disk on failure (crash safety).
3. After all devices are processed, `update_shift_last_sync_timestamp()` runs if `shift_type_device_mapping` is configured.

The script is meant to be invoked repeatedly (`infinite_loop`, or externally via cron/service); `main()` itself is a no-op no-op-fast-return when called before `PULL_FREQUENCY` has elapsed.

### Multi-terminal connectivity gating
This is the main non-obvious piece of logic (see `IMPLEMENTATION_SUMMARY.md` for the full design rationale). ZKBioTime aggregates multiple physical terminals per device. Naively updating a Shift Type's sync timestamp after any attendance pull risks telling ERPNext's auto-attendance job that punches are complete when one physical terminal is actually offline and hasn't reported yet.

- `get_terminals_from_biotime()` auto-discovers registered terminals per device (or uses `expected_terminals` if explicitly configured) and records them in `status.json`.
- Every attendance record carries a `terminal_alias`; per-terminal `<alias>_last_attendance_timestamp` / `<alias>_last_checked` are tracked in `status.json`.
- `check_terminal_connectivity(device_id)` compares each terminal's last-seen time against `TERMINAL_TIMEOUT` (minutes) to decide active/offline.
- `update_shift_last_sync_timestamp()` only pushes a new sync timestamp to a Shift Type when `require_all_terminals` is set AND all its mapped terminals are currently active — using the **earliest** terminal timestamp (conservative) rather than the latest, so ERPNext never treats attendance as "complete" while a terminal is silently down.

### Crash recovery
If `pull_process_and_push_data` fails partway through pushing records to ERPNext, unpushed records are dumped to a per-device file (see `get_dump_file_name_and_directory`) and retried on the next cycle before fetching new data — this prevents attendance loss on transient ERPNext/network failures.

### Logging
Uses `setup_logger()` (stdlib `logging` + `RotatingFileHandler`) to write separate log streams under `logs/`:
- `logs.log` (general info), `error.log`, `terminal_status.log` (terminal connectivity), `status.json` (cycle/device/terminal timestamps state), and per-device `attendance_success_log_<device_id>.log` / `attendance_failed_log_<device_id>.log`.

### Windows service wrapper
`SMWinservice.py` is a generic base class for turning a Python script into a Windows service (via `pywin32`). `erpnext_sync_win.py` subclasses it to run `erpnext_sync.main()` in a loop every 15s as the `ERPNextBiometricPushService` service — this is the Windows deployment path; on Linux the script is typically just cron'd or run under systemd directly.
