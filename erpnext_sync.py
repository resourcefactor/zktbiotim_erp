
import local_config as config
import requests
import datetime
import json
import os
import sys
import time
import logging
import signal
import threading
from logging.handlers import RotatingFileHandler

# The CLI window used to print every page fetch, every per-terminal check, every raw HTTP
# response, etc - all of that still goes to logs.log/error.log either way, so the console
# only needs a short per-cycle summary by default. Set VERBOSE=True in local_config.py to
# get the old, detailed console output back for troubleshooting.
VERBOSE = getattr(config, 'VERBOSE', False)

def _vprint(*args, **kwargs):
    if VERBOSE:
        print(*args, **kwargs)

def _handle_shutdown(signum, frame):
    print("\n\nShutdown signal received. Exiting gracefully...")
    sys.exit(0)

signal.signal(signal.SIGINT, _handle_shutdown)   # Ctrl+C
signal.signal(signal.SIGTERM, _handle_shutdown)  # kill / systemctl stop

# Simple JSON database class to replace pickledb
class SimpleDB:
    def __init__(self, location, auto_dump=True):
        self.location = location
        self.auto_dump = auto_dump
        self.db = {}
        self._loaded = False
        self.load_data()

    def load_data(self):
        if os.path.exists(self.location):
            try:
                with open(self.location, 'r') as f:
                    self.db = json.load(f)
            except:
                self.db = {}
        else:
            self.db = {}
        self._loaded = True

    def set(self, key, value):
        self.db[key] = value
        if self.auto_dump:
            self.save()
        return True

    def get(self, key):
        # status.json has a single reader/writer (this process), so the in-memory copy
        # never goes stale - reloading on every .get() was a full file read/parse for no
        # benefit (this process is the only thing that ever changes the file).
        if not self._loaded:
            self.load_data()
        return self.db.get(key, None)

    def save(self):
        try:
            with open(self.location, 'w') as f:
                json.dump(self.db, f, indent=2)
            return True
        except:
            return False

EMPLOYEE_NOT_FOUND_ERROR_MESSAGE = "No Employee found for the given employee field value"
EMPLOYEE_INACTIVE_ERROR_MESSAGE = "Transactions cannot be created for an Inactive Employee"
DUPLICATE_EMPLOYEE_CHECKIN_ERROR_MESSAGE = "This employee already has a log with the same timestamp"
allowlisted_errors = [EMPLOYEE_NOT_FOUND_ERROR_MESSAGE, EMPLOYEE_INACTIVE_ERROR_MESSAGE, DUPLICATE_EMPLOYEE_CHECKIN_ERROR_MESSAGE]

# Only "no matching employee" is retried (the mapping may get fixed later). A duplicate
# checkin means the record already exists in ERPNext under that exact timestamp, and an
# inactive employee is a settled business fact - neither should ever be retried.
RETRYABLE_ERROR_MESSAGES = [EMPLOYEE_NOT_FOUND_ERROR_MESSAGE]

# Stop retrying a record once it has been pending for longer than this, to avoid retrying
# forever for an employee mapping that will never be fixed.
PENDING_RETRY_MAX_AGE_DAYS = 7

if hasattr(config,'allowed_exceptions'):
    allowlisted_errors_temp = []
    for error_number in config.allowed_exceptions:
        allowlisted_errors_temp.append(allowlisted_errors[error_number-1])
    allowlisted_errors = allowlisted_errors_temp

device_punch_values_IN = getattr(config, 'device_punch_values_IN', [0,4])
device_punch_values_OUT = getattr(config, 'device_punch_values_OUT', [1,5])
ERPNEXT_VERSION = getattr(config, 'ERPNEXT_VERSION', 14)

# Fetch windows are re-queried using a small overlap before the previous cycle's end time,
# because a record's punch_time (device clock) can be earlier than its upload_time (when
# Biotime actually receives and stores it) - a record uploaded just after a cycle's HTTP
# request was made would otherwise fall permanently between that window and the next one,
# since the next window's start is taken from punch_time, not upload_time.
FETCH_WINDOW_OVERLAP = datetime.timedelta(minutes=getattr(config, 'FETCH_WINDOW_OVERLAP_MINUTES', 2))

# Network calls had no timeout at all, so a server that accepts a TCP connection but never
# sends a response body would hang main() forever - these bound every requests call.
# (connect_timeout, read_timeout) tuples: Biotime is a LAN device API, ERPNext is a public
# HTTPS API with more latency headroom.
BIOTIME_REQUEST_TIMEOUT = getattr(config, 'BIOTIME_REQUEST_TIMEOUT', (5, 30))
ERPNEXT_REQUEST_TIMEOUT = getattr(config, 'ERPNEXT_REQUEST_TIMEOUT', (10, 60))

# The pending-retry queue is dominated by permanently-unmapped employee codes that fail
# identically every attempt - retrying it on the same 5-minute cadence as live punches wastes
# HTTP calls and floods the failed log for no benefit, since only something like an HR mapping
# fix would ever change the outcome. Retried on its own, much slower cadence instead.
PENDING_RETRY_INTERVAL_HOURS = getattr(config, 'PENDING_RETRY_INTERVAL_HOURS', 12)

# Wall-clock cap on a single sync cycle - a main.lock older than this is assumed to belong
# to a crashed/killed process (machine reboot, a prior instance killed without running its
# finally-block cleanup) rather than a cycle still legitimately in progress, and is removed.
MAIN_CYCLE_TIMEOUT_SECONDS = getattr(config, 'MAIN_CYCLE_TIMEOUT_SECONDS', 600)
MAIN_LOCK_STALE_SECONDS = getattr(config, 'MAIN_LOCK_STALE_SECONDS', MAIN_CYCLE_TIMEOUT_SECONDS * 2)

# possible area of further developemt
    # Real-time events - setup getting events pushed from the machine rather then polling.
        #- this is documented as 'Real-time events' in the ZKProtocol manual.

# Notes:
# Status Keys in status.json
#  - lift_off_timestamp
#  - mission_accomplished_timestamp
#  - <device_id>_pull_timestamp
#  - <device_id>_push_timestamp
#  - <shift_type>_sync_timestamp

MAIN_LOCK_FILE = os.path.join(config.LOGS_DIRECTORY, 'main.lock')

def _acquire_main_lock():
    """
    Cross-process mutual exclusion for main(), independent of status.json's own
    lift_off_timestamp check. The lift_off_timestamp check alone isn't safe against
    overlapping invocations (e.g. the script launched more than once manually while
    troubleshooting, or any other accidental concurrent run): multiple processes can each
    read a stale lift_off_timestamp before any of them writes their own update, and all
    proceed - this happened in production (147 overlapping "Cleared for lift off" events
    within ~1.6 seconds), causing duplicate work and log spam. O_CREAT|O_EXCL is atomic at
    the OS level on both Windows and Linux, so only one process can ever hold the lock.
    """
    try:
        if os.path.exists(MAIN_LOCK_FILE):
            lock_age = time.time() - os.path.getmtime(MAIN_LOCK_FILE)
            if lock_age > MAIN_LOCK_STALE_SECONDS:
                error_logger.error(f"Removing stale main.lock ({lock_age:.0f}s old) - assuming the process that created it died without cleaning up")
                os.remove(MAIN_LOCK_FILE)
        fd = os.open(MAIN_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False

def _release_main_lock():
    try:
        os.remove(MAIN_LOCK_FILE)
    except FileNotFoundError:
        pass

def main():
    """Takes care of checking if it is time to pull data based on config,
    then calling the relevent functions to pull data and push to EPRNext.

    """
    if not _acquire_main_lock():
        # Another invocation is already running this exact cycle - skip entirely rather
        # than risk duplicate pushes/retries. Not logged as an error: this is the lock
        # doing its job, not a failure.
        print("⏳ Another sync cycle is already running - skipping this invocation")
        return
    try:
        last_lift_off_timestamp = _safe_convert_date(status.get('lift_off_timestamp'), "%Y-%m-%d %H:%M:%S.%f")
        if (last_lift_off_timestamp and last_lift_off_timestamp < datetime.datetime.now() - datetime.timedelta(minutes=config.PULL_FREQUENCY)) or not last_lift_off_timestamp:
            cycle_start_time = datetime.datetime.now()
            status.set('lift_off_timestamp', str(cycle_start_time))
            status.save()

            print(f"\n\n{'#'*60}")
            print(f"#  ATTENDANCE SYNC CYCLE STARTED")
            print(f"#  Time: {cycle_start_time.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"{'#'*60}\n")
            info_logger.info("Cleared for lift off!")

            devices_processed = 0
            devices_failed = 0
            total_records_processed = 0
            all_terminal_last_timestamps = {}

            for device in config.biotime_url:
                device_attendance_logs = None
                _vprint(f"\n{'='*60}")
                _vprint(f"Processing Device: {device['device_id']}")
                _vprint(f"{'='*60}")
                info_logger.info("Processing Device: "+ device['device_id'])

                dump_file = get_dump_file_name_and_directory(device['device_id'], 'biotime')
                if os.path.exists(dump_file):
                    print(f"⚠ Warning: Dump file found - recovering from previous crash")
                    info_logger.error('Device Attendance Dump Found in Log Directory. This can mean the program crashed unexpectedly. Retrying with dumped data.')
                    with open(dump_file, 'r') as f:
                        file_contents = f.read()
                        if file_contents:
                            device_attendance_logs = list(map(lambda x: _apply_function_to_key(x, 'timestamp', datetime.datetime.fromtimestamp), json.loads(file_contents)))
                try:
                    result = pull_process_and_push_data(device, device_attendance_logs)
                    status.set(f'{device["device_id"]}_push_timestamp', str(datetime.datetime.now()))
                    status.save()
                    if os.path.exists(dump_file):
                        os.remove(dump_file)
                        _vprint(f"✓ Removed dump file")
                    total_records_processed += result['records_processed']
                    all_terminal_last_timestamps.update(result['terminal_last_timestamps'])
                    info_logger.info("Successfully processed Device: "+ device['device_id'])
                    devices_processed += 1
                except Exception as e:
                    print(f"✗ Error processing device: {device['device_id']}")
                    print(f"  Error: {str(e)}")
                    error_logger.exception('exception when calling pull_process_and_push_data function for device'+json.dumps(device, default=str))
                    devices_failed += 1

            # Update shift types
            if hasattr(config,'shift_type_device_mapping'):
                update_shift_last_sync_timestamp(config.shift_type_device_mapping)

            cycle_end_time = datetime.datetime.now()
            cycle_duration = (cycle_end_time - cycle_start_time).total_seconds()

            status.set('mission_accomplished_timestamp', str(cycle_end_time))
            status.save()

            # Cycle Summary - the concise console output: total records processed, each
            # terminal's latest record time, and when the next cycle will run. Full
            # per-record/per-page detail still goes to logs.log/error.log either way.
            next_cycle_at = (cycle_end_time + datetime.timedelta(minutes=config.PULL_FREQUENCY)).strftime('%Y-%m-%d %H:%M:%S')
            print(f"\n[{cycle_end_time.strftime('%Y-%m-%d %H:%M:%S')}] Sync cycle complete - {total_records_processed} record(s) processed ({devices_processed} device(s) ok, {devices_failed} failed) in {cycle_duration:.1f}s")
            if all_terminal_last_timestamps:
                for terminal_alias in sorted(all_terminal_last_timestamps):
                    print(f"    {terminal_alias}: latest record {all_terminal_last_timestamps[terminal_alias]}")
            print(f"    Next cycle: {next_cycle_at}")
            info_logger.info(f"Mission Accomplished! Duration: {cycle_duration:.2f}s, Processed: {devices_processed}, Failed: {devices_failed}, Records: {total_records_processed}")
        else:
            # Still waiting for next cycle - not worth printing every 15s poll.
            pass
    except:
        error_logger.exception('exception has occurred in the main function...')
        print(f"\n✗ CRITICAL ERROR in main function - check logs/error.log")
    finally:
        _release_main_lock()


def push_attendance_log(log, device, attendance_success_logger, attendance_failed_logger, terminal_last_timestamps, log_failure=True):
    """
    Pushes a single attendance record to ERPNext and logs the outcome.
    Returns one of:
      'settled'   - pushed successfully, or failed with a terminal (non-retryable)
                    allowlisted error: a duplicate checkin (already exists in ERPNext under
                    this exact timestamp - retrying would never help) or an inactive
                    employee (a settled business fact, not something that will change).
      'retryable' - failed because no matching employee was found in ERPNext yet (the
                    mapping may get configured later); queued for retry, subject to
                    PENDING_RETRY_MAX_AGE_DAYS.
    Raises on a non-allowlisted failure, matching the existing halt-on-unknown-error behavior.

    log_failure=False suppresses the attendance_failed_logger write on failure. Used when
    retrying an already-pending record: it was already logged once when first seen, and the
    pending-retry queue file is itself the durable record of what's stuck and why, so
    re-logging an identical failure on every retry attempt just adds noise.
    """
    punch_direction = device.get('punch_direction', 'AUTO')
    if punch_direction == 'AUTO':
        if log['punch'] in device_punch_values_OUT:
            punch_direction = 'OUT'
        elif log['punch'] in device_punch_values_IN:
            punch_direction = 'IN'
        else:
            punch_direction = None

    terminal_alias = log.get('terminal_alias') or 'Unknown'

    erpnext_status_code, erpnext_message = send_to_erpnext(log['user_id'], log['timestamp'], terminal_alias, punch_direction)
    if erpnext_status_code == 200:
        if terminal_alias not in terminal_last_timestamps or log['timestamp'] > terminal_last_timestamps[terminal_alias]:
            terminal_last_timestamps[terminal_alias] = log['timestamp']

        attendance_success_logger.info("\t".join([
            erpnext_message, str(log['uid']),
            str(log['user_id']), str(log['timestamp'].timestamp()),
            str(log['punch']), str(log['status']),
            terminal_alias,
            # Every field already appears individually above except first_failed_at (only
            # set on records that came through the retry path) - a full json.dumps(log) of
            # already-logged fields was pure bulk on every line.
            str(log.get('first_failed_at', ''))
        ]))
        return 'settled'
    else:
        if log_failure:
            attendance_failed_logger.error("\t".join([
                str(erpnext_status_code), str(log['uid']),
                str(log['user_id']), str(log['timestamp'].timestamp()),
                str(log['punch']), str(log['status']),
                terminal_alias,
                str(log.get('first_failed_at', ''))
            ]))
        if DUPLICATE_EMPLOYEE_CHECKIN_ERROR_MESSAGE in erpnext_message:
            # Already exists in ERPNext under this exact timestamp - nothing to retry.
            return 'settled'
        if any(error in erpnext_message for error in RETRYABLE_ERROR_MESSAGES):
            return 'retryable'
        if not any(error in erpnext_message for error in allowlisted_errors):
            raise Exception('API Call to ERPNext Failed.')
        # An allowlisted error that is neither the duplicate case nor a known retryable
        # case (e.g. allowed_exceptions configured with only a subset) - treat as settled
        # rather than retried forever.
        return 'settled'

def pull_process_and_push_data(device, device_attendance_logs=None):
    """
    Handles pushing attendance logs to ERPNext from either BioTime or ZKTeco.
    """
    attendance_success_log_file = '_'.join(["attendance_success_log", device['device_id']])
    attendance_failed_log_file = '_'.join(["attendance_failed_log", device['device_id']])
    # attendance_success_logger keeps the default retention - get_last_line_from_file()
    # depends on its live file for the resume cursor, and it's not the volume driver.
    attendance_success_logger = setup_logger(attendance_success_log_file, '/'.join([config.LOGS_DIRECTORY, attendance_success_log_file])+'.log')
    # attendance_failed_logger is the single biggest contributor to disk growth (repeated
    # "No Employee found" failures) - smaller rotation ceiling than the default.
    attendance_failed_logger = setup_logger(attendance_failed_log_file, '/'.join([config.LOGS_DIRECTORY, attendance_failed_log_file])+'.log', max_bytes=5000000, backup_count=5)

    terminal_last_timestamps = {}

    # Retry any records that previously failed because no matching employee was found in
    # ERPNext yet. Once the mapping is fixed, this picks them back up automatically. A
    # record is dropped for good after PENDING_RETRY_MAX_AGE_DAYS - past that point the
    # mapping is assumed to never be fixed, and retrying forever would just keep hammering
    # ERPNext with the same failure.
    #
    # This only runs on its own PENDING_RETRY_INTERVAL_HOURS cadence rather than every
    # cycle: the queue is dominated by permanently-unmapped employee codes that fail
    # identically every attempt, so retrying on the same 5-minute cadence as live punches
    # wasted HTTP calls and flooded the failed log for no benefit - nothing about these
    # records changes minute-to-minute, only an HR mapping fix ever would. New punches
    # below are unaffected and still sync every cycle as before.
    last_retry_attempt = _safe_convert_date(status.get(f"{device['device_id']}_last_retry_attempt"), "%Y-%m-%d %H:%M:%S.%f")
    should_attempt_retries = (
        not last_retry_attempt
        or datetime.datetime.now() - last_retry_attempt >= datetime.timedelta(hours=PENDING_RETRY_INTERVAL_HOURS)
    )
    if should_attempt_retries:
        pending_records = load_pending_retry_records(device['device_id'])
        if pending_records:
            _vprint(f"\n{'='*60}")
            _vprint(f"Retrying {len(pending_records)} previously-failed record(s) for device: {device['device_id']}")
            _vprint(f"{'='*60}")
            still_pending = []
            retried_ok = 0
            expired = 0
            now = datetime.datetime.now()
            for log in pending_records:
                first_failed_at = log.get('first_failed_at') or now
                # log_failure=False: this record was already logged once when first seen
                # (see the new-records path below); re-logging an identical failure on
                # every retry attempt just adds noise - the pending queue file is the
                # durable record of what's stuck.
                outcome = push_attendance_log(log, device, attendance_success_logger, attendance_failed_logger, terminal_last_timestamps, log_failure=False)
                if outcome == 'settled':
                    retried_ok += 1
                elif now - first_failed_at > datetime.timedelta(days=PENDING_RETRY_MAX_AGE_DAYS):
                    expired += 1
                    info_logger.warning(
                        f"Dropping record after {PENDING_RETRY_MAX_AGE_DAYS} days of failed retries "
                        f"(uid={log.get('uid')}, user_id={log.get('user_id')}, timestamp={log.get('timestamp')})"
                    )
                else:
                    log['first_failed_at'] = first_failed_at
                    still_pending.append(log)
            save_pending_retry_records(device['device_id'], still_pending)
            _vprint(f"✓ {retried_ok} retried record(s) succeeded, {len(still_pending)} still pending, {expired} expired after {PENDING_RETRY_MAX_AGE_DAYS} days")
        status.set(f"{device['device_id']}_last_retry_attempt", str(datetime.datetime.now()))
        status.save()

    # Determine start time
    import_start_date = _safe_convert_date(config.IMPORT_START_DATE, "%Y%m%d")
    last_sync = _safe_convert_date(status.get(f"{device['device_id']}_pull_timestamp"), "%Y-%m-%d %H:%M:%S.%f")
    if not last_sync:
        last_sync = import_start_date or (datetime.datetime.now() - datetime.timedelta(days=1))
    
    if not device_attendance_logs:
        token = get_biotime_token(device['BASE_URL'], device['USERNAME'], device['PASSWORD'])
        if not token:
            error_logger.error(f"Could not fetch BioTime token for device: {device['device_id']}")
            return {'records_processed': 0, 'terminal_last_timestamps': {}}

        # Fetch registered terminals from Biotime
        _vprint(f"\n{'='*60}")
        _vprint(f"Fetching terminals for device: {device['device_id']}")
        _vprint(f"{'='*60}")
        terminals = get_terminals_from_biotime(device['BASE_URL'], token)

        # Store terminals in status for later validation
        if terminals:
            status.set(f"{device['device_id']}_terminals", json.dumps(terminals))
            status.save()

        now = datetime.datetime.now()
        # Use last pull timestamp as start, falling back to IMPORT_START_DATE on first run.
        # To trigger a backfill: set IMPORT_START_DATE and delete {device_id}_pull_timestamp from status.json
        start_time = last_sync

        _vprint(f"\n{'='*60}")
        _vprint(f"Fetching attendance from: {device['device_id']}")
        _vprint(f"Time range: {start_time} to {now}")
        _vprint(f"{'='*60}")

        device_attendance_logs = get_attendance_from_biotime(
            base_url=device['BASE_URL'],
            token=token,
            start_time=start_time,
            end_time=now,
            device_id=device['device_id']
        )
        # Only advance the pull watermark once the fetch has completed in full -
        # a partial/failed fetch (get_attendance_from_biotime now raises instead of
        # silently truncating) must not move the window past unfetched records.
        # The watermark is set slightly behind `now` (FETCH_WINDOW_OVERLAP) so the next
        # cycle re-queries that overlap: a record's punch_time can be earlier than its
        # upload_time, so a record uploaded just after this request was made would
        # otherwise be excluded by both this window (not yet uploaded) and the next one
        # (its punch_time is before the next window's start). The resume-by-(user_id,
        # timestamp) logic below already de-dupes records re-fetched in this overlap.
        next_watermark = now - FETCH_WINDOW_OVERLAP
        status.set(f"{device['device_id']}_pull_timestamp", str(next_watermark))
        status.save()
        if not device_attendance_logs:
            _vprint(f"✗ No attendance logs found")
            return {'records_processed': 0, 'terminal_last_timestamps': {}}

    # Find the last successful push
    index_of_last = -1
    last_line = get_last_line_from_file('/'.join([config.LOGS_DIRECTORY, attendance_success_log_file])+'.log')
    if last_line:
        try:
            parts = last_line.split("\t")
            _vprint(f"Debug: Last log line has {len(parts)} parts")
            if len(parts) >= 6:
                last_user_id, last_timestamp = parts[4:6]
                last_timestamp = datetime.datetime.fromtimestamp(float(last_timestamp))
                _vprint(f"Debug: Last processed - User: {last_user_id}, Time: {last_timestamp}")
            else:
                print(f"⚠ Warning: Log line format unexpected")
                last_user_id, last_timestamp = None, None
        except Exception as e:
            print(f"⚠ Warning: Error parsing last line: {e}")
            last_user_id, last_timestamp = None, None
        if last_timestamp and import_start_date and last_timestamp < import_start_date:
            last_timestamp = import_start_date
            last_user_id = None
    else:
        last_user_id, last_timestamp = None, import_start_date
        _vprint(f"Debug: No previous attendance log found, starting from: {import_start_date}")

    index_of_last_by_timestamp = -1
    for i, log in enumerate(device_attendance_logs):
        if last_user_id and last_timestamp:
            if last_user_id == str(log['user_id']) and last_timestamp == log['timestamp']:
                index_of_last = i
                break
            if index_of_last_by_timestamp == -1 and log['timestamp'] >= last_timestamp:
                index_of_last_by_timestamp = i
        elif last_timestamp:
            if log['timestamp'] >= last_timestamp:
                index_of_last = i
                break

    if index_of_last == -1 and last_user_id and last_timestamp and index_of_last_by_timestamp != -1:
        # The last-synced record has aged out of the current pull window (e.g. after
        # a long gap). Fall back to a timestamp-only resume point instead of replaying
        # the entire batch, which would otherwise flood ERPNext with duplicate-checkin errors.
        print(f"⚠ Warning: Last synced record not found in current window, falling back to timestamp match")
        index_of_last = index_of_last_by_timestamp - 1

    records_to_process = device_attendance_logs[index_of_last+1:]
    _vprint(f"\n{'='*60}")
    _vprint(f"Processing {len(records_to_process)} new attendance records")
    _vprint(f"{'='*60}")

    # The resume cursor above only advances based on the success log, so a record that
    # fails here with a retryable error (unmapped employee) will be re-fetched by every
    # future cycle's overlapping window and land here again. Without de-duplication that
    # would re-add it to the pending queue with a fresh first_failed_at every cycle,
    # resetting its retry clock forever and defeating PENDING_RETRY_MAX_AGE_DAYS.
    existing_pending = load_pending_retry_records(device['device_id'])
    existing_by_uid = {r['uid']: r for r in existing_pending}

    newly_failed_records = []
    records_processed = 0
    for log in records_to_process:
        if log['uid'] in existing_by_uid:
            continue
        outcome = push_attendance_log(log, device, attendance_success_logger, attendance_failed_logger, terminal_last_timestamps)
        records_processed += 1
        if outcome == 'retryable':
            log['first_failed_at'] = datetime.datetime.now()
            newly_failed_records.append(log)
            existing_by_uid[log['uid']] = log

    if newly_failed_records:
        save_pending_retry_records(device['device_id'], existing_pending + newly_failed_records)
        print(f"⚠ {len(newly_failed_records)} record(s) queued for retry (see {get_pending_retry_file_name(device['device_id'])})")

    # Update terminal last timestamps in status
    if terminal_last_timestamps:
        for terminal_alias, last_timestamp in terminal_last_timestamps.items():
            status.set(f"{terminal_alias}_last_attendance_timestamp", str(last_timestamp))
            status.set(f"{terminal_alias}_last_checked", str(datetime.datetime.now()))
            _vprint(f"✓ Terminal '{terminal_alias}' last attendance: {last_timestamp}")
        status.save()
        _vprint(f"✓ Successfully processed attendance for {len(terminal_last_timestamps)} terminals")
    else:
        _vprint(f"⚠ Warning: No terminal timestamps collected")

    return {'records_processed': records_processed, 'terminal_last_timestamps': terminal_last_timestamps}

def get_biotime_token(base_url, username, password):
    url = f"{base_url}/api-token-auth/"
    headers = {"Content-Type": "application/json"}
    data = {"username": username, "password": password}
    try:
        res = requests.post(url, headers=headers, data=json.dumps(data), timeout=BIOTIME_REQUEST_TIMEOUT)
        _vprint("Token Response:", res.text)
        res.raise_for_status()
        return res.json().get("token")
    except Exception as e:
        error_logger.exception(f"Error getting BioTime token: {e}")
        return None

def get_terminals_from_biotime(base_url, token):
    """
    Fetches list of all registered terminals/devices from Biotime.
    Returns list of terminal aliases.
    """
    url = f"{base_url}/iclock/api/terminals/"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Token {token}"
    }
    try:
        res = requests.get(url, headers=headers, timeout=BIOTIME_REQUEST_TIMEOUT)
        res.raise_for_status()
        terminals_data = res.json().get("data", [])

        # Extract terminal aliases
        terminal_aliases = [t.get("alias", "") for t in terminals_data if t.get("alias")]

        _vprint(f"✓ Found {len(terminal_aliases)} registered terminals")
        for alias in terminal_aliases:
            _vprint(f"  └─ {alias}")

        info_logger.info(f"Fetched {len(terminal_aliases)} terminals: {', '.join(terminal_aliases)}")
        return terminal_aliases
    except Exception as e:
        error_logger.exception(f"Error fetching terminals from Biotime: {e}")
        print(f"✗ Error fetching terminals: {e}")
        return []

def get_attendance_from_biotime(base_url, token, start_time, end_time, device_id=None):
    url = f"{base_url}/iclock/api/transactions/"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Token {token}"
    }
    _vprint(headers, "===================================")

    all_attendance_logs = []
    page = 1
    page_size = 100

    while True:
        params = {
            "start_time": start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": end_time.strftime("%Y-%m-%d %H:%M:%S"),
            "page": page,
            "page_size": page_size
        }

        try:
            _vprint(f"Fetching page {page}...")
            _vprint("Url", url)
            _vprint("Params", params)
            res = requests.get(url, headers=headers, params=params, timeout=BIOTIME_REQUEST_TIMEOUT)
            _vprint(res.url,"reponse")
            _vprint("Status", res.status_code)
            res.raise_for_status()

            response_data = res.json()
            records = response_data.get("data", [])

            if not records:
                _vprint(f"  No more records on page {page}")
                break

            _vprint(f"  ✓ Found {len(records)} records on page {page}")

            # Convert records to attendance logs
            page_logs = [{
                "user_id": r["emp_code"],
                "uid": r["id"],
                "timestamp": datetime.datetime.strptime(r["punch_time"], "%Y-%m-%d %H:%M:%S"),
                "punch": int(r.get("punch_state", 0)),
                "status": 1,
                "terminal_alias": r.get("terminal_alias", "Unknown")
            } for r in records]

            all_attendance_logs.extend(page_logs)

            # Check if there are more pages
            total_count = response_data.get("count", 0)
            fetched_count = page * page_size

            if len(records) < page_size or fetched_count >= total_count:
                _vprint(f"  All pages fetched (total records: {len(all_attendance_logs)})")
                break

            page += 1

        except Exception as e:
            error_logger.exception(f"Error fetching attendance from biotime on page {page}: {e}")
            raise

    if all_attendance_logs:
        _vprint(f"\n✓ Total: {len(all_attendance_logs)} attendance records from Biotime (across {page} page(s))")

        # Log terminal distribution
        terminal_counts = {}
        for log in all_attendance_logs:
            terminal = log.get("terminal_alias", "Unknown")
            terminal_counts[terminal] = terminal_counts.get(terminal, 0) + 1

        _vprint(f"  Attendance by Terminal:")
        for terminal, count in terminal_counts.items():
            _vprint(f"    └─ {terminal}: {count} records")

        # Save dump file
        dump_file_name = get_dump_file_name_and_directory(device_id, "biotime")
        _vprint(f"Writing dump to: {dump_file_name}")

        try:
            with open(dump_file_name, 'w+') as f:
                f.write(json.dumps(all_attendance_logs, default=datetime.datetime.timestamp))
            _vprint("Dump file written successfully")
        except Exception as e:
            error_logger.exception(f"Error writing dump file: {e}")

    return all_attendance_logs

def send_to_erpnext(employee_field_value, timestamp, device_id=None, log_type=None):
    """
    Example: send_to_erpnext('12349',datetime.datetime.now(),'HO1','IN')
    """
    endpoint_app = "hrms" if ERPNEXT_VERSION > 13 else "erpnext"
    url = f"{config.ERPNEXT_URL}/api/method/{endpoint_app}.hr.doctype.employee_checkin.employee_checkin.add_log_based_on_employee_field"
    headers = {
        'Authorization': "token "+ config.ERPNEXT_API_KEY + ":" + config.ERPNEXT_API_SECRET,
        'Accept': 'application/json'
    }
    data = {
        'employee_field_value' : employee_field_value,
        'timestamp' : timestamp.__str__(),
        'device_id' : device_id,
        'log_type' : log_type
    }
    try:
        response = requests.request("POST", url, headers=headers, json=data, timeout=ERPNEXT_REQUEST_TIMEOUT)
    except requests.exceptions.RequestException as e:
        # A network-level failure (timeout, connection refused, etc) isn't an ERPNext
        # validation outcome - it means we don't actually know whether this record was
        # accepted, so it must not be logged as a permanent failure or queued for retry
        # via the normal allowlist path. Let it propagate so main()'s per-device handler
        # aborts this device's cycle and the whole batch is retried next cycle, same as
        # before a timeout was ever possible to hit instead of hanging indefinitely.
        error_logger.error('\t'.join(['Network error during ERPNext API Call.', str(employee_field_value), str(timestamp.timestamp()), str(device_id), str(log_type), str(e)]))
        raise
    if response.status_code == 200:
        return 200, json.loads(response._content)['message']['name']
    else:
        if not _is_json_response(response):
            # A 5xx from nginx/the app server during a deploy or restart (e.g. the "Sorry!
            # We will be back soon." maintenance page) isn't an ERPNext validation outcome -
            # same reasoning as the network-exception branch above: we don't know whether
            # this record was accepted, so it must not be classified as a permanent failure
            # or queued for retry via the allowlist path. Raise so the per-device handler in
            # main() retries the whole batch next cycle instead of halting on "unknown error".
            error_logger.error('\t'.join(['Non-JSON/gateway error during ERPNext API Call (likely a transient restart/maintenance window).', str(employee_field_value), str(timestamp.timestamp()), str(device_id), str(log_type), str(response.status_code)]))
            raise requests.exceptions.RequestException(f"Non-JSON response, HTTP {response.status_code}")
        error_str = _safe_get_error_str(response)
        error_logger.error('\t'.join(['Error during ERPNext API Call.', str(employee_field_value), str(timestamp.timestamp()), str(device_id), str(log_type), error_str]))
        return response.status_code, error_str

def check_terminal_connectivity(device_id):
    """
    Checks if all expected terminals for a device have reported within the timeout period.
    Returns (all_terminals_active, terminal_info)
    """
    TERMINAL_TIMEOUT = getattr(config, 'TERMINAL_TIMEOUT', 120)  # minutes
    timeout_delta = datetime.timedelta(minutes=TERMINAL_TIMEOUT)

    # Get expected terminals from config or from last fetch
    expected_terminals_dict = getattr(config, 'expected_terminals', {})
    expected_terminals = expected_terminals_dict.get(device_id, [])

    # If not in config, get from status (auto-discovered terminals)
    if not expected_terminals:
        terminals_json = status.get(f"{device_id}_terminals")
        if terminals_json:
            try:
                expected_terminals = json.loads(terminals_json)
            except:
                expected_terminals = []

    if not expected_terminals:
        # No terminals configured or discovered - warn but allow processing
        print(f"⚠ Warning: No terminals configured for device '{device_id}'")
        info_logger.warning(f"No terminals configured for device '{device_id}'")
        return True, {}

    _vprint(f"\n{'='*60}")
    _vprint(f"Terminal Connectivity Check for device: {device_id}")
    _vprint(f"{'='*60}")
    _vprint(f"Expected terminals: {len(expected_terminals)}")

    terminal_status = {}
    all_active = True
    now = datetime.datetime.now()

    for terminal_alias in expected_terminals:
        last_checked_str = status.get(f"{terminal_alias}_last_checked")
        last_attendance_str = status.get(f"{terminal_alias}_last_attendance_timestamp")

        if last_checked_str:
            last_checked = _safe_convert_date(last_checked_str, "%Y-%m-%d %H:%M:%S.%f")
            time_since_last = now - last_checked if last_checked else None

            if time_since_last and time_since_last > timeout_delta:
                # Terminal hasn't reported within timeout
                all_active = False
                terminal_status[terminal_alias] = {
                    'active': False,
                    'last_checked': last_checked,
                    'time_since': time_since_last,
                    'last_attendance': last_attendance_str
                }
                hours_ago = int(time_since_last.total_seconds() / 3600)
                print(f"✗ {terminal_alias}: OFFLINE (last seen {hours_ago}h ago)")
                error_logger.warning(f"Terminal '{terminal_alias}' not seen for {hours_ago} hours - may be offline")
            else:
                # Terminal is active
                terminal_status[terminal_alias] = {
                    'active': True,
                    'last_checked': last_checked,
                    'time_since': time_since_last,
                    'last_attendance': last_attendance_str
                }
                minutes_ago = int(time_since_last.total_seconds() / 60) if time_since_last else 0
                _vprint(f"✓ {terminal_alias}: ACTIVE (last seen {minutes_ago}m ago)")
        else:
            # Never seen this terminal
            all_active = False
            terminal_status[terminal_alias] = {
                'active': False,
                'last_checked': None,
                'time_since': None,
                'last_attendance': None
            }
            print(f"✗ {terminal_alias}: NEVER SEEN")
            error_logger.warning(f"Terminal '{terminal_alias}' has never reported attendance")

    _vprint(f"{'='*60}")
    if all_active:
        _vprint(f"✓ All terminals ACTIVE - Safe to update shift type")
        terminal_logger.info(f"Device '{device_id}': All {len(expected_terminals)} terminals active")
    else:
        inactive_count = sum(1 for t in terminal_status.values() if not t['active'])
        print(f"✗ {inactive_count} terminal(s) OFFLINE for device '{device_id}' - skipping shift type update")
        inactive_terminals = [alias for alias, info in terminal_status.items() if not info['active']]
        terminal_logger.warning(f"Device '{device_id}': {inactive_count} terminals offline: {', '.join(inactive_terminals)}")
    _vprint(f"{'='*60}\n")

    # Log detailed status for each terminal
    for terminal_alias, info in terminal_status.items():
        status_str = "ACTIVE" if info['active'] else "OFFLINE"
        last_checked = info['last_checked'].strftime('%Y-%m-%d %H:%M:%S') if info['last_checked'] else "Never"
        terminal_logger.info(f"Terminal '{terminal_alias}': {status_str}, Last checked: {last_checked}")

    return all_active, terminal_status

def update_shift_last_sync_timestamp(shift_type_device_mapping):
    """
    ### Enhanced algo for updating the sync_current_timestamp with terminal validation
    - Get list of devices to check
    - For each device, verify ALL terminals have reported (if require_all_terminals is True)
    - Check if all devices have non 'None' push_timestamp
    - Use the earliest terminal attendance timestamp across all devices
    - Update shift if this timestamp is greater than current sync_timestamp

    """
    _vprint(f"\n{'='*60}")
    _vprint(f"SHIFT TYPE UPDATE VALIDATION")
    _vprint(f"{'='*60}")

    for shift_type_device_map in shift_type_device_mapping:
        require_all_terminals = shift_type_device_map.get('require_all_terminals', True)

        _vprint(f"\nChecking shift type(s): {shift_type_device_map['shift_type_name']}")
        _vprint(f"Related devices: {shift_type_device_map['related_device_id']}")
        _vprint(f"Require all terminals: {require_all_terminals}")

        all_devices_pushed = True
        all_terminals_active = True
        terminal_timestamp_array = []

        # Check each device
        for device_id in shift_type_device_map['related_device_id']:
            # Check if device has pushed data
            if not status.get(f'{device_id}_push_timestamp'):
                all_devices_pushed = False
                _vprint(f"✗ Device '{device_id}' has not pushed data yet")
                break

            # Check terminal connectivity if required
            if require_all_terminals:
                terminals_active, terminal_info = check_terminal_connectivity(device_id)
                if not terminals_active:
                    all_terminals_active = False
                    _vprint(f"✗ Not all terminals active for device '{device_id}'")
                    break

            # Get terminal list: prefer config's expected_terminals, fall back to auto-discovered
            expected_terminals_dict = getattr(config, 'expected_terminals', {})
            terminals = expected_terminals_dict.get(device_id, [])
            if not terminals:
                terminals_json = status.get(f"{device_id}_terminals")
                if terminals_json:
                    try:
                        terminals = json.loads(terminals_json)
                    except:
                        terminals = []

            for terminal_alias in terminals:
                last_attendance_str = status.get(f"{terminal_alias}_last_attendance_timestamp")
                if last_attendance_str:
                    last_attendance = _safe_convert_date(last_attendance_str, "%Y-%m-%d %H:%M:%S.%f")
                    if last_attendance:
                        terminal_timestamp_array.append(last_attendance)

            # Note: No fallback to pull_timestamp - it's a system clock time, not an
            # attendance time, and using it would set sync_timestamp ahead of real data.

        # Process shift type update if all conditions met
        if all_devices_pushed and all_terminals_active and terminal_timestamp_array:
            # Use the EARLIEST terminal timestamp to be conservative
            min_terminal_timestamp = min(terminal_timestamp_array)
            _vprint(f"✓ All checks passed")
            _vprint(f"  Earliest terminal timestamp: {min_terminal_timestamp}")

            if isinstance(shift_type_device_map['shift_type_name'], str):  # backward compatibility
                shift_type_device_map['shift_type_name'] = [shift_type_device_map['shift_type_name']]

            for shift in shift_type_device_map['shift_type_name']:
                try:
                    sync_current_timestamp = _safe_convert_date(status.get(f'{shift}_sync_timestamp'), "%Y-%m-%d %H:%M:%S.%f")
                    _vprint(f"  Current sync timestamp for '{shift}': {sync_current_timestamp}")

                    if (sync_current_timestamp and min_terminal_timestamp > sync_current_timestamp) or (min_terminal_timestamp and not sync_current_timestamp):
                        _vprint(f"  → Updating shift type '{shift}' with timestamp: {min_terminal_timestamp}")
                        response_code = send_shift_sync_to_erpnext(shift, min_terminal_timestamp)
                        if response_code == 200:
                            status.set(f'{shift}_sync_timestamp', str(min_terminal_timestamp))
                            status.save()
                            _vprint(f"  ✓ Successfully updated shift type '{shift}'")
                        else:
                            print(f"  ✗ Failed to update shift type '{shift}' (HTTP {response_code})")
                    else:
                        _vprint(f"  ⊘ No update needed for '{shift}' (timestamp not newer)")
                except:
                    error_logger.exception('Exception in update_shift_last_sync_timestamp, for shift:'+shift)
                    print(f"  ✗ Exception updating shift '{shift}'")
        else:
            if not all_devices_pushed:
                _vprint(f"✗ Skipping shift type update: Not all devices have pushed data")
            elif not all_terminals_active:
                _vprint(f"✗ Skipping shift type update: Not all terminals are active")
            elif not terminal_timestamp_array:
                _vprint(f"✗ Skipping shift type update: No terminal timestamps available")

    _vprint(f"{'='*60}\n")

def send_shift_sync_to_erpnext(shift_type_name, sync_timestamp):
    url = config.ERPNEXT_URL + "/api/resource/Shift Type/" + shift_type_name
    headers = {
        'Authorization': "token "+ config.ERPNEXT_API_KEY + ":" + config.ERPNEXT_API_SECRET,
        'Accept': 'application/json'
    }
    data = {
        "last_sync_of_checkin" : str(sync_timestamp)
    }
    try:
        response = requests.request("PUT", url, headers=headers, data=json.dumps(data), timeout=ERPNEXT_REQUEST_TIMEOUT)
        if response.status_code == 200:
            info_logger.info("\t".join(['Shift Type last_sync_of_checkin Updated', str(shift_type_name), str(sync_timestamp.timestamp())]))
        else:
            error_str = _safe_get_error_str(response)
            error_logger.error('\t'.join(['Error during ERPNext Shift Type API Call.', str(shift_type_name), str(sync_timestamp.timestamp()), error_str]))
        return response.status_code
    except:
        error_logger.exception("\t".join(['exception when updating last_sync_of_checkin in Shift Type', str(shift_type_name), str(sync_timestamp.timestamp())]))

def get_last_line_from_file(file):
    # concerns to address(may be much later):
        # how will last line lookup work with log rotation when a new file is created?
            #- will that new file be empty at any time? or will it have a partial line from the previous file?
    line = None
    try:
        if not os.path.exists(file):
            return None
        if os.stat(file).st_size == 0:
            return None
        if os.stat(file).st_size < 5000:
            # quick hack to handle files with one line
            with open(file, 'r') as f:
                for line in f:
                    pass
        else:
            # optimized for large log files
            with open(file, 'rb') as f:
                f.seek(-2, os.SEEK_END)
                while f.read(1) != b'\n':
                    f.seek(-2, os.SEEK_CUR)
                line = f.readline().decode()
    except:
        return None
    return line


def setup_logger(name, log_file, level=logging.INFO, formatter=None, max_bytes=10000000, backup_count=15):

    if not formatter:
        formatter = logging.Formatter('%(asctime)s\t%(levelname)s\t%(message)s')

    handler = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count)
    handler.setFormatter(formatter)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    if not logger.hasHandlers():
        logger.addHandler(handler)

    return logger

def get_dump_file_name_and_directory(device_id, device_ip):
    return config.LOGS_DIRECTORY + '/' + device_id + "_" + device_ip.replace('.', '_') + '_last_fetch_dump.json'

def get_pending_retry_file_name(device_id):
    return config.LOGS_DIRECTORY + '/' + device_id + '_pending_retry.json'

def load_pending_retry_records(device_id):
    pending_file = get_pending_retry_file_name(device_id)
    if not os.path.exists(pending_file):
        return []
    try:
        with open(pending_file, 'r') as f:
            records = json.load(f)
        records = [_apply_function_to_key(r, 'timestamp', datetime.datetime.fromtimestamp) for r in records]
        for r in records:
            if r.get('first_failed_at') is not None:
                r['first_failed_at'] = datetime.datetime.fromtimestamp(r['first_failed_at'])
        return records
    except Exception as e:
        error_logger.exception(f"Error loading pending retry file {pending_file}: {e}")
        return []

def save_pending_retry_records(device_id, records):
    pending_file = get_pending_retry_file_name(device_id)
    try:
        if not records:
            if os.path.exists(pending_file):
                os.remove(pending_file)
            return
        with open(pending_file, 'w') as f:
            f.write(json.dumps(records, default=datetime.datetime.timestamp))
    except Exception as e:
        error_logger.exception(f"Error saving pending retry file {pending_file}: {e}")

def _apply_function_to_key(obj, key, fn):
    obj[key] = fn(obj[key])
    return obj

def _safe_convert_date(datestring, pattern):
    """
    Safely convert date string to datetime, trying multiple formats.
    """
    if not datestring:
        return None
    try:
        # Try with microseconds first
        return datetime.datetime.strptime(datestring, pattern)
    except:
        # If it fails, try without microseconds
        try:
            pattern_no_micro = pattern.replace('.%f', '')
            return datetime.datetime.strptime(datestring, pattern_no_micro)
        except:
            return None

def _is_json_response(res):
    try:
        json.loads(res._content)
        return True
    except (ValueError, TypeError):
        return False

def _safe_get_error_str(res):
    try:
        error_json = json.loads(res._content)
        if 'exc' in error_json: # this means traceback is available
            error_str = json.loads(error_json['exc'])[0]
        else:
            error_str = json.dumps(error_json)
    except:
        error_str = str(res.__dict__)
    return error_str

# setup logger and status
if not os.path.exists(config.LOGS_DIRECTORY):
    os.makedirs(config.LOGS_DIRECTORY)
error_logger = setup_logger('error_logger', '/'.join([config.LOGS_DIRECTORY, 'error.log']), logging.ERROR)
info_logger = setup_logger('info_logger', '/'.join([config.LOGS_DIRECTORY, 'logs.log']))
terminal_logger = setup_logger('terminal_logger', '/'.join([config.LOGS_DIRECTORY, 'terminal_status.log']))
status = SimpleDB('/'.join([config.LOGS_DIRECTORY, 'status.json']), auto_dump=True)

def infinite_loop(sleep_time=15):
    print("Service Running...")
    print("Press Ctrl+C to stop.")
    while True:
        try:
            # main() runs in a thread so a hung call (e.g. a network read that somehow
            # outlasts its own timeout, or any other unexpected blocking operation) can't
            # freeze the service forever - signal.alarm()/SIGALRM don't exist on Windows,
            # so Thread.join(timeout=...) is the simplest cross-platform wall-clock bound.
            # Python cannot forcibly kill a thread, so this is a backstop that lets the
            # loop keep attempting new cycles rather than a guarantee the stuck call itself
            # is cleaned up - per-request timeouts are what actually prevent the hang.
            cycle_thread = threading.Thread(target=main, daemon=True)
            cycle_thread.start()
            cycle_thread.join(MAIN_CYCLE_TIMEOUT_SECONDS)
            if cycle_thread.is_alive():
                error_logger.error(f"main() exceeded {MAIN_CYCLE_TIMEOUT_SECONDS}s watchdog - abandoning this cycle and continuing the loop")
            time.sleep(sleep_time)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            print(e)

if __name__ == "__main__":
    infinite_loop()
