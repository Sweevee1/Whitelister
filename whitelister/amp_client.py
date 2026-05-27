import logging
import threading

import requests

logger = logging.getLogger(__name__)


class AMPAuthError(Exception):
    pass


class AMPCommandError(Exception):
    pass


class AMPClient:
    def __init__(self, base_url: str, username: str, password: str):
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._session_id = None
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
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            # sessionID is a top-level field, not nested inside "result"
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

    def _is_session_expired(self, response: requests.Response) -> bool:
        if response.status_code == 401:
            return True
        try:
            body = response.json()
            if isinstance(body, dict):
                if body.get("Status") == 5:
                    return True
                if "not logged in" in str(body.get("Title", "")).lower():
                    return True
        except Exception:
            pass
        return False

    def _post(self, endpoint: str, payload: dict) -> requests.Response:
        # SESSIONID must be in the request body, not a cookie
        url = f"{self._base_url}/API/{endpoint}"
        body = {"SESSIONID": self._session_id or "", **payload}
        return self._http.post(url, json=body, timeout=10)

    def ensure_session(self) -> None:
        with self._lock:
            if self._session_id is None:
                self._login()

    def send_console_command(self, command: str) -> None:
        self.ensure_session()
        try:
            resp = self._post("Core/SendConsoleMessage", {"message": command})
        except requests.RequestException as e:
            raise AMPCommandError(f"HTTP request failed: {e}") from e

        if self._is_session_expired(resp):
            logger.info("AMP session expired, re-authenticating")
            with self._lock:
                self._session_id = None
                self._login()
            try:
                resp = self._post("Core/SendConsoleMessage", {"message": command})
            except requests.RequestException as e:
                raise AMPCommandError(f"HTTP request failed after re-auth: {e}") from e

        if resp.status_code not in (200, 204):
            raise AMPCommandError(
                f"AMP returned HTTP {resp.status_code} for command: {command!r}"
            )
        logger.debug("Console command sent: %s", command)

    def whitelist_add(self, username: str) -> None:
        self.send_console_command(f"whitelist add {username}")

    def whitelist_remove(self, username: str) -> None:
        self.send_console_command(f"whitelist remove {username}")
