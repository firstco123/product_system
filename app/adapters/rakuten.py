"""RakutenAdapter — RMS Order API (polling).

What this adapter does:
- `fetch_orders`: searchOrder + getOrder against Rakuten ペイ注文API.
- Webhook is not provided by Rakuten — `verify_webhook` is a stub returning True.
- `push_inventory`: ONE SKU, via PUT to 在庫API 2.0. It requires the SKU管理番号
  as well as the 商品管理番号, because this shop's catalogue is 100%
  項目選択肢別在庫 and a product-level write has nowhere to land.
- `push_inventory_batch`: up to 400 SKUs in ONE POST to bulk-upsert. Rakuten's
  rate limit is counted per REQUEST, so this is the difference between two calls
  and several hundred seconds for a whole-catalogue push.

Both return None on success: Rakuten answers an upsert with 204 No Content. The
wire contract — paths, body shapes, limits, error parsing — lives in
`app.adapters.rakuten_bulk_upsert`, built from the client's own RMS spec.

Authentication is ESA (Encrypted Service Auth):
    Authorization: ESA <base64(serviceSecret:licenseKey)>
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.adapters import rakuten_bulk_upsert as bulk_upsert
from app.adapters.base import (
    ChannelAdapter,
    NormalizedOrder,
    NormalizedOrderLine,
    NormalizedStatus,
)
from app.adapters.rate_limit import TokenBucket
from app.logging import get_logger

log = get_logger(__name__)

JST = timezone(timedelta(hours=9))

_BASE_URL = "https://api.rms.rakuten.co.jp/es/2.0/order"
_SEARCH_PATH = "/searchOrder/"
_GET_PATH = "/getOrder/"
# RakutenPay Order API response schema version. v7 is current as of 2024+.
_RAKUTEN_PAY_VERSION = 7

# 在庫API 2.0 (SKU単位). The old /es/2.0/inventory/updateInventory/ path that
# used to live here was never a Rakuten route at all — measured 2026-10-12, it
# answered 404 from Apache, the same as an invented URL, which is why F1.5 never
# once verified in production. It is deleted rather than kept as a fallback:
# a fallback to a non-route is just a slower failure.
#
# The wire contract lives in `app.adapters.rakuten_bulk_upsert`, built from the
# client's own RMS spec (2026-10-19). Stock is keyed on the PAIR
# (商品管理番号, SKU管理番号) because this shop's catalogue is 100% 項目選択肢別在庫.
_INVENTORY_HOST = "https://api.rms.rakuten.co.jp"


class RakutenTransientError(RuntimeError):
    """A failure worth retrying: a 5xx, or Rakuten asking us to slow down."""


# Rakuten status code -> normalized status (subset; expanded as needed).
_STATUS_MAP: dict[int, NormalizedStatus] = {
    100: "pending",  # 注文確認待ち
    200: "confirmed",  # 楽天処理中
    300: "confirmed",  # 発送待ち
    400: "shipped",  # 変更確定待ち / 発送済
    500: "delivered",  # 発送完了
    600: "cancelled",  # payment in progress / later treated as cancellation
    700: "cancelled",  # キャンセル確定待ち
    800: "cancelled",  # キャンセル確定
    900: "returned",
}


class RakutenAdapter(ChannelAdapter):
    channel = "rakuten"

    def __init__(
        self,
        *,
        service_secret: str,
        license_key: str,
        shop_url: str | None = None,
        client: httpx.AsyncClient | None = None,
        rate_limiter: TokenBucket | None = None,
        base_url: str = _BASE_URL,
    ) -> None:
        if not service_secret or not license_key:
            raise ValueError("service_secret and license_key are required")
        # Strip whitespace defensively — Secret Manager values copied from
        # Windows sources can carry trailing \r, which corrupts the base64
        # auth token and triggers ES04-01 Bad Request on every call.
        self._service_secret = service_secret.strip()
        self._license_key = license_key.strip()
        self._shop_url = shop_url
        self._base_url = base_url
        self._client = client
        self._owns_client = client is None
        # Rakuten RMS API: 1 req/s sustained is conservative; bursts up to 5.
        self._rate_limiter = rate_limiter or TokenBucket(rate=1, capacity=5)
        # 在庫API は受注APIと別ホスト。テストが差し替えられるよう属性にしてある。
        self._inventory_host = _INVENTORY_HOST

    async def __aenter__(self) -> RakutenAdapter:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
            self._owns_client = True
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------- ChannelAdapter API ----------

    async def fetch_orders(
        self,
        since: datetime,
        until: datetime | None = None,
    ) -> list[NormalizedOrder]:
        order_numbers = await self._search_order_numbers(since, until)
        if not order_numbers:
            return []
        out: list[NormalizedOrder] = []
        # Rakuten getOrder supports up to 100 order numbers per call.
        for batch in _batched(order_numbers, 100):
            details = await self._get_orders(batch)
            for raw in details:
                out.append(self._to_normalized(raw))
        return out

    async def push_inventory(
        self, sku: str, quantity: int, *, variant_key: str | None = None
    ) -> dict[str, Any] | None:
        """Set one Rakuten SKU's stock to `quantity` (absolute).

        `sku` is the 商品管理番号 and `variant_key` the SKU管理番号. The pair is
        required: this shop sells 項目選択肢別在庫 only, so a product-level write
        has nowhere to land. A caller that does not know the variant is a caller
        that does not know which size it is about, and guessing would publish one
        size's count onto all of them.

        Uses the single-SKU PUT, so one call can only ever touch one SKU. Batched
        writing goes through `push_inventory_batch`.

        Returns None: Rakuten answers a successful upsert with 204 No Content and
        no body, so there is nothing to record beyond the attempt itself.
        """
        if variant_key is None:
            raise ValueError(
                "Rakuten inventory is SKU-level; push_inventory needs the SKU管理番号 "
                f"(variant_key) for manageNumber={sku!r}"
            )
        row = bulk_upsert.InventoryRow(sku, variant_key, quantity)
        await self._upsert(
            "PUT",
            self._inventory_host + bulk_upsert.variant_path(row),
            bulk_upsert.build_variant_body(row),
            rows=[row],
        )
        return None

    async def push_inventory_batch(self, rows: list[bulk_upsert.InventoryRow]) -> None:
        """Set up to 400 SKUs in one request.

        Rakuten's rate limit is per REQUEST, so this is what makes a whole-
        catalogue push two calls instead of several hundred seconds. Callers
        still record one audit row per SKU; request granularity and audit
        granularity are deliberately not the same thing.
        """
        for batch in bulk_upsert.batches(rows):
            await self._upsert(
                "POST",
                self._inventory_host + bulk_upsert.BULK_UPSERT_PATH,
                bulk_upsert.build_body(batch),
                rows=batch,
            )

    async def _upsert(
        self, method: str, url: str, body: dict[str, Any], *, rows: list[bulk_upsert.InventoryRow]
    ) -> None:
        """One upsert request, read strictly.

        Success is 204 and nothing else. Anything else is raised with the rows
        whose state is now unknown, so the caller can read them back through the
        (granted, confirmed) bulk-get rather than assume the write did nothing.
        """
        status, payload = await self._send(method, url, body)
        if bulk_upsert.succeeded(status):
            return
        errors = "; ".join(str(e) for e in bulk_upsert.parse_errors(payload))
        unknown = bulk_upsert.rows_to_reread(rows, status)
        raise RuntimeError(
            f"Rakuten inventory upsert rejected (HTTP {status}): {errors} — "
            f"{len(unknown)} row(s) must be re-read with bulk-get before they are trusted"
        )

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        # Rakuten does not deliver webhooks for orders; polling is authoritative.
        return True

    # ---------- helpers ----------

    def _auth_header(self) -> dict[str, str]:
        token = base64.b64encode(f"{self._service_secret}:{self._license_key}".encode()).decode(
            "ascii"
        )
        return {
            "Authorization": f"ESA {token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST to the Order API base (kept stable for existing callers)."""
        return await self._post_url(self._base_url + path, body)

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, RakutenTransientError)),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    async def _send(self, method: str, url: str, body: dict[str, Any]) -> tuple[int, Any]:
        """One inventory request. Returns (status, parsed body or None).

        Three deliberate differences from `_post_url`:

        * **No `raise_for_status()`.** A 400 from Rakuten is a verdict about the
          data, not a transport fault; retrying it five times would just repeat a
          rejected write and delay the error by half a minute.
        * **No unconditional `.json()`.** A successful upsert is 204 with an
          empty body, and `resp.json()` raises on that — which, since
          JSONDecodeError is not an httpx error, would escape the retry and make
          `push_single` record every success as a failure.
        * **5xx and 429 are retried** as transient. Re-sending is safe because
          every write here is ABSOLUTE: the same request repeated converges on
          the same quantity rather than accumulating.
        """
        await self._rate_limiter.acquire()
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
            self._owns_client = True
        resp = await self._client.request(method, url, json=body, headers=self._auth_header())
        if resp.status_code >= 400:
            log.error(
                "rakuten.api_error",
                status=resp.status_code,
                url=url,
                body_preview=resp.text[:500],
            )
        if resp.status_code >= 500 or resp.status_code == 429:
            raise RakutenTransientError(f"HTTP {resp.status_code} from {url}")
        if resp.status_code == bulk_upsert.SUCCESS_STATUS or not resp.content:
            return resp.status_code, None
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, resp.text

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError,)),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    async def _post_url(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST to an absolute URL with the same auth, rate limit, and retry."""
        await self._rate_limiter.acquire()
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
            self._owns_client = True
        resp = await self._client.post(
            url,
            json=body,
            headers=self._auth_header(),
        )
        if resp.status_code >= 400:
            log.error(
                "rakuten.api_error",
                status=resp.status_code,
                url=url,
                body_preview=resp.text[:500],
            )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    async def _search_order_numbers(
        self,
        since: datetime,
        until: datetime | None,
    ) -> list[str]:
        # RakutenPay Order API searchOrder. dateType 1=注文日, 4=注文確定日, etc.
        # We use 1 (注文日) — every order has one, and incremental ingest catches
        # everything updated since the last poll regardless of fulfilment state.
        # All timestamps MUST be in JST per Rakuten spec.
        end = until or datetime.now(tz=UTC)
        # PaginationRequestModel and orderProgressList are REQUIRED by Rakuten
        # — omitting either returns ES04-01 Bad Request. requestRecordsAmount
        # default is 30; we use 1000 (the max) to minimize pagination passes.
        body: dict[str, Any] = {
            "dateType": 1,
            "startDatetime": _fmt_jst(since),
            "endDatetime": _fmt_jst(end),
            "orderProgressList": [100, 200, 300, 400, 500, 600, 700, 800, 900],
            "PaginationRequestModel": {
                "requestRecordsAmount": 1000,
                "requestPage": 1,
            },
        }
        data = await self._post(_SEARCH_PATH, body)
        numbers = data.get("orderNumberList") or []
        return [str(n) for n in numbers]

    async def _get_orders(self, order_numbers: list[str]) -> list[dict[str, Any]]:
        data = await self._post(
            _GET_PATH,
            {"orderNumberList": order_numbers, "version": _RAKUTEN_PAY_VERSION},
        )
        return list(data.get("OrderModelList") or [])

    @staticmethod
    def _to_normalized(raw: dict[str, Any]) -> NormalizedOrder:
        status = _STATUS_MAP.get(int(raw.get("orderProgress", 0)), "confirmed")
        items: list[NormalizedOrderLine] = []
        for pkg in raw.get("PackageModelList") or []:
            for ln in pkg.get("ItemModelList") or []:
                items.append(
                    NormalizedOrderLine(
                        line_id=str(ln.get("itemDetailId") or ln.get("itemNumber")),
                        channel_sku=str(ln.get("manageNumber") or ln.get("itemNumber") or ""),
                        # Every line of the 606 measured on 2026-10-12 carried
                        # one. Empty collapses to None so the resolver falls
                        # back to the page-level row rather than hunting for a
                        # mapping keyed on "".
                        channel_variant_key=(str(ln.get("variantId") or "") or None),
                        channel_product_id=str(ln.get("itemNumber") or ""),
                        product_name=(str(ln.get("itemName") or "") or None),
                        quantity=int(ln.get("units") or 0),
                        unit_price=Decimal(str(ln.get("price") or "0")),
                        currency="JPY",
                    )
                )
        ordered_at = datetime.fromisoformat(
            (raw.get("orderDatetime") or raw.get("ordDatetime") or "").replace("Z", "+00:00")
        )
        return NormalizedOrder(
            channel="rakuten",
            channel_order_id=str(raw["orderNumber"]),
            status=status,
            ordered_at=ordered_at,
            items=items,
            raw_payload=raw,
        )


def _batched(seq: list[str], size: int) -> list[list[str]]:
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def _fmt_jst(dt: datetime) -> str:
    """Format a datetime in Rakuten's expected `YYYY-MM-DDTHH:MM:SS+0900` shape."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(JST).strftime("%Y-%m-%dT%H:%M:%S+0900")
