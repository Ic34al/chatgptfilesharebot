"""Shared runtime objects passed to handlers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .config import Config


@dataclass
class Ctx:
    cfg: Config
    db: Any                      # AnimeDB
    filedb: Any                  # database.FileDB (original)
    notifier: Any
    fsub: Any                    # FsubChecker
    sources: Any = None          # SourceRegistry
    anilist: Any = None          # AniListProvider
    monitor: Any = None          # ReleaseMonitor (when running in this process)
    pipeline: Any = None
    background: set = field(default_factory=set)
    extra_admins: set = field(default_factory=set)   # added with /addadmin (MongoDB bot_admins)
    stop_event: Any = None                           # asyncio.Event set by SIGTERM or /restart
    restart_requested: bool = False

    def is_owner(self, user_id: Optional[int]) -> bool:
        """Owners = OWNER_ID / ADMIN_IDS from Config Vars. Only they can add or remove admins."""
        return bool(user_id) and int(user_id) in self.cfg.all_admin_ids

    def is_admin(self, user_id: Optional[int]) -> bool:
        return self.is_owner(user_id) or (bool(user_id) and int(user_id) in self.extra_admins)
