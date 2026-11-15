import asyncio
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from yarl import URL


class MonitoringError(Exception):
    """Safe error text containing no response bodies or authentication secrets."""


class AuthenticationRequired(MonitoringError):
    pass


class Throttled(MonitoringError):
    pass


class VRChat:
    base = "https://api.vrchat.cloud/api/1"

    def __init__(self, user_agent, session_path: Path):
        self.user_agent = user_agent
        self.session_path = session_path
        self.http = None
        self.lock = asyncio.Lock()
        self.next_request = 0.0
        self.blocked_until = 0.0
        self.failures = 0
        self.authenticated = False

    async def start(self):
        self.http = aiohttp.ClientSession(
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=25))
        if self.session_path.exists():
            try:
                data = json.loads(self.session_path.read_text(encoding="utf-8"))
                cookies = {k: v for k, v in data.items()
                           if k in {"auth", "twoFactorAuth"} and isinstance(v, str)}
                self.http.cookie_jar.update_cookies(cookies, response_url=URL(self.base))
            except (ValueError, OSError):
                raise MonitoringError("Cannot read saved VRChat session; check the persistent volume.") from None

    def has_session(self):
        return bool(self.http.cookie_jar.filter_cookies(URL(self.base)).get("auth"))

    def save_session(self):
        cookies = self.http.cookie_jar.filter_cookies(URL(self.base))
        data = {k: v.value for k, v in cookies.items() if k in {"auth", "twoFactorAuth"}}
        if not data.get("auth"):
            raise MonitoringError("VRChat has not issued a session cookie.")
        temp = self.session_path.with_suffix(".json.tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(temp, self.session_path)
        os.chmod(self.session_path, 0o600)

    def clear_session(self):
        self.authenticated = False
        self.http.cookie_jar.clear()
        self.session_path.unlink(missing_ok=True)

    async def request(self, method, path, *, params=None, body=None, allow_404=False):
        async with self.lock:
            remaining = self.blocked_until - time.monotonic()
            if remaining > 0:
                raise Throttled(f"VRChat backoff active for another {int(remaining) + 1}s.")
            await asyncio.sleep(max(0, self.next_request - time.monotonic()))
            self.next_request = time.monotonic() + 2
            try:
                async with self.http.request(method, self.base + path, params=params,
                                             json=body, allow_redirects=False) as response:
                    status = response.status
                    if status == 429 or status >= 500:
                        self.failures += 1
                        try:
                            retry = float(response.headers.get("Retry-After", "0"))
                        except ValueError:
                            retry = 0
                        delay = max(retry, min(900, 30 * 2 ** min(self.failures, 5)))
                        self.blocked_until = time.monotonic() + delay
                        raise Throttled(f"VRChat HTTP {status}; checks paused for at least {int(delay)}s.")
                    if status in {401, 403}:
                        self.authenticated = False
                        raise AuthenticationRequired(f"VRChat HTTP {status}; owner login is required or access was denied.")
                    if status == 404 and allow_404:
                        self.failures = 0
                        return None
                    if status >= 400 or status < 200 or status >= 300:
                        raise MonitoringError(f"VRChat HTTP {status}; request was not accepted.")
                    try:
                        data = await response.json()
                    except (ValueError, aiohttp.ContentTypeError):
                        raise MonitoringError("VRChat returned an unexpected non-JSON response.") from None
                    if not isinstance(data, (dict, list)):
                        raise MonitoringError("VRChat returned an unexpected JSON shape.")
                    self.failures = 0
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError):
                self.failures += 1
                self.blocked_until = time.monotonic() + min(300, 15 * 2 ** min(self.failures, 4))
                raise MonitoringError("VRChat network error; checks temporarily paused.") from None

    async def current_user(self):
        data = await self.request("GET", "/auth/user")
        if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not data["id"].startswith("usr_"):
            self.authenticated = False
            raise AuthenticationRequired("VRChat login is incomplete; a fully approved session is required.")
        return data["id"]

    async def create_device(self):
        # Do not silently reuse an expired/wrong-account session for a fresh approval.
        self.clear_session()
        data = await self.request("POST", "/auth/device/code", body={})
        if not isinstance(data, dict) or not all(data.get(k) for k in
                ("deviceCode", "userCode", "verificationUrl", "expiresIn", "interval")):
            raise MonitoringError("VRChat device-approval response was incomplete.")
        url = urlparse(data["verificationUrl"])
        if url.scheme != "https" or url.netloc != "vrchat.com" or url.path != "/home/device":
            raise MonitoringError("VRChat returned an unexpected approval URL.")
        return data

    async def check_device(self, code):
        await self.request("POST", "/auth/device/status", body={"deviceCode": code})
        return self.has_session()

    async def avatar(self, aid, owner_id):
        data = await self.request("GET", f"/avatars/{aid}", allow_404=True)
        if data is None:
            # A working owner session is required before interpreting a 404 as loss.
            if await self.current_user() != owner_id:
                self.authenticated = False
                raise AuthenticationRequired("VRChat session account does not match the configured owner.")
            return "unavailable", "", "HTTP 404 with an active owner session; possible takedown or deletion."
        if not isinstance(data, dict) or data.get("id", "").lower() != aid or data.get("authorId") != owner_id:
            raise MonitoringError("Avatar response did not match this ID and owner; state was not changed.")
        state = data.get("releaseStatus")
        name = data.get("name", "")
        if not isinstance(name, str):
            raise MonitoringError("Avatar response had an invalid name field.")
        if state in {"public", "private"}:
            return state, name, f"VRChat releaseStatus={state}."
        if state == "hidden":
            return "unavailable", name, "VRChat releaseStatus=hidden; possible takedown."
        raise MonitoringError("Avatar returned an unfamiliar releaseStatus; state was not changed.")

    async def own_avatars(self, owner_id):
        result = {}
        for page in range(100):
            data = await self.request("GET", "/avatars", params={
                "user": "me", "releaseStatus": "all", "n": 100,
                "offset": page * 100, "sort": "created", "order": "ascending"})
            if not isinstance(data, list):
                raise MonitoringError("Avatar discovery returned an unexpected response.")
            for item in data:
                if not isinstance(item, dict) or item.get("authorId") != owner_id:
                    raise MonitoringError("Avatar discovery contained a different owner or malformed entry.")
                aid = item.get("id")
                name = item.get("name", "")
                if not isinstance(aid, str) or not isinstance(name, str):
                    raise MonitoringError("Avatar discovery contained invalid fields.")
                result[aid] = name
            if len(data) < 100:
                return result
        raise MonitoringError("Discovery pagination limit reached; incomplete results were discarded.")

    async def close(self):
        if self.http:
            await self.http.close()
