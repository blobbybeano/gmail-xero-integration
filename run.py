import os
import threading

os.environ.setdefault("WEB_PORT", "5000")
os.environ.setdefault("WEB_HOST", "0.0.0.0")

from app.admin_web import create_app
from app.main import run as run_poller

def _start_poller():
    try:
        run_poller()
    except Exception as exc:
        print(f"[poller] fatal error: {exc}", flush=True)

poller_thread = threading.Thread(target=_start_poller, name="calendar-poller", daemon=True)
poller_thread.start()

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("WEB_PORT", "5000"))
    app.run(host=host, port=port)
