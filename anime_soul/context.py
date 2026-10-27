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

    def is_admin(self, user_id: Optional[int]) -> bool:
        return bool(user_id) and int(user_id) in self.cfg.all_admin_ids
