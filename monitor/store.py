import json
import re
import sqlite3
from datetime import datetime, timezone


ID_PATTERN = re.compile(r"avtr_[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def avatar_id(value):
    from urllib.parse import urlparse
    value = value.strip()
    if ID_PATTERN.fullmatch(value):
        return value.lower()
    url = urlparse(value)
    if url.scheme == "https" and url.hostname in {"vrchat.com", "www.vrchat.com"}:
        parts = url.path.rstrip("/").split("/")
        if len(parts) == 4 and parts[1:3] == ["home", "avatar"] and ID_PATTERN.fullmatch(parts[3]):
            return parts[3].lower()
    raise ValueError("Use an avtr_ ID or an https://vrchat.com/home/avatar/avtr_... link.")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS avatars (
            id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'unknown',
            ever_seen INTEGER NOT NULL DEFAULT 0, last_ok TEXT, checked_at TEXT, detail TEXT NOT NULL DEFAULT 'Waiting for first check');
          CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, avatar_id TEXT, kind TEXT NOT NULL,
            payload TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0);
        """)

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))

    def watch(self, aid, name=""):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO avatars (id,name) VALUES (?,?)", (aid, name[:200]))

    def unwatch(self, aid):
        with self.db:
            self.db.execute("DELETE FROM avatars WHERE id=?", (aid,))
            self.db.execute("DELETE FROM events WHERE avatar_id=? AND delivered=0", (aid,))
        ignored = self.get("ignored", [])
        if aid not in ignored:
            self.set("ignored", ignored + [aid])

    def rows(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM avatars ORDER BY id")]

    def error(self, aid, detail):
        with self.db:
            self.db.execute("UPDATE avatars SET checked_at=?,detail=? WHERE id=?", (now(), detail, aid))

    def observe(self, aid, state, name="", detail=""):
        if state not in {"public", "private", "unavailable"}:
            raise ValueError("Unknown observation state")
        stamp = now()
        with self.db:
            old = self.db.execute("SELECT * FROM avatars WHERE id=?", (aid,)).fetchone()
            if old is None:
                return
            accessible = state in {"public", "private"}
            kind = None
            if old["ever_seen"] and old["state"] != state:
                if state == "unavailable":
                    kind = "unavailable"
                elif old["state"] == "unavailable":
                    kind = "recovered"
                elif old["state"] == "public" and state == "private" and self.get("alerts", {}).get("private_alerts", True):
                    kind = "private"
            actual_name = name[:200] or old["name"] or aid
            if kind:
                payload = dict(name=actual_name, previous=old["state"], current=state,
                               observed_at=stamp, last_ok=old["last_ok"], detail=detail)
                self.db.execute("INSERT INTO events (avatar_id,kind,payload) VALUES (?,?,?)",
                                (aid, kind, json.dumps(payload)))
            self.db.execute("""UPDATE avatars SET name=?, state=?, ever_seen=?, last_ok=?,
                            checked_at=?, detail=? WHERE id=?""",
                            (actual_name, state, int(bool(old["ever_seen"]) or accessible),
                             stamp if accessible else old["last_ok"], stamp, detail, aid))

    def pending(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM events WHERE delivered=0 ORDER BY id LIMIT 20")]

    def health_event(self, detail):
        with self.db:
            self.db.execute("INSERT INTO events (kind,payload) VALUES (?,?)",
                            ("health", json.dumps(dict(detail=detail, observed_at=now()))))

    def delivered(self, event_id):
        with self.db:
            self.db.execute("UPDATE events SET delivered=1 WHERE id=?", (event_id,))

    def close(self):
        self.db.close()
