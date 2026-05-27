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

# HTTP convenience redirect on 8764 → HTTPS 8765 (for old bookmarks / Unraid WebUI links)
python3 -c "
import http.server

class R(http.server.BaseHTTPRequestHandler):
    def _redirect(self):
        host = (self.headers.get('Host') or '').split(':')[0]
        self.send_response(301)
        self.send_header('Location', 'https://{}:8765{}'.format(host, self.path))
        self.end_headers()
    do_GET = do_POST = do_HEAD = _redirect
    def log_message(self, *a): pass

http.server.HTTPServer(('0.0.0.0', 8764), R).serve_forever()
" &

# Gunicorn serves HTTPS on the internal port only; the proxy below owns :8765 publicly.
gunicorn \
    --workers 1 \
    --threads 8 \
    --bind 127.0.0.1:8766 \
    --certfile /config/cert.pem \
    --keyfile /config/key.pem \
    "whitelister.app:create_app()" &

# Give gunicorn a moment before the proxy starts forwarding.
sleep 1

# Dual-protocol proxy on public port 8765:
#   TLS ClientHello (0x16) → raw tunnel to gunicorn on 8766
#   plain HTTP             → 301 redirect to https://host:8765/
exec python3 /app/proxy.py
