FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persistent data directory (mounted as a Fly.io volume)
RUN mkdir -p /data

# Default process runs both admin web + poller thread via run.py.
CMD ["gunicorn", "-b", "0.0.0.0:8080", "--workers", "1", "--threads", "2", "--timeout", "120", "run:app"]
