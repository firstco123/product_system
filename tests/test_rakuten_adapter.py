"""RakutenAdapter unit tests — search + getOrder normalization via MockTransport."""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import httpx
import pytest

from app.adapters import RakutenAdapter
from app.adapters.rakuten import _variant_key
from app.adapters.rate_limit import TokenBucket


def _adapter(client: httpx.AsyncClient | None = None) -> RakutenAdapter:
    return RakutenAdapter(
        service_secret="srv",
        license_key="lic",
        shop_url="https://www.rakuten.co.jp/shop/",
        client=client,
        # Wide bucket so unit tests are not slowed by the limiter.
        rate_limiter=TokenBucket(rate=1000, capacity=1000),
    )


@pytest.mark.unit
def test_rejects_missing_credentials() -> None:
    with pytest.raises(ValueError):
        RakutenAdapter(service_secret="", license_key="x")
    with pytest.raises(ValueError):
        RakutenAdapter(service_secret="x", license_key="")


@pytest.mark.unit
def test_auth_header_uses_esa_base64() -> None:
    adapter = _adapter()
    header = adapter._auth_header()["Authorization"]
    assert header.startswith("ESA ")
    decoded = base64.b64decode(header[4:]).decode()
    assert decoded == "srv:lic"


@pytest.mark.unit
async def test_verify_webhook_is_noop_true() -> None:
    """Rakuten has no webhooks in Phase 1-A; the verifier returns True."""
    assert _adapter().verify_webhook({}, b"")


@pytest.mark.unit
async def test_push_inventory_refuses_a_product_level_write() -> None:
    """P2-039: Rakuten stock is SKU-level for this shop, so the SKU管理番号 is
    required. The full write path is covered in test_rakuten_push_inventory.py;
    this only pins the refusal, which needs no HTTP call."""
    with pytest.raises(ValueError, match="SKU管理番号"):
        await _adapter().push_inventory("SKU", 1)


@pytest.mark.unit
async def test_fetch_orders_normalizes_response() -> None:
    search_response = {"orderNumberList": ["240101-12345678-0001"]}
    get_response = {
        "OrderModelList": [
            {
                "orderNumber": "240101-12345678-0001",
                "orderProgress": 300,
                "orderDatetime": "2026-05-11T10:00:00+09:00",
                "PackageModelList": [
                    {
                        "ItemModelList": [
                            {
                                "itemDetailId": 11,
                                "manageNumber": "10087goldS",
                                "SkuModelList": [
                                    {
                                        "variantId": "10087-gold-s",
                                        "merchantDefinedSkuId": "10087goldS",
                                    }
                                ],
                                "itemNumber": "10111",
                                "units": 2,
                                "price": "3000",
                            }
                        ]
                    }
                ],
            }
        ]
    }
    responses = iter([search_response, get_response])

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(client=client)
        orders = await adapter.fetch_orders(since=datetime(2026, 5, 11, tzinfo=UTC))

    assert len(orders) == 1
    order = orders[0]
    assert order.channel == "rakuten"
    assert order.channel_order_id == "240101-12345678-0001"
    assert order.status == "confirmed"  # 300 = 発送待ち
    assert len(order.items) == 1
    assert order.items[0].channel_sku == "10087goldS"
    assert order.items[0].quantity == 2
    # The page alone cannot say which colour or size sold; the pair can.
    assert order.items[0].channel_variant_key == "10087-gold-s"


@pytest.mark.unit
async def test_fetch_orders_without_a_variant_id_leaves_the_key_none() -> None:
    """A line carrying no `variantId` must resolve by page, not by key "".

    An empty string would be looked up as a real variant key, match nothing,
    and — before the fallback was logged — silently behave like an unmapped
    pair. None is what makes the resolver take the page-level row directly.
    """
    search_response = {"orderNumberList": ["240101-12345678-0002"]}
    get_response = {
        "OrderModelList": [
            {
                "orderNumber": "240101-12345678-0002",
                "orderProgress": 300,
                "orderDatetime": "2026-05-11T10:00:00+09:00",
                "PackageModelList": [
                    {
                        "ItemModelList": [
                            {
                                "itemDetailId": 12,
                                "manageNumber": "10111",
                                "SkuModelList": [{"variantId": "", "merchantDefinedSkuId": ""}],
                                "itemNumber": "10111",
                                "units": 1,
                                "price": "3000",
                            }
                        ]
                    }
                ],
            }
        ]
    }
    responses = iter([search_response, get_response])

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(client=client)
        orders = await adapter.fetch_orders(since=datetime(2026, 5, 11, tzinfo=UTC))

    assert orders[0].items[0].channel_variant_key is None


@pytest.mark.unit
async def test_fetch_orders_empty_search_short_circuits() -> None:
    """An empty search response must NOT call getOrder."""
    calls = {"count": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json={"orderNumberList": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(client=client)
        assert await adapter.fetch_orders(since=datetime(2026, 5, 11, tzinfo=UTC)) == []
    assert calls["count"] == 1


@pytest.mark.unit
async def test_cancelled_status_normalizes() -> None:
    search_response = {"orderNumberList": ["X-1"]}
    get_response = {
        "OrderModelList": [
            {
                "orderNumber": "X-1",
                "orderProgress": 800,
                "orderDatetime": "2026-05-11T10:00:00+09:00",
                "PackageModelList": [],
            }
        ]
    }
    responses = iter([search_response, get_response])

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(client=client)
        orders = await adapter.fetch_orders(since=datetime(2026, 5, 11, tzinfo=UTC))
    assert orders[0].status == "cancelled"


@pytest.mark.unit
def test_the_variant_key_is_read_from_the_nested_sku_model() -> None:
    """The depth is the whole point. 楽天 puts the SKU管理番号 in
    `SkuModelList[0].variantId`; the item itself carries no `variantId` key at
    all — measured 2026-11-04 over 500 orders / 606 lines, every one of which
    had `SkuModelList` and none of which had a flat `variantId`. Reading the
    item level returned None for every order ever ingested, which is why the
    1,104 SKU-level mappings applied on 2026-10-19 never resolved a single line.
    """
    line = {
        "manageNumber": "10111c",
        "SkuModelList": [{"merchantDefinedSkuId": "10087goldM", "variantId": "gold-m-B50"}],
    }

    assert _variant_key(line) == "gold-m-B50"


@pytest.mark.unit
def test_the_variant_key_ignores_the_merchant_defined_sku_id() -> None:
    """`merchantDefinedSkuId` holds システム連携用SKU番号, not SKU管理番号, and
    `channel_variant_key` is keyed on the latter. Falling back to it would
    invent a key in a namespace no mapping uses — it would miss, degrade to the
    page-level row, and look exactly like the bug it was meant to fix.
    """
    line = {"SkuModelList": [{"merchantDefinedSkuId": "10087goldM", "variantId": ""}]}

    assert _variant_key(line) is None


@pytest.mark.unit
def test_the_variant_key_still_reads_a_flat_variant_id() -> None:
    """Kept as a fallback so the same identifier resolves at either depth. The
    shape 楽天 sends today is the nested one; this is only insurance."""
    assert _variant_key({"variantId": "gold-m-B50"}) == "gold-m-B50"


@pytest.mark.unit
def test_the_variant_key_is_none_when_no_sku_is_named() -> None:
    """A page-level line. None makes the resolver take the page-level mapping
    row; "" would be looked up as a real variant key and match nothing."""
    assert _variant_key({"manageNumber": "0012"}) is None
    assert _variant_key({"SkuModelList": []}) is None
