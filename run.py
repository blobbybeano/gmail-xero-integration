import os
import threading

os.environ.setdefault("WEB_PORT", "5000")
os.environ.setdefault("WEB_HOST", "0.0.0.0")

from app.admin_web import create_app
from app.main import run as run_poller

def _start_poller():
    import time
    delay = 5
    while True:
        try:
            run_poller()
        except Exception as exc:
            print(f"[poller] fatal error: {exc} — restarting in {delay}s", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 60)

# The background poller always starts.  It handles the email invoice scanner,
# Google Calendar watch management, and (when the Calendar→Xero sync toggle is
# ON in Live View) calendar event → Xero invoice creation.
# Xero API connectivity is now fully independent of this thread.
poller_thread = threading.Thread(
    target=_start_poller, name="calendar-poller", daemon=True
)
poller_thread.start()
print("[poller] background poller started (Calendar→Xero sync controlled by Live View toggle)", flush=True)

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("WEB_PORT", "5000"))
    app.run(host=host, port=port, threaded=True)
