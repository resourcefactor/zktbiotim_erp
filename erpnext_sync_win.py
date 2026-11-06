import time
import threading
from pathlib import Path
from SMWinservice import SMWinservice
from erpnext_sync import main, error_logger, MAIN_CYCLE_TIMEOUT_SECONDS

class PythonCornerExample(SMWinservice):
    _svc_name_ = "ERPNextBiometricPushService"
    _svc_display_name_ = "ERPNext Biometric Push Service"
    _svc_description_ = "Service to push biometric data from device to ERPNext"

    def start(self):
        self.isrunning = True

    def stop(self):
        self.isrunning = False

    def main(self):
        while self.isrunning:
            # main() runs in a thread so a hung call can't freeze the Windows service
            # forever (signal.alarm()/SIGALRM don't exist on Windows) - same pattern as
            # erpnext_sync.infinite_loop(), which this service entry point does not call
            # into (it has its own isrunning-gated loop), so it needed its own copy of
            # this watchdog rather than getting it for free.
            cycle_thread = threading.Thread(target=main, daemon=True)
            cycle_thread.start()
            cycle_thread.join(MAIN_CYCLE_TIMEOUT_SECONDS)
            if cycle_thread.is_alive():
                error_logger.error(f"main() exceeded {MAIN_CYCLE_TIMEOUT_SECONDS}s watchdog - abandoning this cycle and continuing the loop")
            time.sleep(15)

if __name__ == '__main__':
    PythonCornerExample.parse_command_line()