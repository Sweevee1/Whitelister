FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY whitelister/ whitelister/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENV CONFIG_PATH=/config/config.yaml

EXPOSE 8765

CMD ["/app/entrypoint.sh"]
