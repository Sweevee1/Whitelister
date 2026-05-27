#!/usr/bin/env python3
"""
Dual-protocol proxy on LISTEN_PORT.
  0x16 (TLS ClientHello) → raw TCP tunnel to HTTPS gunicorn on UPSTREAM_PORT
  anything else           → HTTP 301 redirect to https://host:LISTEN_PORT/path
"""
import os
import signal
import socket
import sys
import threading

LISTEN_PORT = int(os.environ.get("PROXY_PORT", 8765))
UPSTREAM_PORT = int(os.environ.get("UPSTREAM_PORT", 8766))


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            chunk = src.recv(65536)
            if not chunk:
                break
            dst.sendall(chunk)
    except Exception:
        pass
    finally:
        # Half-close dst's write so the remote end sees EOF; leave src alone
        # so the other _pipe thread can drain it cleanly.
        try:
            dst.shutdown(socket.SHUT_WR)
        except Exception:
            pass


def _tunnel(conn: socket.socket) -> None:
    try:
        up = socket.create_connection(("127.0.0.1", UPSTREAM_PORT), timeout=5)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return
    for a, b in [(conn, up), (up, conn)]:
        threading.Thread(target=_pipe, args=(a, b), daemon=True).start()


def _http_redirect(conn: socket.socket) -> None:
    try:
        data = conn.recv(8192)
        host = ""
        path = b"/"
        lines = data.split(b"\r\n")
        parts = lines[0].split(b" ")
        if len(parts) >= 2:
            path = parts[1]
        for line in lines[1:]:
            if line.lower().startswith(b"host:"):
                host = line[5:].strip().decode(errors="replace").split(":")[0]
                break
        location = "https://{}:{}{}".format(
            host, LISTEN_PORT, path.decode(errors="replace")
        )
        resp = (
            "HTTP/1.1 301 Moved Permanently\r\n"
            "Location: {}\r\n"
            "Content-Length: 0\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).format(location).encode()
        conn.sendall(resp)
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def handle(conn: socket.socket) -> None:
    try:
        first = conn.recv(1, socket.MSG_PEEK)
        if not first:
            try:
                conn.close()
            except Exception:
                pass
            return
        if first == b"\x16":
            _tunnel(conn)
        else:
            _http_redirect(conn)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


def main() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", LISTEN_PORT))
    srv.listen(128)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print(
        f"Proxy listening on {LISTEN_PORT} -> upstream :{UPSTREAM_PORT}", flush=True
    )
    try:
        while True:
            try:
                conn, _ = srv.accept()
                threading.Thread(target=handle, args=(conn,), daemon=True).start()
            except OSError:
                break
    finally:
        srv.close()


if __name__ == "__main__":
    main()
