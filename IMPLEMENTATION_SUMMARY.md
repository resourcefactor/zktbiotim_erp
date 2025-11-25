# Multi-Terminal Attendance Tracking - Implementation Summary

## Problem Statement
The original system could not detect when individual biometric machines were disconnected from Biotime. If any machine was offline, the system would still update the shift type in ERPNext, leading to incomplete attendance processing.

## Solution Overview
Implemented comprehensive multi-terminal tracking that:
1. Fetches and tracks individual terminal/machine data
2. Monitors terminal connectivity
3. Only updates shift types when ALL terminals are active
4. Uses the earliest terminal timestamp for conservative sync

---

## Changes Made

### 1. Configuration Changes (`local_config.py`)

#### Added:
```python
# Terminal connectivity timeout
TERMINAL_TIMEOUT = 120  # minutes

# Optional: Explicitly list expected terminals
expected_terminals = {
    # 'biotime': ['Main Entrance', 'Back Door']
}

# Enhanced shift type mapping
shift_type_device_mapping = [
    {
        'shift_type_name': ['Morning', 'Testing'],
        'related_device_id': ['test'],
        'require_all_terminals': True  # NEW: Enforce terminal validation
    }
]
```

---

### 2. Core Code Changes (`erpnext_sync.py`)

#### A. New Functions

**`get_terminals_from_biotime(base_url, token)`** - Line 180
- Fetches list of registered terminals from Biotime API
- Endpoint: `/iclock/api/terminals/`
- Returns list of terminal aliases
- Stores terminal list in status.json

**`check_terminal_connectivity(device_id)`** - Line 380
- Validates all expected terminals have reported within timeout
- Returns (all_active, terminal_status)
- Logs warnings for offline terminals
- Checks both configured and auto-discovered terminals

#### B. Modified Functions

**`get_attendance_from_biotime()`** - Line 209
- **Added**: Extracts `terminal_alias` from each transaction record
- **Added**: Terminal distribution logging
- **New Structure**:
  ```python
  {
      "user_id": emp_code,
      "uid": id,
      "timestamp": datetime,
      "punch": punch_state,
      "status": 1,
      "terminal_alias": terminal_alias  # NEW
  }
  ```

**`pull_process_and_push_data()`** - Line 122
- **Added**: Fetches terminals at start of each cycle
- **Added**: Stores terminal list in status.json
- **Added**: Tracks `terminal_alias` for each attendance record
- **Added**: Updates per-terminal timestamps:
  - `<terminal_alias>_last_attendance_timestamp`
  - `<terminal_alias>_last_checked`
- **Added**: Enhanced console output with terminal breakdown

**`update_shift_last_sync_timestamp()`** - Line 468
- **Complete Rewrite**: Now validates terminal connectivity
- **Added**: Checks `require_all_terminals` flag
- **Added**: Calls `check_terminal_connectivity()` before update
- **Changed**: Uses earliest **terminal** timestamp instead of device timestamp
- **Added**: Comprehensive console logging of validation process
- **Logic**: Only updates shift type if:
  1. All devices have pushed data
  2. All terminals are active (if required)
  3. Terminal timestamps are available

**`main()`** - Line 41
- **Added**: Cycle start/end banners with timestamp
- **Added**: Device processing counters
- **Added**: Cycle duration tracking
- **Added**: Enhanced error handling per device
- **Added**: Cycle summary with statistics

#### C. Enhanced Logging

**Success Log Format** (now includes terminal):
```
<timestamp>  INFO  <emp_code>  <timestamp>  <punch>  <status>  <terminal_alias>  <json>
```

**New Terminal Status Log** (`logs/terminal_status.log`):
- Terminal connectivity status
- Active/offline status per terminal
- Last checked timestamps
- Warnings for offline terminals

---

### 3. Status.json Keys

#### New Keys:
```json
{
  "<device_id>_terminals": "[\"Terminal1\", \"Terminal2\"]",
  "<terminal_alias>_last_attendance_timestamp": "2025-11-25 10:30:45.123456",
  "<terminal_alias>_last_checked": "2025-11-25 10:30:45.123456"
}
```

#### Existing Keys (unchanged):
```json
{
  "lift_off_timestamp": "...",
  "mission_accomplished_timestamp": "...",
  "<device_id>_pull_timestamp": "...",
  "<device_id>_push_timestamp": "...",
  "<shift_type>_sync_timestamp": "..."
}
```

---

## Console Output Examples

### Successful Cycle with All Terminals Active:
```
############################################################
#  ATTENDANCE SYNC CYCLE STARTED
#  Time: 2025-11-25 10:30:00
############################################################

============================================================
Processing Device: biotime
============================================================

============================================================
Fetching terminals for device: biotime
============================================================
✓ Found 3 registered terminals
  └─ Main Entrance
  └─ Back Door
  └─ Loading Bay

============================================================
Fetching attendance from: biotime
Time range: 2025-11-22 10:30:00 to 2025-11-25 10:30:00
============================================================
✓ Found 145 attendance records from Biotime
  Attendance by Terminal:
    └─ Main Entrance: 58 records
    └─ Back Door: 42 records
    └─ Loading Bay: 45 records

============================================================
Processing 145 new attendance records
============================================================
✓ Terminal 'Main Entrance' last attendance: 2025-11-25 10:29:45
✓ Terminal 'Back Door' last attendance: 2025-11-25 10:28:12
✓ Terminal 'Loading Bay' last attendance: 2025-11-25 10:27:33
✓ Successfully processed attendance for 3 terminals

============================================================
SHIFT TYPE UPDATE VALIDATION
============================================================

Checking shift type(s): ['Morning', 'Testing']
Related devices: ['biotime']
Require all terminals: True

============================================================
Terminal Connectivity Check for device: biotime
============================================================
Expected terminals: 3
✓ Main Entrance: ACTIVE (last seen 0m ago)
✓ Back Door: ACTIVE (last seen 1m ago)
✓ Loading Bay: ACTIVE (last seen 2m ago)
============================================================
✓ All terminals ACTIVE - Safe to update shift type
============================================================

✓ All checks passed
  Earliest terminal timestamp: 2025-11-25 10:27:33
  Current sync timestamp for 'Morning': 2025-11-25 09:30:00
  → Updating shift type 'Morning' with timestamp: 2025-11-25 10:27:33
  ✓ Successfully updated shift type 'Morning'

############################################################
#  SYNC CYCLE COMPLETED
#  Duration: 8.45 seconds
#  Devices Processed: 1
#  Devices Failed: 0
#  Next cycle in: 1 minute(s)
############################################################
```

### When Terminal is Offline:
```
============================================================
Terminal Connectivity Check for device: biotime
============================================================
Expected terminals: 3
✓ Main Entrance: ACTIVE (last seen 2m ago)
✓ Back Door: ACTIVE (last seen 5m ago)
✗ Loading Bay: OFFLINE (last seen 3h ago)
============================================================
✗ 1 terminals OFFLINE - Skipping shift type update
============================================================

✗ Skipping shift type update: Not all terminals are active
```

---

## Testing Scenarios

### ✅ Scenario 1: All Terminals Online
**Expected**: Normal operation, shift type updated with earliest timestamp

### ✅ Scenario 2: One Terminal Offline
**Expected**:
- Warning logged
- Terminal status shows offline
- Shift type NOT updated
- Clear console message about which terminal is offline

### ✅ Scenario 3: Terminal Reconnects
**Expected**:
- Terminal marked active again
- Catch-up attendance processed
- Shift type updated with new data

### ✅ Scenario 4: No Terminals Configured
**Expected**:
- Auto-discovery from Biotime
- Terminals saved to status.json
- Normal operation continues

### ✅ Scenario 5: First Run (No Historical Data)
**Expected**:
- Graceful handling of missing timestamps
- Starts tracking from current cycle
- No false offline warnings

---

## Migration Notes

### For Existing Deployments:
1. **Backward Compatible**: Old logs without terminal info work fine
2. **First Run**: Will auto-discover terminals and populate status.json
3. **Configuration**: Update `local_config.py` with new settings
4. **Grace Period**: Set high `TERMINAL_TIMEOUT` initially (e.g., 240 minutes)

### Recommended Rollout:
1. Deploy code changes
2. Update configuration
3. Monitor `terminal_status.log` for 24-48 hours
4. Adjust `TERMINAL_TIMEOUT` based on observation
5. Enable `require_all_terminals` enforcement

---

## Benefits Achieved

✅ **Data Integrity**: Never process incomplete attendance
✅ **Fault Detection**: Immediately know when machines go offline
✅ **Conservative Sync**: Only sync up to point where ALL machines reported
✅ **Audit Trail**: Terminal-level logging for troubleshooting
✅ **Flexible**: Configurable timeout and enforcement per shift
✅ **Auto-Discovery**: Automatically detects new/removed machines
✅ **Visual Feedback**: Clear console output for monitoring
✅ **ERPNext Compatible**: Follows frappe/biometric-attendance-sync-tool pattern

---

## File Changes Summary

| File | Changes | Lines Modified |
|------|---------|----------------|
| `local_config.py` | Added terminal settings | ~20 lines |
| `erpnext_sync.py` | Core terminal tracking logic | ~300 lines |
| `README.md` | Documentation for multi-terminal | ~80 lines |

---

## Next Steps

1. **Test** with your Biotime setup
2. **Configure** expected_terminals if needed
3. **Monitor** terminal_status.log
4. **Adjust** TERMINAL_TIMEOUT based on your environment
5. **Enable** require_all_terminals after validation period

---

## Support

For issues or questions:
- Check `logs/error.log` for errors
- Check `logs/terminal_status.log` for terminal status
- Review console output for visual feedback
- Refer to `README.md` for configuration examples
