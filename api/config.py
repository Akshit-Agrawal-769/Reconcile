"""Settings from the environment, and the authenticated user carried through a request."""
import os
from dataclasses import dataclass
from typing import Optional

ROLES = ("partner", "staff", "client")


@dataclass(frozen=True)
class Settings:
    database_url: str
    secret: str
    static_dir: str = "web/dist"
    token_max_age: int = 8 * 60 * 60  # seconds


@dataclass(frozen=True)
class CurrentUser:
    user_id: int
    role: str  # 'partner' | 'staff' | 'client'
    client_id: Optional[int]
    firm_id: int


def load_settings(env=None) -> Settings:
    env = os.environ if env is None else env
    missing = [k for k in ("DATABASE_URL", "REC_SECRET") if not env.get(k)]
    if missing:
        raise RuntimeError(f"set {', '.join(missing)} in the environment")
    return Settings(database_url=env["DATABASE_URL"], secret=env["REC_SECRET"],
                    static_dir=env.get("REC_STATIC_DIR", "web/dist"))
