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

# HTTP on 8764 → redirect to HTTPS on 8765, so plain-HTTP clients don't get SSL errors.
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

exec gunicorn \
    --workers 1 \
    --threads 8 \
    --bind 0.0.0.0:8765 \
    --certfile /config/cert.pem \
    --keyfile /config/key.pem \
    "whitelister.app:create_app()"
