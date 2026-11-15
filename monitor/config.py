import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    token: str
    guild_id: int
    owner_id: int
    user_agent: str
    data_dir: Path
    vrchat_user_id: str = ""
    interval: int = 90
    discovery_interval: int = 300

    @classmethod
    def from_env(cls):
        def required(key):
            value = os.environ.get(key, "").strip()
            if not value:
                raise ValueError(f"Set {key} in Railway Variables before starting.")
            return value

        agent = required("VRCHAT_USER_AGENT")
        if "/" not in agent or " " not in agent or "your-real-contact" in agent:
            raise ValueError("VRCHAT_USER_AGENT must include app/version and real contact information.")
        interval = int(os.getenv("CHECK_INTERVAL_SECONDS", "90"))
        discovery = int(os.getenv("DISCOVERY_INTERVAL_SECONDS", "300"))
        if interval < 60 or discovery < 60:
            raise ValueError("Check and discovery intervals must be at least 60 seconds.")
        return cls(required("DISCORD_TOKEN"), int(required("DISCORD_GUILD_ID")),
                   int(required("DISCORD_OWNER_ID")), agent,
                   Path(os.getenv("DATA_DIR", "data")),
                   os.getenv("VRCHAT_USER_ID", "").strip(), interval, discovery)
