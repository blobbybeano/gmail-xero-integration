import os
os.environ.setdefault("WEB_PORT", "5000")
os.environ.setdefault("WEB_HOST", "0.0.0.0")

from app.admin_web import create_app

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("WEB_PORT", "5000"))
    app.run(host=host, port=port)
