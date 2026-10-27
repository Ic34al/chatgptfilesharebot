"""Source-adapter interface.

A source is only used when an administrator has registered it with an
explicit authorization note (licence / permission to redistribute). The bot
never searches the open web, never bypasses DRM, logins or paywalls.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class SourceItem:
    source: str                 # source name
    kind: str                   # "telegram" | "local" | "url"
    ref: str                    # file_id | absolute path | URL
    file_name: str
    size: Optional[int] = None
    sha256: Optional[str] = None
    quality: Optional[str] = None
    language: Optional[str] = None
    owned: bool = True          # False => never delete (e.g. a file in a local library)
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_doc(self) -> Dict[str, Any]:
        return {"source": self.source, "kind": self.kind, "ref": self.ref, "file_name": self.file_name,
                "size": self.size, "sha256": self.sha256, "quality": self.quality, "language": self.language,
                "owned": self.owned, "extra": self.extra}

    @classmethod
    def from_doc(cls, d: Dict[str, Any]) -> "SourceItem":
        return cls(**{k: d.get(k) for k in ("source", "kind", "ref", "file_name", "size", "sha256",
                                             "quality", "language")},
                   owned=d.get("owned", True), extra=d.get("extra") or {})


class SourceUnavailable(Exception):
    """Temporary failure contacting a source."""


class SourceAdapter(abc.ABC):
    type = "base"

    def __init__(self, name: str, config: Dict[str, Any]):
        self.name = name
        self.config = config

    @abc.abstractmethod
    async def find(self, anime: dict, episode: int) -> Optional[SourceItem]:
        """Return a matching authorized item, or None if not (yet) available."""

    async def health(self) -> str:
        return "ok"

    async def close(self) -> None:
        pass
