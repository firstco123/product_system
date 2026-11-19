"""Unit tests for the Slack notifier.

These tests use httpx.MockTransport — no network access. Covers:
- no-op when webhook URL is empty (per D-3 initial state)
- level filtering (min_level default 'error' suppresses info)
- successful delivery returns True and produces the expected payload
- HTTP non-2xx returns False and does not raise
- transport errors return False and do not raise
- attachment structure (color, title prefix, fields shape)
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import cast

import httpx
import pytest

from app.notifications.slack import Level, SlackNotifier


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_url_is_noop() -> None:
    n = SlackNotifier(webhook_url="", min_level="error")
    assert n.is_enabled is False
    sent = await n.notify(level="error", title="x", message="y")
    assert sent is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_below_min_level_skipped() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        n = SlackNotifier(
            webhook_url="https://hooks.slack.test/T/B/X",
            min_level="error",
            client=client,
        )
        sent = await n.notify(level="info", title="x", message="y")
    assert sent is False
    assert captured == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_successful_post() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        n = SlackNotifier(
            webhook_url="https://hooks.slack.test/T/B/X",
            min_level="error",
            client=client,
        )
        sent = await n.notify(
            level="critical",
            title="DB down",
            message="cannot reach postgres",
            fields=[("env", "prod"), ("sku_count", "0")],
        )

    assert sent is True
    assert len(captured) == 1
    body = json.loads(captured[0].content)
    assert "attachments" in body
    att = body["attachments"][0]
    assert att["title"].startswith("[CRITICAL]")
    assert att["color"] == "#dc2626"
    assert any(f["title"] == "env" and f["value"] == "prod" for f in att["fields"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_2xx_returns_false_no_raise() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        n = SlackNotifier(
            webhook_url="https://hooks.slack.test/T/B/X",
            min_level="error",
            client=client,
        )
        sent = await n.notify(level="error", title="x", message="y")
    assert sent is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transport_error_returns_false_no_raise() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failed")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        n = SlackNotifier(
            webhook_url="https://hooks.slack.test/T/B/X",
            min_level="error",
            client=client,
        )
        sent = await n.notify(level="error", title="x", message="y")
    assert sent is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attachment_color_by_level() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        for level, _expected_color in [
            ("critical", "#dc2626"),
            ("error", "#f59e0b"),
        ]:
            n = SlackNotifier(
                webhook_url="https://hooks.slack.test/T/B/X",
                min_level="error",
                client=client,
            )
            await n.notify(level=level, title="t", message="m")  # type: ignore[arg-type]
    assert [c["attachments"][0]["color"] for c in captured] == ["#dc2626", "#f59e0b"]


# ---------- get_slack_notifier factory ----------


@pytest.mark.unit
def test_get_slack_notifier_accepts_settings_object() -> None:
    """Regression: passing a (pydantic, unhashable) Settings must not raise.
    Previously get_slack_notifier was lru_cache'd and tried to hash Settings,
    raising `TypeError: unhashable type: 'Settings'` on every explicit call."""
    from app.config import Settings
    from app.notifications.slack import get_slack_notifier

    s = Settings(
        slack_webhook_url="https://hooks.slack.com/services/T/B/X",
        slack_notify_min_level="error",
    )
    n = get_slack_notifier(s)
    assert n._webhook_url == "https://hooks.slack.com/services/T/B/X"  # type: ignore[attr-defined]


@pytest.mark.unit
def test_get_slack_notifier_no_arg_is_cached_singleton() -> None:
    from app.notifications.slack import get_slack_notifier

    assert get_slack_notifier() is get_slack_notifier()


# --- 送れなかったことが見えるか ---------------------------------------------
#
# Production ran with no SLACK_WEBHOOK_URL on the Cloud Run service at all: the
# secret was wired into the verify-slack job and never into the service. The
# Rakuten 401 outage raised an alert every five minutes for three days and not
# one was delivered. Nothing showed it, because this path logged at DEBUG and
# the app runs at INFO — the logs even looked healthy, since the throttle in
# `_alert_job_failure` logs `internal.alert_suppressed` BEFORE calling notify.


class _Recorder:
    """Stands in for the module logger and records which METHOD was called.

    The level is the whole point of these tests, and structlog's own capture
    helper cannot see it here: `configure_logging` caches bound loggers, so
    once any other test in the suite has configured logging, a processor-level
    capture records nothing. Swapping the logger object is stable regardless of
    how logging happens to be configured, and still exercises the shipped call.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def _record(self, level: str) -> Callable[..., None]:
        def log(event: str, **_: object) -> None:
            self.calls.append((level, event))

        return log

    def __getattr__(self, name: str) -> Callable[..., None]:
        return self._record(name)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_unsendable_alert_is_logged_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """At WARNING, so it survives the production log level. "Something asked
    for an alert and there is nowhere to send it" is the most important thing
    this module can say."""
    recorder = _Recorder()
    monkeypatch.setattr("app.notifications.slack.log", recorder)

    await SlackNotifier(webhook_url="", min_level="error").notify(
        level="critical", title="定期ジョブ poll-rakuten が失敗しています", message="401"
    )

    assert ("warning", "slack.skip_no_url") in recorder.calls, recorder.calls


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_level_filtered_alert_is_still_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deliberate filter, but still a notification asked for and not sent.
    INFO is enough; silence is not."""
    recorder = _Recorder()
    monkeypatch.setattr("app.notifications.slack.log", recorder)

    await SlackNotifier(webhook_url="https://example.invalid/hook", min_level="critical").notify(
        level="info", title="x", message="y"
    )

    assert ("info", "slack.skip_below_min_level") in recorder.calls, recorder.calls


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_unknown_level_sends_instead_of_raising() -> None:
    """`_LEVEL_RANK[level]` used to raise KeyError straight out of `notify`, so a
    bad level crashed the CALLER rather than the alert. The bundle push passed
    "warning" from inside `session.begin()`, so the KeyError rolled back every
    attempt record the run had written and failed the hourly job with HTTP 500
    from 2026-11-10 until 2026-11-19. Raising an alert must not be able to fail
    the work that asked for it.
    """
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text="ok")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        n = SlackNotifier(
            webhook_url="https://hooks.slack.test/T/B/X", min_level="error", client=client
        )
        # Deliberately not one of the three levels, as the bundle push did.
        sent = await n.notify(level=cast("Level", "warning"), title="refused", message="m")

    assert sent is True
    assert len(captured) == 1
    att = json.loads(captured[0].content)["attachments"][0]
    # Degraded to "error", which is visible at the default minimum.
    assert att["title"].startswith("[ERROR]")
    assert att["color"] == "#f59e0b"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_unknown_level_is_logged_under_its_original_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The coercion runs before the `is_enabled` return, so the original name is
    recorded even when there is nowhere to send the alert."""
    recorder = _Recorder()
    monkeypatch.setattr("app.notifications.slack.log", recorder)

    await SlackNotifier(webhook_url="", min_level="error").notify(
        level=cast("Level", "warning"), title="x", message="y"
    )

    assert ("warning", "slack.unknown_level") in recorder.calls, recorder.calls
