"""Slack Incoming Webhook notifier.

Phase 1-B F1.2. Behavior:
- When SLACK_WEBHOOK_URL is empty (the default until the client provides a
  real URL per D-3), `notify` is a structured no-op: the call is logged for
  audit but no HTTP request is made and no error is raised. This lets every
  caller assume `notify` is always safe to invoke.
- Level filtering: notifications below `slack_notify_min_level` are skipped.
  Ordering is critical > error > info; "error" is the default minimum, so
  info-level notifications stay quiet unless explicitly opted in.
- Attachment color is per Slack legacy attachments: red for critical, orange
  for error, blue for info. We use legacy attachments rather than Block Kit
  because the format is stable across all webhook URL types.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

import httpx

from app.config import Settings, get_settings
from app.logging import get_logger

log = get_logger(__name__)

Level = Literal["critical", "error", "info"]

_LEVEL_RANK: dict[Level, int] = {"critical": 30, "error": 20, "info": 10}
_LEVEL_COLOR: dict[Level, str] = {
    "critical": "#dc2626",  # red-600
    "error": "#f59e0b",  # amber-500
    "info": "#3b82f6",  # blue-500
}


def _coerce_level(level: str) -> Level:
    """Map an unrecognised level onto the default minimum instead of raising.

    An unknown level used to crash the CALLER, not the notifier. `level="warning"`
    (not one of the three levels) reached `_LEVEL_RANK[level]` and raised KeyError
    inside the bundle push's `session.begin()`, so every attempt record the run had
    written rolled back and the scheduled job returned 500. That ran hourly from
    2026-11-10 to 2026-11-19 and published no availability at all. Tests missed it
    because `notify` returns at `is_enabled` when no webhook URL is set, which is
    every test and was production until 2026-09-23.

    Raising an alert must never be able to fail the work that asked for it, so an
    unknown level degrades to "error" (visible at the default minimum) and is
    logged under its original name.
    """
    if level in _LEVEL_RANK:
        # `in` against dict[Level, int] narrows this to Level; no cast needed.
        return level
    log.warning("slack.unknown_level", level=level, used="error")
    return "error"


class SlackNotifier:
    def __init__(
        self,
        *,
        webhook_url: str,
        min_level: Level,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._webhook_url = webhook_url
        self._min_level: Level = min_level
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout_seconds

    @property
    def is_enabled(self) -> bool:
        return bool(self._webhook_url)

    def _should_send(self, level: Level) -> bool:
        return _LEVEL_RANK[level] >= _LEVEL_RANK[self._min_level]

    async def notify(
        self,
        *,
        level: Level,
        title: str,
        message: str,
        fields: list[tuple[str, str]] | None = None,
    ) -> bool:
        """Send a notification. Returns True if delivered, False if skipped
        (URL not configured, level filtered, or HTTP failed silently)."""
        level = _coerce_level(level)
        if not self.is_enabled:
            # WARNING, not debug. "Something asked for an alert and there is
            # nowhere to send it" is the single most important thing this
            # module can say, and saying it below the app's log level is how
            # it went unsaid. Production ran from deploy until 2026-09-23 with
            # no SLACK_WEBHOOK_URL on the service at all; the Rakuten 401
            # outage raised alerts every five minutes for three days and every
            # one of them stopped here, invisibly.
            log.warning("slack.skip_no_url", level=level, title=title)
            return False
        if not self._should_send(level):
            # INFO: a deliberate filter, but still a notification that was
            # asked for and not sent.
            log.info("slack.skip_below_min_level", level=level, min_level=self._min_level)
            return False

        attachment = self._build_attachment(level, title, message, fields or [])
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            resp = await client.post(self._webhook_url, json={"attachments": [attachment]})
            if 200 <= resp.status_code < 300:
                log.info("slack.delivered", level=level, title=title, status=resp.status_code)
                return True
            log.warning(
                "slack.http_error",
                level=level,
                title=title,
                status=resp.status_code,
                body_preview=resp.text[:200],
            )
            return False
        except httpx.HTTPError as exc:
            log.warning("slack.transport_error", level=level, title=title, error=str(exc))
            return False
        finally:
            if self._owns_client:
                await client.aclose()

    @staticmethod
    def _build_attachment(
        level: Level,
        title: str,
        message: str,
        fields: list[tuple[str, str]],
    ) -> dict[str, Any]:
        return {
            "color": _LEVEL_COLOR[level],
            "title": f"[{level.upper()}] {title}",
            "text": message,
            "fields": [{"title": k, "value": v, "short": len(v) < 30} for k, v in fields],
        }


def _notifier_from(settings: Settings) -> SlackNotifier:
    return SlackNotifier(
        webhook_url=settings.slack_webhook_url,
        min_level=settings.slack_notify_min_level,
    )


@lru_cache(maxsize=1)
def _default_slack_notifier() -> SlackNotifier:
    return _notifier_from(get_settings())


def get_slack_notifier(settings: Settings | None = None) -> SlackNotifier:
    """Process-wide notifier, configured from app settings.

    With no argument, returns a cached singleton built from `get_settings()`.
    Pass an explicit `settings` to build one from a specific Settings — this
    path is NOT cached, because pydantic Settings is unhashable and cannot be
    an lru_cache key (caching on it raised `TypeError: unhashable type`).
    """
    if settings is None:
        return _default_slack_notifier()
    return _notifier_from(settings)
