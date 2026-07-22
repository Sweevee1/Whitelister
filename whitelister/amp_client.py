import logging
import threading
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

GAMEMODES = {"survival", "creative", "adventure", "spectator"}


class AMPAuthError(Exception):
    pass


class AMPCommandError(Exception):
    pass


class AMPClient:
    def __init__(self, base_url: str, username: str, password: str, instance_id: str = ""):
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._instance_id = instance_id.strip()
        self._session_id = None
        self._sub_session_id = None  # Sub-instance session when using ADS proxy
        self._lock = threading.Lock()
        self._http = requests.Session()
        self._http.headers.update({"Accept": "application/json"})

    def _login(self) -> None:
        url = f"{self._base_url}/API/Core/Login"
        try:
            resp = self._http.post(
                url,
                json={
                    "username": self._username,
                    "password": self._password,
                    "token": "",
                    "rememberMe": False,
                },
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("success"):
                reason = data.get("resultReason") or "unknown"
                raise AMPAuthError(f"AMP login rejected (reason: {reason})")
            session_id = data.get("sessionID")
            if not session_id:
                raise AMPAuthError(f"Login succeeded but no sessionID returned: {data}")
            self._session_id = session_id
            logger.debug("AMP login successful")
        except requests.RequestException as e:
            raise AMPAuthError(f"AMP login request failed: {e}") from e

        # ADS proxy requires a separate login on the sub-instance through the proxy.
        # Without this the proxy returns "Session.Exists" for every call.
        if self._instance_id:
            self._proxy_login()

    def _proxy_login(self) -> None:
        url = f"{self._base_url}/API/ADSModule/Servers/{self._instance_id}/API/Core/Login"
        try:
            resp = self._http.post(
                url,
                json={
                    "SESSIONID": self._session_id,
                    "username": self._username,
                    "password": self._password,
                    "token": "",
                    "rememberMe": False,
                },
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("success"):
                reason = data.get("resultReason") or "unknown"
                raise AMPAuthError(f"AMP proxy login rejected (reason: {reason})")
            sub_session = data.get("sessionID")
            if not sub_session:
                raise AMPAuthError(f"Proxy login succeeded but no sessionID returned: {data}")
            self._sub_session_id = sub_session
            logger.debug("AMP proxy login successful")
        except requests.RequestException as e:
            raise AMPAuthError(f"AMP proxy login request failed: {e}") from e

    def _is_session_expired(self, response: requests.Response) -> bool:
        if response.status_code == 401:
            return True
        try:
            body = response.json()
            if isinstance(body, dict):
                if body.get("Status") == 5:
                    return True
                title = str(body.get("Title", "")).lower()
                msg = str(body.get("Message", "")).lower()
                if "not logged in" in title:
                    return True
                if "unauthorized" in title and "session.exists" in msg:
                    return True
        except Exception:
            pass
        return False

    def _post(self, endpoint: str, payload: dict, *, direct: bool = False) -> requests.Response:
        """POST to an AMP API endpoint.

        If instance_id is configured and direct=False, routes through the ADS proxy using
        the sub-instance session obtained during _proxy_login().
        direct=True forces the base URL path (used for GetInstances).
        """
        if self._instance_id and not direct:
            url = f"{self._base_url}/API/ADSModule/Servers/{self._instance_id}/API/{endpoint}"
            session = self._sub_session_id or self._session_id or ""
        else:
            url = f"{self._base_url}/API/{endpoint}"
            session = self._session_id or ""
        body = {"SESSIONID": session, **payload}
        return self._http.post(url, json=body, timeout=10)

    def reconfigure(self, base_url: str, username: str, password: str, instance_id: str = "") -> None:
        """Update connection settings and drop the current session so the next call re-auths."""
        with self._lock:
            self._base_url = base_url.rstrip("/")
            self._username = username
            self._password = password
            self._instance_id = instance_id.strip()
            self._session_id = None
            self._sub_session_id = None

    def ensure_session(self) -> None:
        with self._lock:
            if self._session_id is None:
                self._login()

    def _call(self, endpoint: str, payload: dict = None, *, direct: bool = False) -> requests.Response:
        """ensure_session + _post with one re-auth retry on session expiry."""
        self.ensure_session()
        try:
            resp = self._post(endpoint, payload or {}, direct=direct)
        except requests.RequestException as e:
            raise AMPCommandError(f"HTTP request failed: {e}") from e
        if self._is_session_expired(resp):
            logger.info("AMP session expired, re-authenticating")
            with self._lock:
                self._session_id = None
                self._sub_session_id = None
                self._login()
            try:
                resp = self._post(endpoint, payload or {}, direct=direct)
            except requests.RequestException as e:
                raise AMPCommandError(f"HTTP request failed after re-auth: {e}") from e
        return resp

    def send_console_command(self, command: str) -> None:
        resp = self._call("Core/SendConsoleMessage", {"message": command})
        if resp.status_code not in (200, 204):
            raise AMPCommandError(
                f"AMP returned HTTP {resp.status_code} for command: {command!r}"
            )
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("Title"):
            raise AMPCommandError(
                f"AMP rejected command {command!r}: {body.get('Message') or body.get('Title')}"
            )
        logger.debug("SendConsoleMessage response for %r: %s", command, body)
        logger.info("Console command sent: %s", command)

    def get_status(self) -> dict:
        resp = self._call("Core/GetStatus")
        if resp.status_code not in (200, 204):
            raise AMPCommandError(f"AMP returned HTTP {resp.status_code}")
        data = resp.json()
        if isinstance(data, dict) and data.get("Title"):
            raise AMPCommandError(f"AMP API error: {data.get('Message') or data.get('Title')}")
        return data

    def get_console_updates(self) -> list:
        """Return new console log entries since the last call (AMP's Core/GetUpdates)."""
        resp = self._call("Core/GetUpdates")
        if resp.status_code not in (200, 204):
            raise AMPCommandError(f"AMP returned HTTP {resp.status_code}")
        data = resp.json()
        if isinstance(data, dict) and data.get("Title"):
            raise AMPCommandError(f"AMP API error: {data.get('Message') or data.get('Title')}")
        entries = data.get("ConsoleEntries", []) if isinstance(data, dict) else []
        if entries:
            logger.debug("Core/GetUpdates console entries: %s", entries)
        return entries

    def get_instances(self) -> list:
        """Return the list of instances from the ADS panel (always direct, never proxied)."""
        resp = self._call("ADSModule/GetInstances", direct=True)
        if resp.status_code not in (200, 204):
            raise AMPCommandError(f"AMP returned HTTP {resp.status_code}")
        data = resp.json()
        parsed = urlparse(self._base_url)
        base_host = f"{parsed.scheme}://{parsed.hostname}"
        instances = []
        if isinstance(data, list):
            for target in data:
                for inst in target.get("AvailableInstances", []):
                    port = inst.get("Port", 0)
                    direct_url = f"{base_host}:{port}" if port else ""
                    instances.append({
                        "instance_id": inst.get("InstanceID", ""),
                        "name": inst.get("FriendlyName") or inst.get("InstanceName", ""),
                        "module": inst.get("Module", ""),
                        "running": inst.get("Running", False),
                        "port": port,
                        "url": direct_url,
                    })
        return instances

    def _assert_running(self) -> None:
        """Raise AMPCommandError if the Minecraft instance is not in the Running state."""
        try:
            data = self.get_status()
        except (AMPAuthError, AMPCommandError):
            raise
        except Exception as e:
            raise AMPCommandError(f"Failed to check instance state: {e}") from e
        state = data.get("State")
        # AMP ApplicationState: 20 = Running (server is up and processing commands).
        # Only block if we got a definitive non-running state; if State is absent we proceed.
        if state is not None and state != 20:
            raise AMPCommandError(
                f"Minecraft instance is not running (state={state}); whitelist command skipped"
            )

    def whitelist_add(self, username: str) -> None:
        self._assert_running()
        self.send_console_command(f"whitelist add {username}")

    def whitelist_remove(self, username: str) -> None:
        self.send_console_command(f"whitelist remove {username}")

    def set_gamemode(self, username: str, gamemode: str) -> None:
        if gamemode not in GAMEMODES:
            raise ValueError(f"Invalid gamemode: {gamemode!r}")
        self.send_console_command(f"gamemode {gamemode} {username}")
