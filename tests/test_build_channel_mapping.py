"""Unit tests for app/cli/build_channel_mapping.py (the variant-level 対応表)."""

from __future__ import annotations

import pytest

from app.cli.build_channel_mapping import (
    build_mapping,
    build_rakuten_index,
    build_shopify_index,
    extract_color,
    extract_size,
    extract_token,
    product_token,
    resolve_rakuten,
    resolve_shop_target,
    resolve_shopify,
)

pytestmark = pytest.mark.unit

# ---------- token / color / size ----------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("316L heart signet ring #R69", "R69"),
        ("シルバー925 ブレスレット B13", "B13"),  # bare token, no '#'
        ("s925 compact horseshoe bracelet #B71", "B71"),  # s925 not a token
        ("メンズ ネックレス #N100", "N100"),  # 3-digit
        ("ジュエリーボックス", None),  # packaging — no token
        ("", None),
        ("#R09 sun ring", "R09"),  # token not at the very end
        ("316L hawaiian horseshoe ring ?R52", "R52"),  # '?' = mojibake'd '#'
        ("316L id chain bracelet ?B49", "B49"),
    ],
)
def test_extract_token(name: str, expected: str | None) -> None:
    assert extract_token(name) == expected


@pytest.mark.unit
def test_extract_color() -> None:
    assert extract_color("??? / GOLD", "B71gold") == "gold"
    assert extract_color("US7 / SILVER") == "silver"
    assert extract_color("Default Title", "R05") == ""


@pytest.mark.unit
def test_extract_color_combined() -> None:
    # A single two-tone variant must not be collapsed onto the 'gold' bucket.
    assert extract_color("gold & silver") == "gold&silver"
    assert extract_color("R19goldsilverus7") == "gold&silver"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("texts", "expected"),
    [
        (("US7 / GOLD",), "7"),
        (("USA9",), "9"),
        (("50cm",), "50"),
        (("フリー",), ""),
        (("M",), "M"),
        (("Default Title", "R05"), ""),
        # dual-type products encode the type in the second axis, on every channel
        (("アンクレット", "gold"), "anklet"),  # CROSS MALL 属性２名
        (("ブレスレット",), "bracelet"),
        (("?? / GOLD", "B09goldanklet"), "anklet"),  # Shopify sku suffix
        (("ネックレス",), "necklace"),
    ],
)
def test_extract_size(texts: tuple[str, ...], expected: str) -> None:
    assert extract_size(*texts) == expected


@pytest.mark.unit
def test_dual_type_anklet_bracelet_resolves_distinctly() -> None:
    # #B09 is sold as both an anklet AND a bracelet in gold; each must map to its
    # OWN Shopify + Rakuten SKU (previously they collapsed and went to confirm).
    shop = build_shopify_index(
        [
            ("B09goldanklet", "horseshoe anklet #B09", "?? / GOLD", "iidA"),
            ("B09goldbracelet", "horseshoe anklet #B09", "?? / GOLD", "iidB"),
        ]
    )
    rk = build_rakuten_index(
        [
            {"manage": "027c", "name": "anklet #B09", "sku_mgmt": "", "opt1": "", "opt2": ""},
            {
                "manage": "027c",
                "name": "",
                "sku_mgmt": "1308",
                "opt1": "gold",
                "opt2": "アンクレット",
            },
            {
                "manage": "027c",
                "name": "",
                "sku_mgmt": "1307",
                "opt1": "gold",
                "opt2": "ブレスレット",
            },
        ]
    )
    rk.manage.add("027c")
    rk.manage_token["027c"] = "B09"
    xm_name = {"027c": "anklet #B09"}
    xm_var = {
        "027c": [
            {"sku": "027cgoldanklet", "color": "gold", "size": "anklet"},
            {"sku": "027cgoldbracelet", "color": "gold", "size": "bracelet"},
        ]
    }
    mapping, _confirm, stats = build_mapping(
        xm_name=xm_name, xm_var=xm_var, stock_map={}, rk=rk, shop=shop
    )
    assert stats["full"] == 2
    by_type = {r[3]: (r[4], r[6]) for r in mapping}  # size(type) -> (shopify, rakuten)
    assert by_type["anklet"] == ("B09goldanklet", "1308")
    assert by_type["bracelet"] == ("B09goldbracelet", "1307")


# ---------- Shopify index + resolver ----------


def _shop():  # type: ignore[no-untyped-def]
    # (sku, product_title, variant_title, inventory_item_id)
    rows = [
        ("B13gold", "bracelet #B13", "??? / GOLD", "iidB13g"),
        ("B13silver", "bracelet #B13", "??? / SILVER", "iidB13s"),
        ("R05", "feather ring #R05", "??? / GOLD", "iidR05"),  # single, gold-tagged
        ("R09goldus7", "sun ring #R09", "US7 / GOLD", "iidR09g7"),
        ("R09goldus9", "sun ring #R09", "US9 / GOLD", "iidR09g9"),
        ("", "no-sku ring #N90", "??? / GOLD", "iidN90"),  # empty SKU
        ("B17gold", "stud bangle #B17", "??? / GOLD", "iidB17"),  # single, gold only
        # B29gold reused on TWO products (bracelet #B29 + necklace #N29) => ambiguous
        ("B29gold", "coin bracelet #B29", "??? / GOLD", "iidB29a"),
        ("B29gold", "horseshoe necklace #N29", "??? / GOLD", "iidB29b"),
        # S/M bracelet whose CROSS MALL skucode equals the live sku (equality pass)
        ("B66goldS", "S&M bracelet #B66", "17.5cm / GOLD", "iidB66gS"),
        ("B66goldM", "S&M bracelet #B66", "19cm / GOLD", "iidB66gM"),
    ]
    return build_shopify_index(rows)


@pytest.mark.unit
def test_shopify_index_counts_and_empty_sku() -> None:
    idx = _shop()
    assert idx.total == 11
    assert idx.empty_sku == [("N90", "no-sku ring #N90")]
    assert idx.is_ambiguous("B29gold") is True  # 2 inventory items
    assert idx.has_unique_sku("B13gold") is True
    assert idx.unique_sku_iid("B13gold") == "iidB13g"


@pytest.mark.unit
def test_resolve_shopify_exact() -> None:
    idx = _shop()
    assert resolve_shopify(idx, "B13", "gold", "") == "B13gold"
    assert resolve_shopify(idx, "R09", "gold", "7") == "R09goldus7"


@pytest.mark.unit
def test_resolve_shopify_single_variant_fallback() -> None:
    # CROSS MALL has no color, Shopify tagged it gold -> still resolves (unique).
    idx = _shop()
    assert resolve_shopify(idx, "R05", "", "") == "R05"


@pytest.mark.unit
def test_resolve_shopify_single_variant_color_guard() -> None:
    # Only B17gold exists; a silver CROSS MALL variant must NOT reuse it.
    idx = _shop()
    assert resolve_shopify(idx, "B17", "gold", "") == "B17gold"
    assert resolve_shopify(idx, "B17", "silver", "") == ""


@pytest.mark.unit
def test_resolve_shopify_ambiguous_returns_empty() -> None:
    # B29gold maps to 2 inventory items -> never returned from the resolver.
    idx = _shop()
    assert resolve_shopify(idx, "B29", "gold", "") == ""
    assert resolve_shopify(idx, "N29", "gold", "") == ""


@pytest.mark.unit
def test_resolve_shopify_not_found() -> None:
    idx = _shop()
    assert resolve_shopify(idx, "R07", "gold", "7") == ""  # token absent
    assert resolve_shopify(idx, "N90", "gold", "") == ""  # only empty-sku variant


@pytest.mark.unit
def test_resolve_shop_target_direct_equality() -> None:
    # CROSS MALL skucode == a unique live Shopify sku: authoritative, even when
    # the size encodings (S vs cm) would defeat fuzzy matching.
    idx = _shop()
    sku, iid, reason = resolve_shop_target(idx, "B66", "gold", "S", "B66goldS")
    assert (sku, iid, reason) == ("B66goldS", "iidB66gS", "")


@pytest.mark.unit
def test_resolve_shop_target_ambiguous_reason() -> None:
    idx = _shop()
    sku, iid, reason = resolve_shop_target(idx, "B29", "gold", "", "B29gold")
    assert sku == "" and iid == ""
    assert "重複" in reason


# ---------- Rakuten index + resolver ----------


def _rk():  # type: ignore[no-untyped-def]
    # Real Rakuten variation values are English lowercase 'gold'/'silver',
    # and color sometimes sits in opt2 with size in opt1 (so scan both).
    rows = [
        {"manage": "030", "name": "bracelet #B13", "sku_mgmt": "", "opt1": "", "opt2": ""},
        {"manage": "030", "name": "", "sku_mgmt": "030-gold", "opt1": "gold", "opt2": ""},
        {"manage": "030", "name": "", "sku_mgmt": "030-silver", "opt1": "USA7号", "opt2": "silver"},
    ]
    return build_rakuten_index(rows)


@pytest.mark.unit
def test_rakuten_index_token_and_variants() -> None:
    idx = _rk()
    assert idx.token2manage["B13"] == "030"
    assert idx.manage_token["030"] == "B13"
    assert len(idx.var["030"]) == 2


@pytest.mark.unit
def test_resolve_rakuten_by_color() -> None:
    idx = _rk()
    assert resolve_rakuten(idx, "030", "gold", "") == "030-gold"
    assert resolve_rakuten(idx, "030", "silver", "") == "030-silver"
    assert resolve_rakuten(idx, None, "gold", "") == ""


# ---------- product_token ----------


@pytest.mark.unit
def test_product_token_prefers_rakuten_name() -> None:
    rk = build_rakuten_index(
        [{"manage": "00037c", "name": "bracelet #B13", "sku_mgmt": "", "opt1": "", "opt2": ""}]
    )
    assert product_token("00037c", {"00037c": "no token here"}, rk) == "B13"


@pytest.mark.unit
def test_product_token_code_fallback() -> None:
    # name carries no trailing token, but the 商品コード itself IS the 商品番号.
    rk = build_rakuten_index([])
    assert product_token("B34", {"B34": "316L plain necklace"}, rk) == "B34"
    assert product_token("P07", {"P07": "anklet"}, rk) == "P07"
    assert product_token("9999", {"9999": "ジュエリーBOX"}, rk) is None  # not a token


@pytest.mark.unit
def test_product_token_excludes_addons() -> None:
    # A length-extension add-on mentions #N19 mid-name but is not an N19 variant.
    rk = build_rakuten_index([])
    name = "s925 anchor necklace #N19 長さ変更用 ※一緒にご購入ください"
    assert product_token("T01", {"T01": name}, rk) is None


# ---------- end-to-end build_mapping ----------


def test_build_mapping_full_and_confirm() -> None:
    shop = _shop()
    rk = build_rakuten_index(
        [
            {"manage": "030c", "name": "bracelet #B13", "sku_mgmt": "", "opt1": "", "opt2": ""},
            {"manage": "030c", "name": "", "sku_mgmt": "030c-gold", "opt1": "gold", "opt2": ""},
            {"manage": "030c", "name": "", "sku_mgmt": "030c-silver", "opt1": "silver", "opt2": ""},
        ]
    )
    xm_name = {"00037c": "bracelet (no token in crossmall name)", "9999": "ジュエリーボックス"}
    xm_var = {
        "00037c": [
            {"sku": "00037cgold", "color": "gold", "size": ""},
            {"sku": "00037csilver", "color": "silver", "size": ""},
        ],
        "9999": [{"sku": "9999", "color": "", "size": ""}],  # no-token product
    }
    stock_map = {("00037c", "gold", ""): 7, ("00037c", "silver", ""): 3}

    rk.manage.add("00037c")
    rk.manage_token["00037c"] = "B13"
    rk.var["00037c"] = rk.var["030c"]

    mapping, confirm, stats = build_mapping(
        xm_name=xm_name, xm_var=xm_var, stock_map=stock_map, rk=rk, shop=shop
    )

    assert stats["full"] == 2
    assert stats["mapping_rows"] == 2
    assert stats["no_token_products"] == 1
    gold = next(r for r in mapping if r[2] == "gold")
    assert gold[4] == "B13gold"  # real looked-up Shopify sku
    assert gold[6] in ("00037c-gold", "030c-gold")
    assert gold[7] == 7  # qty
    assert gold[8] == "iidB13g"  # inventory_item_id threaded through
    assert any("トークン無し" in str(r[-1]) for r in confirm)


@pytest.mark.unit
def test_build_mapping_diverts_negative_stock() -> None:
    shop = _shop()
    rk = build_rakuten_index(
        [
            {"manage": "030", "name": "", "sku_mgmt": "030-gold", "opt1": "gold", "opt2": ""},
        ]
    )
    rk.manage.add("00037c")
    rk.manage_token["00037c"] = "B13"
    rk.var["00037c"] = rk.var["030"]
    xm_var = {"00037c": [{"sku": "00037cgold", "color": "gold", "size": ""}]}
    stock_map = {("00037c", "gold", ""): -5}  # negative on-hand

    _mapping, confirm, stats = build_mapping(
        xm_name={"00037c": "x"}, xm_var=xm_var, stock_map=stock_map, rk=rk, shop=shop
    )
    assert stats["full"] == 0  # negative stock is never sync-ready
    assert stats["negative_diverted"] == 1
    assert any("在庫マイナス" in str(r[-1]) for r in confirm)


@pytest.mark.unit
def test_build_mapping_routes_ambiguous_shopify_to_confirm() -> None:
    shop = _shop()
    rk = build_rakuten_index([])
    # token N29 resolves (via Shopify) to B29gold, which is ambiguous -> confirm.
    xm_name = {"056c": "necklace #N29"}
    xm_var = {"056c": [{"sku": "056cgold", "color": "gold", "size": ""}]}
    mapping, confirm, stats = build_mapping(
        xm_name=xm_name, xm_var=xm_var, stock_map={}, rk=rk, shop=shop
    )
    assert mapping == []
    assert stats["ambiguous_shopify"] == 1
    assert any("重複" in str(r[-1]) for r in confirm)


@pytest.mark.unit
def test_build_mapping_recovers_via_sku_equality() -> None:
    # CROSS MALL skucode B66goldS == a unique live Shopify sku, even though the
    # S-vs-cm sizes defeat fuzzy matching -> recovered as a full mapping.
    shop = _shop()
    rk = build_rakuten_index(
        [
            {"manage": "B66c", "name": "", "sku_mgmt": "B66c-goldS", "opt1": "gold", "opt2": "S"},
        ]
    )
    rk.manage.add("B66c")
    rk.manage_token["B66c"] = "B66"
    xm_name = {"B66c": "S&M bracelet #B66"}
    xm_var = {"B66c": [{"sku": "B66goldS", "color": "gold", "size": "S"}]}
    mapping, _confirm, stats = build_mapping(
        xm_name=xm_name, xm_var=xm_var, stock_map={("B66c", "gold", "S"): 4}, rk=rk, shop=shop
    )
    assert stats["full"] == 1
    row = mapping[0]
    assert row[4] == "B66goldS" and row[8] == "iidB66gS"


@pytest.mark.unit
def test_build_mapping_shopify_only_when_rakuten_missing() -> None:
    shop = _shop()
    rk = build_rakuten_index([])  # no Rakuten data at all
    xm_name = {"x": "ring #R05"}
    xm_var = {"x": [{"sku": "xg", "color": "", "size": ""}]}
    mapping, confirm, stats = build_mapping(
        xm_name=xm_name, xm_var=xm_var, stock_map={}, rk=rk, shop=shop
    )
    assert stats["full"] == 0
    assert stats["shopify_only"] == 1
    assert mapping == []
    assert any("楽天SKU未確定" in str(r[-1]) for r in confirm)


# ---------- scope layer (client decisions) ----------


@pytest.mark.unit
def test_scope_excludes_codes() -> None:
    shop = _shop()
    rk = build_rakuten_index([])
    xm_name = {"del": "ring #R05"}
    xm_var = {"del": [{"sku": "xg", "color": "", "size": ""}]}
    mapping, confirm, stats = build_mapping(
        xm_name=xm_name, xm_var=xm_var, stock_map={}, rk=rk, shop=shop, scope={"del": "exclude"}
    )
    assert stats["excluded"] == 1
    assert mapping == [] and confirm == []  # dropped from both


@pytest.mark.unit
def test_scope_shopify_only_is_complete_without_rakuten() -> None:
    # 楽天未販売: a Shopify-resolved row is a FULL mapping, not "楽天SKU未確定".
    shop = _shop()
    rk = build_rakuten_index([])  # no Rakuten
    xm_name = {"0012c": "ring #R05"}
    xm_var = {"0012c": [{"sku": "0012c", "color": "", "size": ""}]}
    mapping, _confirm, stats = build_mapping(
        xm_name=xm_name,
        xm_var=xm_var,
        stock_map={},
        rk=rk,
        shop=shop,
        scope={"0012c": "shopify_only"},
    )
    assert stats["full"] == 1
    assert mapping[0][4] == "R05" and mapping[0][6] == ""  # Shopify set, Rakuten blank


@pytest.mark.unit
def test_scope_bundle_is_set_aside() -> None:
    shop = _shop()
    rk = build_rakuten_index([])
    xm_name = {"0010c": "necklace #N21"}
    xm_var = {"0010c": [{"sku": "0010cgold", "color": "gold", "size": ""}]}
    mapping, confirm, stats = build_mapping(
        xm_name=xm_name, xm_var=xm_var, stock_map={}, rk=rk, shop=shop, scope={"0010c": "bundle"}
    )
    assert stats["bundle_set_aside"] == 1
    assert mapping == [] and confirm == []  # handled by the bundle feature, not here


@pytest.mark.unit
def test_bundle_set_aside_by_token_survives_recoding() -> None:
    # Client re-coded the N21 parent from 0010c to a new code 'N21'; a token-based
    # bundle set-aside catches it even without a scope entry for the new code.
    shop = _shop()
    rk = build_rakuten_index([])
    xm_name = {"N21": "necklace #N21"}
    xm_var = {"N21": [{"sku": "N21gold", "color": "gold", "size": ""}]}
    mapping, confirm, stats = build_mapping(
        xm_name=xm_name,
        xm_var=xm_var,
        stock_map={},
        rk=rk,
        shop=shop,
        scope={},  # no scope entry for the new code
        bundle_tokens={"N21"},
    )
    assert stats["bundle_set_aside"] == 1
    assert mapping == [] and confirm == []
