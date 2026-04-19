FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default process is web admin; worker runs via Fly process group.
CMD ["gunicorn", "-b", "0.0.0.0:8080", "app.admin_web:create_app()"]
