import requests
import json
from datetime import datetime
import time
from pathlib import Path

# Set Software ZkBio Time Software URL, Username and password
BASE_URL = "http://127.0.0.1:8080"
USERNAME = "RfAdmin"
PASSWORD = "RfBioTime@2025"

# Erpnext config
ERPNEXT_API_KEY = '3e6ad70ba357e1f'
ERPNEXT_API_SECRET = 'cfc4057a78a5a9d'
ERPNEXT_URL = 'https://demo.erprf.com'
ERPNEXT_VERSION = 15

# Set time
PULL_FREQUENCY = 1  # in minutes
IMPORT_START_DATE = '20250610'
START_TIME = "2020-01-01 00:00:00"
END_TIME = "2030-12-31 23:59:59"

# Shift mapping
shift_type_device_mapping = [
    {'shift_type_name': ['Morning', 'Testing'], 'related_device_id': ['TST']}
]

# ERPNext Headers
erpnext_headers = {
    "Authorization": f"token {ERPNEXT_API_KEY}:{ERPNEXT_API_SECRET}",
    "Content-Type": "application/json"
}
# Logging Setup
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)


def log_error(msg):
    with open(LOG_DIR / "error_logs.txt", "a") as f:
        f.write(f"[{datetime.now()}] {msg}\n")


def log_failure(msg):
    with open(LOG_DIR / "failure_logs.txt", "a") as f:
        f.write(f"[{datetime.now()}] {msg}\n")


def log_last_sync_time(emp_code, punch_time):
    with open(LOG_DIR / "last_sync.txt", "w") as f:
        f.write(f"{emp_code}|{punch_time}")
def get_last_sync_info():
    path = LOG_DIR / "last_sync.txt"
    if path.exists():
        with open(path, "r") as f:
            line = f.read().strip()
            if "|" in line:
                emp_code, punch_time = line.split("|")
                return emp_code.strip(), punch_time.strip()
    return None, START_TIME


def update_shift_last_sync(device_id, punch_time):
    for mapping in shift_type_device_mapping:
        for shift_name in mapping["shift_type_name"]:
            try:
                url = f"{ERPNEXT_URL}/api/resource/Shift Type/{shift_name}"
                data = {"last_sync_of_checkin": punch_time}
                res = requests.put(url, headers=erpnext_headers, data=json.dumps(data))
                if res.status_code == 200:
                    print(f"✅ Updated shift '{shift_name}' with last sync time {punch_time}")
                else:
                    log_failure(f"Failed to update shift '{shift_name}'. Response: {res.text}")
            except Exception as e:
                log_error(f"Exception updating shift type '{shift_name}': {str(e)}")




def authenticate_external_system():
    auth_url = f"{BASE_URL}/api-token-auth/"
    auth_data = {"username": USERNAME, "password": PASSWORD}
    headers = {"Content-Type": "application/json"}

    try:
        auth_response = requests.post(auth_url, data=json.dumps(auth_data), headers=headers)
        if auth_response.status_code == 200:
            token = auth_response.json().get("token")
            print("✅ Authenticated to the external system")
            return token
        else:
            log_failure("Failed to authenticate to external system")
            return None
    except Exception as e:
        log_error(f"Exception during authentication: {str(e)}")
        return None


def fetch_devices(token):
    devices_url = f"{BASE_URL}/iclock/api/terminals/"
    headers = {"Content-Type": "application/json", "Authorization": f"Token {token}"}

    try:
        device_response = requests.get(devices_url, headers=headers)
        if device_response.status_code == 200:
            return device_response.json().get("data", [])
        else:
            log_failure("Failed to fetch devices")
            return []
    except Exception as e:
        log_error(f"Exception fetching devices: {str(e)}")
        return []


def fetch_employees(token):
    emp_url = f"{BASE_URL}/personnel/api/employees/"
    headers = {"Content-Type": "application/json", "Authorization": f"Token {token}"}

    try:
        emp_response = requests.get(emp_url, headers=headers, params={"page": 1, "page_size": 100})
        if emp_response.status_code == 200:
            return emp_response.json().get("data", [])
        else:
            log_failure("Failed to fetch employees")
            return []
    except Exception as e:
        log_error(f"Exception fetching employees: {str(e)}")
        return []


def fetch_attendance_data(emp_code, token):
    attendance_url = f"{BASE_URL}/iclock/api/transactions/"
    headers = {"Content-Type": "application/json", "Authorization": f"Token {token}"}
    att_params = {
        "emp_code": emp_code,
        "start_time": START_TIME,
        "end_time": END_TIME,
        "page_size": 100
    }
    try:
        att_response = requests.get(attendance_url, headers=headers, params=att_params)
        if att_response.status_code == 200:
            return att_response.json().get("data", [])
        else:
            log_failure(f"Failed to fetch attendance for employee {emp_code}")
            return []
    except Exception as e:
        log_error(f"Exception fetching attendance for {emp_code}: {str(e)}")
        return []


def sync_employee_checkin_to_erpnext(employee, attendance_records, devices):
    from pytz import timezone, utc
    emp_url = f"{ERPNEXT_URL}/api/resource/Employee"
    params = {
        "filters": json.dumps([["attendance_device_id", "=", str(employee["emp_code"])]]),
        "fields": json.dumps(["name", "attendance_device_id"])
    }
    try:
        response = requests.get(emp_url, headers=erpnext_headers, params=params)
        if response.status_code != 200:
            log_failure(f"Failed to fetch Employee from ERPNext: {response.text}")
            return

        employees_in_erpnext = response.json().get("data", [])
        if not employees_in_erpnext:
            print(f"⚠️ No Employee found in ERPNext with attendance_device_id = {employee['emp_code']}. Skipping.")
            return

        matched_employee = employees_in_erpnext[0]
        employee_name = matched_employee.get("name")
        device_id = matched_employee.get("attendance_device_id")

        for record in attendance_records:
            log_type = "IN" if record.get("punch_state_display") == "Check In" else "OUT"
            punch_time = record.get("punch_time")
            # local = timezone("Asia/Karachi")
            # navie_dt = datetime.strptime(punch_time_raw, "%Y-%m-%d %H:%M:%S")
            # localized_dt = local.localize(navie_dt)
            # punch_time = localized_dt.astimezone(utc).strftime("%Y-%m-%d %H:%M:%S")
           
            checkin_data = {
                "employee": employee_name,
                "log_type": log_type,
                "time": punch_time,
                "device_id": device_id
            }

            checkin_url = f"{ERPNEXT_URL}/api/resource/Employee Checkin"
            checkin_response = requests.post(checkin_url, headers=erpnext_headers, data=json.dumps(checkin_data))
            if checkin_response.status_code == 200:
                response_json = checkin_response.json()
                checkin_name = response_json.get("data", {}).get("name")
                if checkin_name:
                    log_last_sync_time(device_id, punch_time)
                    update_shift_last_sync(device_id, punch_time)
                else:
                    print(f"⚠️ No Checkin record returned even after 200 response: {response_json}")
                    log_failure(f"No Checkin created: {response_json}")
            else:
                # print("⛔ ERPNext Response:", checkin_response.text)
                log_failure(f"❌ Failed to sync check-in for {employee_name} at {punch_time}: {checkin_response.text}")

    except Exception as e:
        log_error(f"Exception during sync of employee {employee['emp_code']}: {str(e)}")


def sync_attendance():
    token = authenticate_external_system()
    if not token:
        print("❌ Authentication failed, aborting.")
        return

    devices = fetch_devices(token)
    employees = fetch_employees(token)
    if not employees:
        print("❌ No employees found.")
        return

    from dateutil import parser
    last_synced_emp, last_synced_time = get_last_sync_info()

    try:
        last_sync_dt = parser.parse(last_synced_time)
    except Exception:
        last_sync_dt = datetime.strptime(START_TIME, "%Y-%m-%d %H:%M:%S")
    for emp in employees:
        emp_code = emp.get("emp_code")
        if not emp_code:
            continue

        attendance_data = fetch_attendance_data(emp_code, token)
        # print(f"Attdendance for {emp_code}: {len(attendance_data)} records")
        # print(last_sync_dt, last_synced_time)
        

        # new_attendance = [
        #     r for r in attendance_data
        #     if r.get("punch_time") > last_synced_time
        # ]
        new_attendance = []
        latest_record_time = None
        for r in attendance_data:
            try:
                punch_time_str = r.get("punch_time")
                record_time = parser.parse(punch_time_str)
                if latest_record_time is None or record_time > latest_record_time:
                    latest_record_time = record_time
                if record_time > last_sync_dt:
                    new_attendance.append(r)
            except Exception as e:
                log_error(f"Error Parsing Punch time for {emp_code}")
        new_attendance.sort(key=lambda x: parser.parse(x["punch_time"]))
        if new_attendance:
            sync_employee_checkin_to_erpnext(emp, new_attendance, devices)
            latest_time = parser.parse(new_attendance[-1]["punch_time"])
            log_last_sync_time(emp_code, latest_time.strftime("%Y-%m-%d %H:%M:%S"))
        else:
            if latest_record_time:
                log_last_sync_time(emp_code, latest_record_time.strftime("%Y-%m-%d %H:%M:%S"))
        # for r in attendance_data:
        #     try:
        #         record_time = parser.parse(r.get("punch_time"))
        #         print(record_time, last_sync_dt, "===============okokokokoko")
        #         if record_time > last_sync_dt:
        #             new_attendance.append(r)
        #     except Exception as e:
        #         log_error(f"Error parsing punch time for {emp_code}")
        # new_attendance.sort(key=lambda x: parser.parse(x["punch_time"]))
        # print(emp, new_attendance, devices)
        # print(len(new_attendance))
        # if new_attendance:
        #     print(new_attendance)
        #     sync_employee_checkin_to_erpnext(emp, new_attendance, devices)
        #     latest_time = parser.parse(new_attendance[-1]["punch_time"])
        #     log_last_sync_time(emp.get("emp_code"),latest_time.strftime("%Y-%m-%d %H:%M:%S"))
        # else:
        #     return
        


# Run
while True:
    try:
        sync_attendance()
    except Exception as e:
        log_error(f"Unhandled exception during sync loop: {str(e)}")
    print(f"⏳ Sleeping for {PULL_FREQUENCY} minutes...")
    time.sleep(PULL_FREQUENCY * 60)
