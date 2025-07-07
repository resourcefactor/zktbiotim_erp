# ZKBioTime to ERPNext Attendance Integration

This guide helps you install and configure **ZKBioTime 9.0.3** and connect it with ERPNext using a Python script to fetch attendance data.


## ✅ Step 1: Software Installation

1. Download and install `ZKBioTime 9.0.3 Build_20241022.exe`.
2. During installation, set the **port** to `8080`.
3. After installation, complete the registration form with your **username, email, password**, etc.

---

## ✅ Step 2: Device Settings

1. Go to the device and set the **server address** of your system and the **server port** (set during software installation) in the **Cloud Server Settings**:
   ![Device Settings](https://github.com/user-attachments/assets/68bc44f2-a1c9-44f4-b45d-59ec0b7ea19d)

2. In **Security Settings**, enable **Standalone Communication**:
   ![Security Settings](https://github.com/user-attachments/assets/e4918afc-cd83-4014-94a2-d75289538c2a)

3. Set the **Device Type** to `T&A Push`:
   ![Device Type](https://github.com/user-attachments/assets/0e715e15-5b44-4e4c-9827-e03c79342166)

---

## ✅ Step 3: Connect Device with Software

1. In the software, enter the **IP address** and **serial number** of your device.
2. Then, click **Connect**.

---

## ✅ Step 4: Run the Python Script (Fetch Attendance to ERPNext)

1. Set your **ZKBioTime software** base URL, username, and password in the script:
   ![Base URL Settings](https://github.com/user-attachments/assets/29cf9cc1-54ad-4416-90b2-03ea24eb4bb2)

2. Set the **ERPNext configurations**:
   ![ERPNext Configs](https://github.com/user-attachments/assets/fd50c0cf-7830-464f-b9e1-d5feba266c07)

3. Set the **time settings**:
   ![Time Configs](https://github.com/user-attachments/assets/144f1c75-2ec4-4f1a-81c2-84f8718d9b1c)

4. Open your terminal, navigate to the script location, and run the following command:

   ```bash
   python zk.py
   ```
