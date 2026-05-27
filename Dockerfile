FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY whitelister/ whitelister/

ENV CONFIG_PATH=/config/config.yaml

EXPOSE 8765

CMD ["gunicorn", "--workers", "1", "--threads", "8", "--bind", "0.0.0.0:8765", "whitelister.app:create_app()"]
