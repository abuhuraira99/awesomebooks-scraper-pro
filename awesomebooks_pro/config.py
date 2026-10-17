"""Runtime configuration.

Every value can be overridden with an environment variable prefixed ``ABP_``
(for example ``ABP_PORT=7700``) so the same build runs on a laptop, a Windows
VPS or inside Docker without code changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 7642  # 5000 / 8000 / 7319 are taken by the client's other tools


def _env(name: str, default: str) -> str:
    return os.getenv(f"ABP_{name}", default)


@dataclass(frozen=True)
class Pace:
    """A request-pacing preset. ``interval`` is the global gap between requests."""

    key: str
    label: str
    interval: float  # seconds between requests, shared by every session
    description: str


PACES: dict[str, Pace] = {
    "careful": Pace("careful", "Careful", 2.0, "~30 requests/min. Maximum stealth for very long runs."),
    "balanced": Pace("balanced", "Balanced", 1.1, "~55 requests/min. Recommended default."),
    "fast": Pace("fast", "Fast", 0.6, "~100 requests/min. The adaptive guard still slows down on any warning."),
}
DEFAULT_PACE = "balanced"


@dataclass(frozen=True)
class Settings:
    base_url: str = field(default_factory=lambda: _env("BASE_URL", "https://www.awesomebooks.com").rstrip("/"))
    # Prices on AwesomeBooks depend on the shipping country. The client sells on Amazon US.
    ship_to: str = field(default_factory=lambda: _env("SHIP_TO", "US").upper())
    currency: str = field(default_factory=lambda: _env("CURRENCY", "USD").upper())
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", str(PROJECT_ROOT / "data"))))
    host: str = field(default_factory=lambda: _env("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("PORT", str(DEFAULT_PORT))))
    max_sessions: int = field(default_factory=lambda: int(_env("MAX_SESSIONS", "6")))
    request_timeout: float = field(default_factory=lambda: float(_env("REQUEST_TIMEOUT", "40")))
    # How many times a single request is retried on network errors / throttling.
    max_request_attempts: int = field(default_factory=lambda: int(_env("MAX_REQUEST_ATTEMPTS", "7")))
    # How many times an item is re-queued after a failed extraction before it is marked "error".
    max_item_attempts: int = field(default_factory=lambda: int(_env("MAX_ITEM_ATTEMPTS", "3")))
    # Optional file with one proxy URL per line (http://user:pass@host:port). Empty = direct.
    proxy_file: str = field(default_factory=lambda: _env("PROXY_FILE", ""))
    # Optional HTTP Basic Auth for the dashboard (recommended when HOST=0.0.0.0 on a VPS).
    auth_user: str = field(default_factory=lambda: _env("AUTH_USER", ""))
    auth_password: str = field(default_factory=lambda: _env("AUTH_PASSWORD", ""))
    # Finished runs are saved here automatically. Empty = the user's Downloads folder.
    export_dir: str = field(default_factory=lambda: _env("EXPORT_DIR", ""))
    # Resume a job that was interrupted by a crash, reboot or power cut as soon as the app starts again.
    auto_resume: bool = field(default_factory=lambda: _env("AUTO_RESUME", "1") not in ("0", "false", "no"))
    # Extra host names allowed in the Host header (comma separated) when serving beyond localhost.
    allowed_hosts: str = field(default_factory=lambda: _env("ALLOWED_HOSTS", ""))

    @property
    def catalog_db(self) -> Path:
        return self.data_dir / "catalog.db"

    @property
    def inventory_db(self) -> Path:
        return self.data_dir / "inventory.db"

    def resolve_export_dir(self) -> Path:
        if self.export_dir:
            return Path(self.export_dir)
        from .export import downloads_dir

        return downloads_dir()

    def load_proxies(self) -> list[str]:
        if not self.proxy_file:
            return []
        path = Path(self.proxy_file)
        if not path.is_file():
            return []
        lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
        return [line for line in lines if line and not line.startswith("#")]
