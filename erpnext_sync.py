
import local_config as config
import requests
import datetime
import json
import os
import sys
import time
import logging
from logging.handlers import RotatingFileHandler
from pickledb import PickleDB
from zk import ZK, const

EMPLOYEE_NOT_FOUND_ERROR_MESSAGE = "No Employee found for the given employee field value"
EMPLOYEE_INACTIVE_ERROR_MESSAGE = "Transactions cannot be created for an Inactive Employee"
DUPLICATE_EMPLOYEE_CHECKIN_ERROR_MESSAGE = "This employee already has a log with the same timestamp"
allowlisted_errors = [EMPLOYEE_NOT_FOUND_ERROR_MESSAGE, EMPLOYEE_INACTIVE_ERROR_MESSAGE, DUPLICATE_EMPLOYEE_CHECKIN_ERROR_MESSAGE]

if hasattr(config,'allowed_exceptions'):
    allowlisted_errors_temp = []
    for error_number in config.allowed_exceptions:
        allowlisted_errors_temp.append(allowlisted_errors[error_number-1])
    allowlisted_errors = allowlisted_errors_temp

device_punch_values_IN = getattr(config, 'device_punch_values_IN', [0,4])
device_punch_values_OUT = getattr(config, 'device_punch_values_OUT', [1,5])
ERPNEXT_VERSION = getattr(config, 'ERPNEXT_VERSION', 14)

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

def main():
    """Takes care of checking if it is time to pull data based on config,
    then calling the relevent functions to pull data and push to EPRNext.

    """
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

            for device in config.biotime_url:
                device_attendance_logs = None
                print(f"\n{'='*60}")
                print(f"Processing Device: {device['device_id']}")
                print(f"{'='*60}")
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
                    pull_process_and_push_data(device, device_attendance_logs)
                    status.set(f'{device["device_id"]}_push_timestamp', str(datetime.datetime.now()))
                    status.save()
                    if os.path.exists(dump_file):
                        os.remove(dump_file)
                        print(f"✓ Removed dump file")
                    print(f"✓ Successfully processed device: {device['device_id']}")
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

            # Cycle Summary
            print(f"\n{'#'*60}")
            print(f"#  SYNC CYCLE COMPLETED")
            print(f"#  Duration: {cycle_duration:.2f} seconds")
            print(f"#  Devices Processed: {devices_processed}")
            print(f"#  Devices Failed: {devices_failed}")
            print(f"#  Next cycle in: {config.PULL_FREQUENCY} minute(s)")
            print(f"{'#'*60}\n")
            info_logger.info(f"Mission Accomplished! Duration: {cycle_duration:.2f}s, Processed: {devices_processed}, Failed: {devices_failed}")
        else:
            # Still waiting for next cycle
            time_until_next = (last_lift_off_timestamp + datetime.timedelta(minutes=config.PULL_FREQUENCY) - datetime.datetime.now()).total_seconds()
            if time_until_next > 0:
                print(f"⏳ Waiting... Next sync in {int(time_until_next)}s", end='\r')
    except:
        error_logger.exception('exception has occurred in the main function...')
        print(f"\n✗ CRITICAL ERROR in main function - check error.log")


def pull_process_and_push_data(device, device_attendance_logs=None):
    """
    Handles pushing attendance logs to ERPNext from either BioTime or ZKTeco.
    """
    attendance_success_log_file = '_'.join(["attendance_success_log", device['device_id']])
    attendance_failed_log_file = '_'.join(["attendance_failed_log", device['device_id']])
    attendance_success_logger = setup_logger(attendance_success_log_file, '/'.join([config.LOGS_DIRECTORY, attendance_success_log_file])+'.log')
    attendance_failed_logger = setup_logger(attendance_failed_log_file, '/'.join([config.LOGS_DIRECTORY, attendance_failed_log_file])+'.log')

    # Determine start time
    import_start_date = _safe_convert_date(config.IMPORT_START_DATE, "%Y%m%d")
    last_sync = _safe_convert_date(status.get(f"{device['device_id']}_pull_timestamp"), "%Y-%m-%d %H:%M:%S.%f")
    if not last_sync:
        last_sync = import_start_date or (datetime.datetime.now() - datetime.timedelta(days=1))
    
    if not device_attendance_logs:
        token = get_biotime_token(device['BASE_URL'], device['USERNAME'], device['PASSWORD'])
        if not token:
            error_logger.error(f"Could not fetch BioTime token for device: {device['device_id']}")
            return

        # Fetch registered terminals from Biotime
        print(f"\n{'='*60}")
        print(f"Fetching terminals for device: {device['device_id']}")
        print(f"{'='*60}")
        terminals = get_terminals_from_biotime(device['BASE_URL'], token)

        # Store terminals in status for later validation
        if terminals:
            status.set(f"{device['device_id']}_terminals", json.dumps(terminals))
            status.save()

        now = datetime.datetime.now()
        start_time = now - datetime.timedelta(days=3)

        print(f"\n{'='*60}")
        print(f"Fetching attendance from: {device['device_id']}")
        print(f"Time range: {start_time} to {now}")
        print(f"{'='*60}")

        device_attendance_logs = get_attendance_from_biotime(
            base_url=device['BASE_URL'],
            token=token,
            start_time=start_time,
            end_time=now,
            device_id=device['device_id']
        )
        status.set(f"{device['device_id']}_pull_timestamp", str(now))
        status.save()
        if not device_attendance_logs:
            print(f"✗ No attendance logs found")
            return

    # Find the last successful push
    index_of_last = -1
    last_line = get_last_line_from_file('/'.join([config.LOGS_DIRECTORY, attendance_success_log_file])+'.log')
    if last_line:
        try:
            last_user_id, last_timestamp = last_line.split("\t")[4:6]
            last_timestamp = datetime.datetime.fromtimestamp(float(last_timestamp))
        except Exception:
            last_user_id, last_timestamp = None, None
        if last_timestamp and import_start_date and last_timestamp < import_start_date:
            last_timestamp = import_start_date
            last_user_id = None
    else:
        last_user_id, last_timestamp = None, import_start_date

    for i, log in enumerate(device_attendance_logs):
        if last_user_id and last_timestamp:
            if last_user_id == str(log['user_id']) and last_timestamp == log['timestamp']:
                index_of_last = i
                break
        elif last_timestamp:
            if log['timestamp'] >= last_timestamp:
                index_of_last = i
                break

    # Track terminals that reported in this batch
    terminal_last_timestamps = {}

    print(f"\n{'='*60}")
    print(f"Processing {len(device_attendance_logs[index_of_last+1:])} new attendance records")
    print(f"{'='*60}")

    for log in device_attendance_logs[index_of_last+1:]:
        punch_direction = device.get('punch_direction', 'AUTO')
        if punch_direction == 'AUTO':
            if log['punch'] in device_punch_values_OUT:
                punch_direction = 'OUT'
            elif log['punch'] in device_punch_values_IN:
                punch_direction = 'IN'
            else:
                punch_direction = None

        terminal_alias = log.get('terminal_alias', 'Unknown')

        erpnext_status_code, erpnext_message = send_to_erpnext(log['user_id'], log['timestamp'], device['device_id'], punch_direction)
        if erpnext_status_code == 200:
            # Track last timestamp for each terminal
            if terminal_alias not in terminal_last_timestamps or log['timestamp'] > terminal_last_timestamps[terminal_alias]:
                terminal_last_timestamps[terminal_alias] = log['timestamp']

            attendance_success_logger.info("\t".join([
                erpnext_message, str(log['uid']),
                str(log['user_id']), str(log['timestamp'].timestamp()),
                str(log['punch']), str(log['status']),
                terminal_alias,  # NEW: Include terminal alias
                json.dumps(log, default=str)
            ]))
        else:
            attendance_failed_logger.error("\t".join([
                str(erpnext_status_code), str(log['uid']),
                str(log['user_id']), str(log['timestamp'].timestamp()),
                str(log['punch']), str(log['status']),
                terminal_alias,  # NEW: Include terminal alias
                json.dumps(log, default=str)
            ]))
            if not any(error in erpnext_message for error in allowlisted_errors):
                raise Exception('API Call to ERPNext Failed.')

    # Update terminal last timestamps in status
    for terminal_alias, last_timestamp in terminal_last_timestamps.items():
        status.set(f"{terminal_alias}_last_attendance_timestamp", str(last_timestamp))
        status.set(f"{terminal_alias}_last_checked", str(datetime.datetime.now()))
        print(f"✓ Terminal '{terminal_alias}' last attendance: {last_timestamp}")

    status.save()
    print(f"✓ Successfully processed attendance for {len(terminal_last_timestamps)} terminals")

def get_biotime_token(base_url, username, password):
    url = f"{base_url}/api-token-auth/"
    headers = {"Content-Type": "application/json"}
    data = {"username": username, "password": password}
    try:
        res = requests.post(url, headers=headers, data=json.dumps(data))
        print("TOken Response:", res.text)
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
        res = requests.get(url, headers=headers)
        res.raise_for_status()
        terminals_data = res.json().get("data", [])

        # Extract terminal aliases
        terminal_aliases = [t.get("alias", "") for t in terminals_data if t.get("alias")]

        print(f"✓ Found {len(terminal_aliases)} registered terminals")
        for alias in terminal_aliases:
            print(f"  └─ {alias}")

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
    print(headers, "===================================")
    params = {
        "start_time": start_time.strftime("%Y-%m-%d %H:%M:%S"),
        "end_time": end_time.strftime("%Y-%m-%d %H:%M:%S"),
        "page": 1,
        "page_size": 100
    }
    try:
        print("Url", url)
        print("Params", params)
        res = requests.get(url, headers=headers, params=params)
        print(res.url,"reponse")
        print("Status", res.status_code)
        res.raise_for_status()
        records = res.json().get("data", [])
        print(f"✓ Found {len(records)} attendance records from Biotime")

        attendance_logs = [{
            "user_id": r["emp_code"],
            "uid": r["id"],
            "timestamp": datetime.datetime.strptime(r["punch_time"], "%Y-%m-%d %H:%M:%S"),
            "punch": int(r.get("punch_state", 0)),
            "status": 1,
            "terminal_alias": r.get("terminal_alias", "Unknown")  # NEW: Extract terminal alias
        } for r in records]

        # Log terminal distribution
        if attendance_logs:
            terminal_counts = {}
            for log in attendance_logs:
                terminal = log.get("terminal_alias", "Unknown")
                terminal_counts[terminal] = terminal_counts.get(terminal, 0) + 1

            print(f"  Attendance by Terminal:")
            for terminal, count in terminal_counts.items():
                print(f"    └─ {terminal}: {count} records")
        print(len(attendance_logs), "Attendance Logs ====================================")
        if attendance_logs:
            dump_file_name = get_dump_file_name_and_directory(device_id, "biotime")
            print((f"Writing dump to: {dump_file_name}"))

            try:
                print("===================================")
                with open(dump_file_name, 'w+') as f:
                    print("================ffffffffffffffffffffffffffff")
                    # f.write(json.dumps(list(map(lambda x: x.__dict__, attendances)), default=datetime.datetime.timestamp))
                    f.write(json.dumps(attendance_logs, default=datetime.datetime.timestamp))
           
                # with open(dump_file_name, 'w+') as f:
                #     f.write(json.dumps(attendance_logs, default=datetime.datetime.timestamp))
                print("Dump file written successfully")
            except Exception as e:
                print("Error on creating dump file")
        return attendance_logs
    except Exception as e:
        error_logger.exception(f"Error fetching attendance from biotome: {e}")
        return []

def get_all_attendance_from_device(ip, port=4370, timeout=30, device_id=None, clear_from_device_on_fetch=False):
    #  Sample Attendance Logs [{'punch': 255, 'user_id': '22', 'uid': 12349, 'status': 1, 'timestamp': datetime.datetime(2019, 2, 26, 20, 31, 29)},{'punch': 255, 'user_id': '7', 'uid': 7, 'status': 1, 'timestamp': datetime.datetime(2019, 2, 26, 20, 31, 36)}]
    zk = ZK(ip, port=port, timeout=timeout)
    conn = None
    attendances = []
    try:
        conn = zk.connect()
        x = conn.disable_device()
        # device is disabled when fetching data
        info_logger.info("\t".join((ip, "Device Disable Attempted. Result:", str(x))))
        attendances = conn.get_attendance()
        info_logger.info("\t".join((ip, "Attendances Fetched:", str(len(attendances)))))
        status.set(f'{device_id}_push_timestamp', None)
        status.set(f'{device_id}_pull_timestamp', str(datetime.datetime.now()))
        status.save()
        if len(attendances):
            # keeping a backup before clearing data incase the programs fails.
            # if everything goes well then this file is removed automatically at the end.
            dump_file_name = get_dump_file_name_and_directory(device_id, ip)
            with open(dump_file_name, 'w+') as f:
                f.write(json.dumps(list(map(lambda x: x.__dict__, attendances)), default=datetime.datetime.timestamp))
            if clear_from_device_on_fetch:
                x = conn.clear_attendance()
                info_logger.info("\t".join((ip, "Attendance Clear Attempted. Result:", str(x))))
        x = conn.enable_device()
        info_logger.info("\t".join((ip, "Device Enable Attempted. Result:", str(x))))
    except:
        error_logger.exception(str(ip)+' exception when fetching from device...')
        raise Exception('Device fetch failed.')
    finally:
        if conn:
            conn.disconnect()
    return list(map(lambda x: x.__dict__, attendances))


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
    response = requests.request("POST", url, headers=headers, json=data)
    if response.status_code == 200:
        return 200, json.loads(response._content)['message']['name']
    else:
        error_str = _safe_get_error_str(response)
        if EMPLOYEE_NOT_FOUND_ERROR_MESSAGE in error_str:
            error_logger.error('\t'.join(['Error during ERPNext API Call.', str(employee_field_value), str(timestamp.timestamp()), str(device_id), str(log_type), error_str]))
            # TODO: send email?
        else:
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

    print(f"\n{'='*60}")
    print(f"Terminal Connectivity Check for device: {device_id}")
    print(f"{'='*60}")
    print(f"Expected terminals: {len(expected_terminals)}")

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
                print(f"✓ {terminal_alias}: ACTIVE (last seen {minutes_ago}m ago)")
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

    print(f"{'='*60}")
    if all_active:
        print(f"✓ All terminals ACTIVE - Safe to update shift type")
        terminal_logger.info(f"Device '{device_id}': All {len(expected_terminals)} terminals active")
    else:
        inactive_count = sum(1 for t in terminal_status.values() if not t['active'])
        print(f"✗ {inactive_count} terminals OFFLINE - Skipping shift type update")
        inactive_terminals = [alias for alias, info in terminal_status.items() if not info['active']]
        terminal_logger.warning(f"Device '{device_id}': {inactive_count} terminals offline: {', '.join(inactive_terminals)}")
    print(f"{'='*60}\n")

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
    print(f"\n{'='*60}")
    print(f"SHIFT TYPE UPDATE VALIDATION")
    print(f"{'='*60}")

    for shift_type_device_map in shift_type_device_mapping:
        require_all_terminals = shift_type_device_map.get('require_all_terminals', True)

        print(f"\nChecking shift type(s): {shift_type_device_map['shift_type_name']}")
        print(f"Related devices: {shift_type_device_map['related_device_id']}")
        print(f"Require all terminals: {require_all_terminals}")

        all_devices_pushed = True
        all_terminals_active = True
        terminal_timestamp_array = []

        # Check each device
        for device_id in shift_type_device_map['related_device_id']:
            # Check if device has pushed data
            if not status.get(f'{device_id}_push_timestamp'):
                all_devices_pushed = False
                print(f"✗ Device '{device_id}' has not pushed data yet")
                break

            # Check terminal connectivity if required
            if require_all_terminals:
                terminals_active, terminal_info = check_terminal_connectivity(device_id)
                if not terminals_active:
                    all_terminals_active = False
                    print(f"✗ Not all terminals active for device '{device_id}'")
                    break

            # Get all terminal timestamps for this device
            terminals_json = status.get(f"{device_id}_terminals")
            if terminals_json:
                try:
                    terminals = json.loads(terminals_json)
                    for terminal_alias in terminals:
                        last_attendance_str = status.get(f"{terminal_alias}_last_attendance_timestamp")
                        if last_attendance_str:
                            last_attendance = _safe_convert_date(last_attendance_str, "%Y-%m-%d %H:%M:%S.%f")
                            if last_attendance:
                                terminal_timestamp_array.append(last_attendance)
                except:
                    pass

            # Fallback to device pull timestamp if no terminal timestamps available
            if not terminal_timestamp_array:
                device_pull_timestamp = _safe_convert_date(status.get(f'{device_id}_pull_timestamp'), "%Y-%m-%d %H:%M:%S.%f")
                if device_pull_timestamp:
                    terminal_timestamp_array.append(device_pull_timestamp)

        # Process shift type update if all conditions met
        if all_devices_pushed and all_terminals_active and terminal_timestamp_array:
            # Use the EARLIEST terminal timestamp to be conservative
            min_terminal_timestamp = min(terminal_timestamp_array)
            print(f"✓ All checks passed")
            print(f"  Earliest terminal timestamp: {min_terminal_timestamp}")

            if isinstance(shift_type_device_map['shift_type_name'], str):  # backward compatibility
                shift_type_device_map['shift_type_name'] = [shift_type_device_map['shift_type_name']]

            for shift in shift_type_device_map['shift_type_name']:
                try:
                    sync_current_timestamp = _safe_convert_date(status.get(f'{shift}_sync_timestamp'), "%Y-%m-%d %H:%M:%S.%f")
                    print(f"  Current sync timestamp for '{shift}': {sync_current_timestamp}")

                    if (sync_current_timestamp and min_terminal_timestamp > sync_current_timestamp) or (min_terminal_timestamp and not sync_current_timestamp):
                        print(f"  → Updating shift type '{shift}' with timestamp: {min_terminal_timestamp}")
                        response_code = send_shift_sync_to_erpnext(shift, min_terminal_timestamp)
                        if response_code == 200:
                            status.set(f'{shift}_sync_timestamp', str(min_terminal_timestamp))
                            status.save()
                            print(f"  ✓ Successfully updated shift type '{shift}'")
                        else:
                            print(f"  ✗ Failed to update shift type '{shift}' (HTTP {response_code})")
                    else:
                        print(f"  ⊘ No update needed for '{shift}' (timestamp not newer)")
                except:
                    error_logger.exception('Exception in update_shift_last_sync_timestamp, for shift:'+shift)
                    print(f"  ✗ Exception updating shift '{shift}'")
        else:
            if not all_devices_pushed:
                print(f"✗ Skipping shift type update: Not all devices have pushed data")
            elif not all_terminals_active:
                print(f"✗ Skipping shift type update: Not all terminals are active")
            elif not terminal_timestamp_array:
                print(f"✗ Skipping shift type update: No terminal timestamps available")

    print(f"{'='*60}\n")

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
        response = requests.request("PUT", url, headers=headers, data=json.dumps(data))
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
    return line


def setup_logger(name, log_file, level=logging.INFO, formatter=None):

    if not formatter:
        formatter = logging.Formatter('%(asctime)s\t%(levelname)s\t%(message)s')

    handler = RotatingFileHandler(log_file, maxBytes=10000000, backupCount=50)
    handler.setFormatter(formatter)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    if not logger.hasHandlers():
        logger.addHandler(handler)

    return logger

def get_dump_file_name_and_directory(device_id, device_ip):
    return config.LOGS_DIRECTORY + '/' + device_id + "_" + device_ip.replace('.', '_') + '_last_fetch_dump.json'

def _apply_function_to_key(obj, key, fn):
    obj[key] = fn(obj[key])
    return obj

def _safe_convert_date(datestring, pattern):
    try:
        return datetime.datetime.strptime(datestring, pattern)
    except:
        return None

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
status = PickleDB('/'.join([config.LOGS_DIRECTORY, 'status.json']))

def infinite_loop(sleep_time=15):
    print("Service Running...")
    while True:
        try:
            main()
            time.sleep(sleep_time)
        except BaseException as e:
            print(e)

if __name__ == "__main__":
    infinite_loop()
