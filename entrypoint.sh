#!/bin/sh
# Generate a self-signed TLS cert on first boot (stored in /config so it persists).
# This lets the app serve HTTPS so Twitch OAuth redirect URIs work without a reverse proxy.
if [ ! -f /config/key.pem ] || [ ! -f /config/cert.pem ]; then
    echo "Generating self-signed TLS certificate..."
    openssl req -x509 -newkey rsa:2048 \
        -keyout /config/key.pem -out /config/cert.pem \
        -days 3650 -nodes \
        -subj '/CN=whitelister' 2>/dev/null
    echo "Certificate generated."
fi

exec gunicorn \
    --workers 1 \
    --threads 8 \
    --bind 0.0.0.0:8765 \
    --certfile /config/cert.pem \
    --keyfile /config/key.pem \
    "whitelister.app:create_app()"
