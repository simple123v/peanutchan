import asyncio
import random
import time

from .store import avatar_id, now
from .vrchat import AuthenticationRequired, MonitoringError


class Engine:
    def __init__(self, config, store, api):
        self.config, self.store, self.api = config, store, api
        self.owner = None
        self.login_running = False
        self.attempted_restore = False
        self.due = {}
        self.next_slot = 0.0
        self.next_discovery = 0.0
        self.health_notified = {}
        self.health = "Waiting for owner login."
        self.last_success = None

    async def bind_session(self):
        uid = await self.api.current_user()
        expected = self.config.vrchat_user_id or self.store.get("vrchat_owner")
        if expected and uid != expected:
            self.api.clear_session()
            raise AuthenticationRequired("Approval was for a different VRChat account. Approve the original owner's account.")
        self.store.set("vrchat_owner", uid)
        self.api.save_session()
        self.owner = uid
        self.api.authenticated = True
        self.health = "Connected to the owner's VRChat account."
        self.next_discovery = 0
        return uid

    def report_error(self, error):
        self.health = str(error)
        stamp = time.monotonic()
        if stamp - self.health_notified.get(self.health, -1000000) > 900:
            self.store.health_event(self.health)
            self.health_notified[self.health] = stamp

    async def run(self):
        while True:
            try:
                if self.login_running:
                    await asyncio.sleep(1)
                    continue
                if not self.attempted_restore:
                    if self.api.has_session():
                        try:
                            await self.bind_session()
                        except AuthenticationRequired as error:
                            self.report_error(error)
                        except MonitoringError as error:
                            self.report_error(error)
                            await asyncio.sleep(5)
                            continue
                    self.attempted_restore = True
                if not self.api.authenticated:
                    await asyncio.sleep(2)
                    continue
                stamp = time.monotonic()
                if stamp < self.api.blocked_until:
                    await asyncio.sleep(1)
                    continue
                rows = self.store.rows()
                ids = {row["id"] for row in rows}
                self.due = {key: val for key, val in self.due.items() if key in ids}
                for index, row in enumerate(rows):
                    self.due.setdefault(row["id"], stamp + index * self.config.interval / max(1, len(rows)))
                ready = sorted((row for row in rows if self.due[row["id"]] <= stamp),
                               key=lambda row: self.due[row["id"]])
                if ready and stamp >= self.next_slot:
                    row = ready[0]
                    aid = row["id"]
                    try:
                        state, name, detail = await self.api.avatar(aid, self.owner)
                        # A command may have removed the avatar while the request ran.
                        self.store.observe(aid, state, name, detail)
                        self.last_success = now()
                        self.health = "Monitoring normally."
                    except MonitoringError as error:
                        self.store.error(aid, str(error))
                        self.report_error(error)
                    self.due[aid] = time.monotonic() + self.config.interval * random.uniform(0.95, 1.05)
                    self.next_slot = time.monotonic() + max(2, self.config.interval / max(1, len(rows))) * random.uniform(0.9, 1.1)
                elif stamp >= self.next_discovery:
                    try:
                        found = await self.api.own_avatars(self.owner)
                        ignored = self.store.get("ignored", [])
                        for raw_id, name in found.items():
                            aid = avatar_id(raw_id)
                            if aid not in ignored:
                                self.store.watch(aid, name)
                        self.store.set("last_discovery", now())
                    except (MonitoringError, ValueError) as error:
                        self.report_error(error)
                    self.next_discovery = time.monotonic() + self.config.discovery_interval * random.uniform(0.95, 1.05)
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Do not log arbitrary remote bodies, cookies, or approval codes.
                self.report_error(MonitoringError("Unexpected monitor error; retrying in 30 seconds."))
                await asyncio.sleep(30)
