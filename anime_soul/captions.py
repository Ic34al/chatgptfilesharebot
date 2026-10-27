"""Per-anime caption templates (Telegram HTML parse mode)."""
from __future__ import annotations

import html
import re
import string
from html.parser import HTMLParser
from typing import Dict

PLACEHOLDERS = ("anime_title", "season", "episode", "quality", "language", "release_date", "file_size",
                "channel_name")
CAPTION_LIMIT = 1024   # Telegram media caption limit (visible characters)
MESSAGE_LIMIT = 4096   # text message limit

DEFAULT_TEMPLATE = (
    "🔥 <b>{anime_title}</b>\n\n"
    "📺 Season: {season}\n"
    "🎬 Episode: {episode}\n"
    "🎧 Language: {language}\n"
    "💎 Quality: {quality}\n"
    "📦 Size: {file_size}\n"
    "🗓 Released: {release_date}\n\n"
    "The new episode is available!\n\n"
    "👇 Tap <b>GET EPISODE</b> below to get the actual episode file.\n\n"
    "Join <b>{channel_name}</b> for upcoming releases and updates."
)

ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a",
                "tg-spoiler", "span", "blockquote", "tg-emoji"}


class TemplateError(ValueError):
    pass


class _TagChecker(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.error = None

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.error = self.error or f"Tag <{tag}> is not supported by Telegram"
        if tag == "a":
            href = dict(attrs).get("href", "")
            if not re.match(r"^(https?://|tg://)", href or ""):
                self.error = self.error or "Links must start with https://, http:// or tg://"
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.error = self.error or f"Unbalanced closing tag </{tag}>"
        else:
            self.stack.pop()


def validate_template(template: str) -> None:
    if not template or not template.strip():
        raise TemplateError("Template is empty")
    if len(template) > 3000:
        raise TemplateError("Template is too long")
    try:
        fields = list(string.Formatter().parse(template))
    except ValueError as e:
        raise TemplateError(f"Bad braces: {e}. Use {{{{ and }}}} for literal braces.")
    for _, name, spec, conv in fields:
        if name is None:
            continue
        if name not in PLACEHOLDERS:
            raise TemplateError(f"Unknown placeholder {{{name}}}. Allowed: " +
                                ", ".join("{" + p + "}" for p in PLACEHOLDERS))
        if spec or conv:
            raise TemplateError("Format specs/conversions are not allowed in placeholders")
    checker = _TagChecker()
    checker.feed(template)
    checker.close()
    if checker.error:
        raise TemplateError(checker.error)
    if checker.stack:
        raise TemplateError(f"Unclosed tag <{checker.stack[-1]}>")


def visible_length(rendered_html: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", rendered_html)))


def human_size(n) -> str:
    if not n:
        return "N/A"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} GB"


def render(template: str, values: Dict[str, object], limit: int = CAPTION_LIMIT) -> str:
    validate_template(template)
    safe = {k: html.escape(str(values.get(k, "") if values.get(k) is not None else ""), quote=False)
            for k in PLACEHOLDERS}
    out = template.format_map(safe)
    if visible_length(out) <= limit:
        return out
    title = str(values.get("anime_title") or "")
    while len(title) > 10 and visible_length(out) > limit:
        title = title[: max(10, len(title) - 20)].rstrip() + "…"
        safe["anime_title"] = html.escape(title, quote=False)
        out = template.format_map(safe)
    if visible_length(out) > limit:
        raise TemplateError(f"Caption is longer than Telegram's {limit}-character limit")
    return out


def caption_values(anime: dict, episode: dict, cfg, file_size=None) -> Dict[str, object]:
    from .timeutil import fmt_ist
    storage = episode.get("storage") or {}
    return {
        "anime_title": anime.get("title_english") or anime.get("title_romaji") or "Unknown",
        "season": anime.get("season_number") or 1,
        "episode": episode.get("episode"),
        "quality": (episode.get("candidate") or {}).get("quality") or anime.get("quality") or cfg.default_quality,
        "language": (episode.get("candidate") or {}).get("language") or anime.get("language") or cfg.default_language,
        "release_date": fmt_ist(episode.get("airing_at")),
        "file_size": human_size(file_size or storage.get("file_size")),
        "channel_name": cfg.channel_name,
    }
