import requests
import json
from datetime import datetime
import time

# Set Software ZkBio Time Software URL, Username and password
# BASE_URL = "http://127.0.0.1:8080"
# USERNAME = "admin"
# PASSWORD = "admin@123"


# Erpnext config
ERPNEXT_API_KEY = ''
ERPNEXT_API_SECRET = ''
ERPNEXT_URL = 'https://demo.erprf.com'
ERPNEXT_VERSION = 15
# Set time
PULL_FREQUENCY = 1 # in minutes
IMPORT_START_DATE = '20250610'

# Try a wide range to make sure we don't miss any data
START_TIME = "2020-01-01 00:00:00"
END_TIME = "2030-12-31 23:59:59"





# =======================================================================================================================
# Headers for ERPNext API calls
erpnext_headers = {
    "Authorization": f"token {ERPNEXT_API_KEY}:{ERPNEXT_API_SECRET}",
    "Content-Type": "application/json"
}

# Step 1: Authenticate to the external system
def authenticate_external_system():
    auth_url = f"{BASE_URL}/api-token-auth/"
    auth_data = {
        "username": USERNAME,
        "password": PASSWORD
    }
    headers = {
        "Content-Type": "application/json"
    }

    auth_response = requests.post(auth_url, data=json.dumps(auth_data), headers=headers)
    if auth_response.status_code == 200:
        token = auth_response.json().get("token")
        print("✅ Authenticated to the external system")
        return token
    else:
        print("❌ Failed to authenticate to the external system")
        return None

# Step 2: Fetch devices from the system
def fetch_devices(token):
    devices_url = f"{BASE_URL}/iclock/api/terminals/"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Token {token}"
    }

    device_response = requests.get(devices_url, headers=headers)
    if device_response.status_code == 200:
        devices = device_response.json().get("data", [])
        return devices
    else:
        print("❌ Failed to fetch devices")
        return []

# Step 3: Fetch employees from the external system
def fetch_employees(token):
    emp_url = f"{BASE_URL}/personnel/api/employees/"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Token {token}"
    }

    emp_response = requests.get(emp_url, headers=headers, params={"page": 1, "page_size": 100})
    if emp_response.status_code == 200:
        employees = emp_response.json().get("data", [])
        return employees
    else:
        print("❌ Failed to fetch employees")
        return []

# Step 4: Fetch attendance data for each employee
def fetch_attendance_data(emp_code, token):
    attendance_url = f"{BASE_URL}/iclock/api/transactions/"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Token {token}"
    }

    att_params = {
        "emp_code": emp_code,
        "start_time": START_TIME,
        "end_time": END_TIME,
        "page": 1,
        "page_size": 100
    }

    att_response = requests.get(attendance_url, headers=headers, params=att_params)
    if att_response.status_code == 200:
        return att_response.json().get("data", [])
    else:
        print(f"❌ Failed to fetch attendance for employee {emp_code}")
        return []

# Step 5: Sync employee check-in data to ERPNext
def sync_employee_checkin_to_erpnext(employee, attendance_records, devices):
    # Check if the employee exists in ERPNext
    employee_url = f"{ERPNEXT_URL}/api/resource/Employee/{employee['emp_code']}"
    response = requests.get(employee_url, headers=erpnext_headers)

    if response.status_code == 200:
        print(f"✅ Employee {employee['emp_code']} exists in ERPNext, syncing check-in data.")
        
        # Select the first device (adjust if necessary to get the correct one)
        device_id = devices[0]['id'] if devices else None

        # Now sync check-ins for the employee
        for record in attendance_records:
            if record['punch_state_display'] == "Check In":
                log_type = "IN"

            # Prepare check-in data
            checkin_data = {
                "employee": employee['emp_code'],
                "log_type": log_type,
                "time": record['punch_time'],
                "device_id": device_id
            }

            # Correct API endpoint for creating employee check-in
            checkin_url = f"{ERPNEXT_URL}/api/resource/Employee Checkin"
            
            # Send the request to ERPNext to create the check-in
            checkin_response = requests.post(checkin_url, headers=erpnext_headers, data=json.dumps(checkin_data))

            if checkin_response.status_code == 200:
                print(f"✅ Synced check-in for {employee['emp_code']} at {checkin_data['time']}")
            else:
                print(f"❌ Failed to sync check-in for {employee['emp_code']} at {checkin_data['time']}, error: {checkin_response.text}")
    else:
        print(f"❌ Employee {employee['emp_code']} does not exist in ERPNext. Skipping check-in sync.")

# Main sync function
def sync_attendance():
    token = authenticate_external_system()
    if token:
        devices = fetch_devices(token)
        employees = fetch_employees(token)
        
        if employees:
            for emp in employees:
                emp_code = emp.get("emp_code")
                if emp_code:
                    attendance_data = fetch_attendance_data(emp_code, token)
                    if attendance_data:
                        print(f"Syncing check-in data for {emp_code}...")
                        sync_employee_checkin_to_erpnext(emp, attendance_data, devices)
                    else:
                        print(f"No attendance records found for employee {emp_code}")
                else:
                    print("⚠️ emp_code missing, skipping employee.")
        else:
            print("❌ No employees found.")
    else:
        print("❌ Authentication failed, aborting.")

# Run the sync every PULL_FREQUENCY minutes
while True:
    sync_attendance()
    print(f"⏳ Sleeping for {PULL_FREQUENCY} minutes...")
    time.sleep(PULL_FREQUENCY * 60)  # Convert minutes to seconds
