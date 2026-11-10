"""Unit tests for InventoryPushService.

Uses a fake async session that records `add` / `flush` calls and assigns
ids on flush — no Postgres required. Adapter is a stub.
"""

from __future__ import annotations

import httpx
import pytest

from app.adapters.base import ChannelAdapter
from app.models import SyncAttempt, SyncAttemptStatusEnum
from app.notifications.slack import SlackNotifier
from app.services.inventory import (
    NEGATIVE_QUANTITY,
    UNKNOWN_QUANTITY,
    PushQuantity,
)
from app.services.inventory_push import InventoryPushService, PushRequest


class _FakeSession:
    """Minimal in-memory stand-in for AsyncSession.

    Records add/flush so the test can inspect them; assigns id on flush so
    `attempt.id` is observable, matching real ORM behavior.
    """

    def __init__(self) -> None:
        self._next_id = 1
        self.added: list[SyncAttempt] = []
        self.flush_calls = 0

    def add(self, obj: SyncAttempt) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flush_calls += 1
        for o in self.added:
            if o.id is None:
                o.id = self._next_id
                self._next_id += 1


class _StubAdapter(ChannelAdapter):
    channel = "shopify"

    def __init__(self, *, return_value=None, raise_exc: BaseException | None = None) -> None:
        self._return_value = return_value
        self._raise = raise_exc
        self.calls: list[tuple[str, int]] = []

    async def fetch_orders(self, since, until=None):  # pragma: no cover
        raise NotImplementedError

    async def push_inventory(self, sku: str, quantity: int):
        self.calls.append((sku, quantity))
        if self._raise is not None:
            raise self._raise
        return self._return_value

    def verify_webhook(self, headers, body):  # pragma: no cover
        return True


def _no_op_notifier() -> SlackNotifier:
    return SlackNotifier(webhook_url="", min_level="error")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_successful_push_records_succeeded_attempt() -> None:
    session = _FakeSession()
    adapter = _StubAdapter(return_value={"inventory_item_id": "abc"})
    svc = InventoryPushService(session, _no_op_notifier())  # type: ignore[arg-type]

    attempt = await svc.push_single(
        adapter,
        PushRequest(
            master_sku_id=10,
            channel_sku="SKU-001",
            quantity=7,
            triggered_by="reconcile_run:42",
            quantity_trust=PushQuantity.trust(7),
        ),
    )

    assert adapter.calls == [("SKU-001", 7)]
    assert len(session.added) == 1
    assert attempt.status == SyncAttemptStatusEnum.SUCCEEDED.value
    assert attempt.error_code is None
    assert attempt.finished_at is not None
    assert attempt.response_payload == {"inventory_item_id": "abc"}
    # 1 flush after insert (pending), 1 after marking succeeded
    assert session.flush_calls == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pending_state_persisted_before_call() -> None:
    """If the adapter raises, the attempt row must exist (status=failed)
    rather than be lost — proves the pending row was flushed up front."""
    session = _FakeSession()
    adapter = _StubAdapter(raise_exc=RuntimeError("api down"))
    svc = InventoryPushService(session, _no_op_notifier())

    attempt = await svc.push_single(
        adapter,
        PushRequest(
            master_sku_id=1,
            channel_sku="X",
            quantity=1,
            triggered_by="t",
            quantity_trust=PushQuantity.trust(1),
        ),
    )

    assert attempt.id is not None  # got an id from the initial flush
    assert attempt.status == SyncAttemptStatusEnum.FAILED.value
    assert attempt.error_code == "RuntimeError"
    assert attempt.error_message == "api down"
    assert attempt.finished_at is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failure_notifies_slack() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        notifier = SlackNotifier(
            webhook_url="https://hooks.slack.test/T/B/X",
            min_level="error",
            client=client,
        )
        session = _FakeSession()
        adapter = _StubAdapter(
            raise_exc=httpx.HTTPStatusError(
                "429 too many requests",
                request=httpx.Request("POST", "https://x"),
                response=httpx.Response(429),
            )
        )
        svc = InventoryPushService(session, notifier)
        await svc.push_single(
            adapter,
            PushRequest(
                master_sku_id=3,
                channel_sku="B23gold",
                quantity=2,
                triggered_by="t",
                quantity_trust=PushQuantity.trust(2),
            ),
        )

    assert len(captured) == 1
    import json

    body = json.loads(captured[0].content)
    att = body["attachments"][0]
    assert att["title"].startswith("[ERROR]")
    assert "B23gold" in str(att["fields"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_success_does_not_notify() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        notifier = SlackNotifier(
            webhook_url="https://hooks.slack.test/T/B/X",
            min_level="error",
            client=client,
        )
        session = _FakeSession()
        adapter = _StubAdapter(return_value=None)
        svc = InventoryPushService(session, notifier)
        await svc.push_single(
            adapter,
            PushRequest(
                master_sku_id=3,
                channel_sku="x",
                quantity=2,
                triggered_by="t",
                quantity_trust=PushQuantity.trust(2),
            ),
        )
    assert captured == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attempt_payload_records_request_inputs() -> None:
    session = _FakeSession()
    adapter = _StubAdapter()
    svc = InventoryPushService(session, _no_op_notifier())
    attempt = await svc.push_single(
        adapter,
        PushRequest(
            master_sku_id=5,
            channel_sku="N111gold",
            quantity=42,
            triggered_by="reconcile_run:99",
            quantity_trust=PushQuantity.trust(42),
            parent_attempt_id=11,
        ),
    )
    assert attempt.payload["channel_sku"] == "N111gold"
    assert attempt.payload["quantity"] == 42
    assert attempt.payload["triggered_by"] == "reconcile_run:99"
    assert attempt.parent_attempt_id == 11


@pytest.mark.unit
@pytest.mark.asyncio
async def test_error_message_truncated_to_500_chars() -> None:
    session = _FakeSession()
    adapter = _StubAdapter(raise_exc=RuntimeError("x" * 1000))
    svc = InventoryPushService(session, _no_op_notifier())
    attempt = await svc.push_single(
        adapter,
        PushRequest(
            master_sku_id=1,
            channel_sku="X",
            quantity=1,
            triggered_by="t",
            quantity_trust=PushQuantity.trust(1),
        ),
    )
    assert attempt.error_message is not None
    assert len(attempt.error_message) <= 500


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_error_message_falls_back_to_class_name() -> None:
    class _Bare(Exception):
        pass

    session = _FakeSession()
    adapter = _StubAdapter(raise_exc=_Bare())
    svc = InventoryPushService(session, _no_op_notifier())
    attempt = await svc.push_single(
        adapter,
        PushRequest(
            master_sku_id=1,
            channel_sku="X",
            quantity=1,
            triggered_by="t",
            quantity_trust=PushQuantity.trust(1),
        ),
    )
    assert attempt.error_message == "_Bare"


# ---------------------------------------------------------------------------
# The quantity floor. `push_single` is the gate because it is the one point
# every push passes through -- including the 同期エラー screen's 再実行 button,
# which calls it directly and was therefore never screened at all.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_uncounted_master_is_refused_and_the_channel_is_never_called() -> None:
    session = _FakeSession()
    adapter = _StubAdapter(return_value={"ok": True})
    svc = InventoryPushService(session, _no_op_notifier())

    attempt = await svc.push_single(
        adapter,
        PushRequest(
            master_sku_id=10,
            channel_sku="SKU-001",
            quantity=0,
            triggered_by="t",
            quantity_trust=PushQuantity.distrust(
                reason=UNKNOWN_QUANTITY, blamed_sku_code="B25goldanklet"
            ),
        ),
    )

    assert adapter.calls == []
    assert attempt.status == SyncAttemptStatusEnum.REFUSED.value
    assert attempt.error_code == UNKNOWN_QUANTITY
    assert attempt.finished_at is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_negative_master_is_refused() -> None:
    session = _FakeSession()
    adapter = _StubAdapter(return_value={"ok": True})
    svc = InventoryPushService(session, _no_op_notifier())

    attempt = await svc.push_single(
        adapter,
        PushRequest(
            master_sku_id=11,
            channel_sku="SKU-002",
            quantity=0,
            triggered_by="t",
            quantity_trust=PushQuantity.distrust(
                reason=NEGATIVE_QUANTITY, blamed_sku_code="N23gold"
            ),
        ),
    )

    assert adapter.calls == []
    assert attempt.status == SyncAttemptStatusEnum.REFUSED.value
    assert attempt.error_code == NEGATIVE_QUANTITY


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_counted_zero_is_published() -> None:
    """The asymmetry that makes this a trust test and not a magnitude test.

    Four rows in the catalogue are a legitimate 0 (B29silver, B38silveranklet).
    A `quantity > 0` floor would silence a real sell-out while still refusing
    nothing that matters.
    """
    session = _FakeSession()
    adapter = _StubAdapter(return_value={"ok": True})
    svc = InventoryPushService(session, _no_op_notifier())

    attempt = await svc.push_single(
        adapter,
        PushRequest(
            master_sku_id=12,
            channel_sku="B38silveranklet",
            quantity=0,
            triggered_by="t",
            quantity_trust=PushQuantity.trust(0),
        ),
    )

    assert adapter.calls == [("B38silveranklet", 0)]
    assert attempt.status == SyncAttemptStatusEnum.SUCCEEDED.value


@pytest.mark.unit
@pytest.mark.asyncio
async def test_refusal_names_the_component_to_count_not_the_master_pushed() -> None:
    """For a bundle the two differ, and only the component can be counted."""
    session = _FakeSession()
    adapter = _StubAdapter(return_value={"ok": True})
    svc = InventoryPushService(session, _no_op_notifier())

    attempt = await svc.push_single(
        adapter,
        PushRequest(
            master_sku_id=13,
            channel_sku="N22silver",
            quantity=0,
            triggered_by="t",
            quantity_trust=PushQuantity.distrust(
                reason=NEGATIVE_QUANTITY, blamed_sku_code="N23silver"
            ),
        ),
    )

    assert attempt.error_message is not None
    assert "N23silver" in attempt.error_message
    assert "N22silver" not in attempt.error_message


@pytest.mark.unit
@pytest.mark.asyncio
async def test_refusal_does_not_notify_slack() -> None:
    """A full Rakuten run today would refuse 268 targets. 268 messages is an
    outage-shaped alert for a condition whose fix is one stocktake; the batch
    caller sends a single summary instead."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        notifier = SlackNotifier(
            webhook_url="https://hooks.slack.test/T/B/X",
            min_level="error",
            client=client,
        )
        session = _FakeSession()
        adapter = _StubAdapter(return_value={"ok": True})
        svc = InventoryPushService(session, notifier)
        await svc.push_single(
            adapter,
            PushRequest(
                master_sku_id=14,
                channel_sku="SKU-003",
                quantity=0,
                triggered_by="t",
                quantity_trust=PushQuantity.distrust(reason=UNKNOWN_QUANTITY),
            ),
        )

    assert captured == []
