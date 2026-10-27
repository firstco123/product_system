"""Shared dependencies for the admin UI."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates

from app.models.enums import ChannelEnum
from app.services.timeframe import JST

_TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def humanize_status(value: str) -> str:
    return value.replace("_", " ").title()


def jst(value: Any) -> Any:
    """A database timestamp, in Japan time.

    Timestamps are stored in UTC and the screens printed them as they came,
    unlabelled, so an order placed at 12:51 showed in the event log as 03:51 —
    found 2026-10-02 while writing the client's test guide, which points them
    at exactly such an event. Dates pass through untouched (a stocktake date is
    already a calendar day), and so does a naive datetime, which has no zone to
    convert from.
    """
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(JST)
    return value


#: What each channel is called on screen.
#:
#: The screens printed the stored key, so a 検収 reader saw `rakuten` and
#: `shopify` in the same column as `未マッピング` — which is a label, not a key,
#: because unmapped revenue has no channel to store. Naming all of them stops
#: that row reading as a different kind of thing.
#:
#: Brand names stay in Latin script because that is how the client writes them
#: (docs/35 §1-4 names them 楽天 and Shopify).
CHANNEL_LABELS = {
    ChannelEnum.RAKUTEN.value: "楽天",
    ChannelEnum.SHOPIFY.value: "Shopify",
    ChannelEnum.AMAZON.value: "Amazon",
    ChannelEnum.WHOLESALE.value: "卸売",
}


def channel_label(value: Any) -> Any:
    """A channel key, as a reader should see it.

    Anything unknown passes through unchanged — `None`, and the already-Japanese
    `未マッピング` pseudo-channel — so a channel that reaches the data before it
    reaches this map is still legible rather than blank.
    """
    if not isinstance(value, str):
        return value
    return CHANNEL_LABELS.get(value, value)


templates.env.filters["humanize_status"] = humanize_status
templates.env.filters["channel_label"] = channel_label
templates.env.filters["jst"] = jst
