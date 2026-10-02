"""Tests for the Square provider — API calls through a stub http helper, no network."""

import json
import logging

import pytest
from marvin_integration_sdk import IntegrationContext, Response

from marvin_integration_square import SquareProvider
from marvin_integration_square.provider import SQUARE_VERSION

_LOG = logging.getLogger("test")
SANDBOX = "https://connect.squareupsandbox.com"
PRODUCTION = "https://connect.squareup.com"
LOCATION = "LOC1"

LOCATIONS = {
    "locations": [{"id": LOCATION, "name": "Studio", "status": "ACTIVE", "currency": "USD"}, {"id": "LOC2", "name": "Fair", "status": "INACTIVE"}]
}
NEW_ITEM = {
    "catalog_object": {
        "type": "ITEM",
        "id": "ITEM1",
        "version": 1,
        "item_data": {
            "name": "Blue Hour",
            "variations": [{"type": "ITEM_VARIATION", "id": "VAR1", "version": 1, "item_variation_data": {"item_id": "ITEM1"}}],
        },
    },
    "id_mappings": [{"client_object_id": "#item", "object_id": "ITEM1"}, {"client_object_id": "#variation", "object_id": "VAR1"}],
}
EXISTING_ITEM = {
    "type": "ITEM",
    "id": "ITEM0",
    "version": 1700,
    "item_data": {
        "name": "Old name",
        "image_ids": ["IMG1"],
        "description_html": "<p>old</p>",
        "variations": [
            {
                "type": "ITEM_VARIATION",
                "id": "VAR0",
                "version": 1701,
                "item_variation_data": {
                    "item_id": "ITEM0",
                    "name": "Original",
                    "pricing_type": "FIXED_PRICING",
                    "price_money": {"amount": 1, "currency": "USD"},
                },
            }
        ],
    },
}
RETRIEVED_VARIATION = {"object": EXISTING_ITEM["item_data"]["variations"][0], "related_objects": [EXISTING_ITEM]}
UPDATED_ITEM = {"catalog_object": {**EXISTING_ITEM, "version": 1800}}
INVENTORY = {"counts": [{"catalog_object_id": "VAR1", "state": "IN_STOCK", "location_id": LOCATION, "quantity": "1"}]}
LINK = {
    "payment_link": {"id": "PL1", "version": 1, "url": "https://square.link/u/abc", "long_url": "https://checkout.square.site/x", "order_id": "ORD1"}
}
SQUARE_400 = {"errors": [{"category": "INVALID_REQUEST_ERROR", "code": "INVALID_VALUE", "detail": "Invalid currency 'XYZ'.", "field": "price_money"}]}


class _StubHttp:
    """Answers by (method, URL substring) in route order and records every call."""

    def __init__(self, routes=None):
        self.routes = routes or []
        self.calls: list[dict] = []

    def _answer(self, method, url, body=None, headers=None):
        self.calls.append({"method": method, "url": url, "json": body, "headers": headers})
        for route_method, needle, status, payload in self.routes:
            if route_method == method and needle in url:
                return Response(status_code=status, content=json.dumps(payload).encode())
        return Response(status_code=599, content=b'{"errors":[{"detail":"no stub route"}]}')

    def get(self, url, *, headers=None, timeout=15):
        return self._answer("GET", url, headers=headers)

    def post(self, url, *, json=None, data=None, headers=None, timeout=15):
        return self._answer("POST", url, json, headers)

    def put(self, url, *, json=None, data=None, headers=None, timeout=15):
        return self._answer("PUT", url, json, headers)

    def delete(self, url, *, headers=None, timeout=15):
        return self._answer("DELETE", url, headers=headers)

    def paths(self):
        return [(c["method"], c["url"].split(".com", 1)[1]) for c in self.calls]

    def body(self, method, needle):
        return next(c["json"] for c in self.calls if c["method"] == method and needle in c["url"])


def _listing_http(upsert=NEW_ITEM, delete_status=200, upsert_status=200, retrieve_status=200):
    return _StubHttp(
        [
            ("GET", "/v2/locations", 200, LOCATIONS),
            ("GET", "/v2/catalog/object/", retrieve_status, RETRIEVED_VARIATION),
            ("POST", "/v2/catalog/object", upsert_status, upsert if upsert_status == 200 else SQUARE_400),
            ("POST", "/v2/inventory/changes/batch-create", 200, INVENTORY),
            ("DELETE", "/v2/online-checkout/payment-links/", delete_status, {"id": "OLD", "cancelled_order_id": "ORD0"}),
            ("POST", "/v2/online-checkout/payment-links", 200, LINK),
        ]
    )


def _ctx(http=None, secret="tok", **config):
    cfg = {"environment": "sandbox", "location_id": LOCATION, "redirect_url": "https://art.example/sold/{slug}", **config}
    return IntegrationContext(config=cfg, secret=secret, logger=_LOG, http=http or _listing_http())


def _list(http, **args):
    base = {"slug": "blue-hour", "name": "Blue Hour", "price": "$1,170"}
    return SquareProvider().run_action("create_listing", {**base, **args}, _ctx(http))


# ---- create_listing -------------------------------------------------------------------------


def test_create_listing_new_calls_catalog_then_inventory_then_payment_link():
    http = _listing_http()
    _list(http)
    assert http.paths() == [
        ("POST", "/v2/catalog/object"),
        ("POST", "/v2/inventory/changes/batch-create"),
        ("POST", "/v2/online-checkout/payment-links"),
    ]


def test_create_listing_new_upserts_tracked_item_with_one_priced_variation():
    http = _listing_http()
    _list(http, description="Oil on linen")
    obj = http.body("POST", "/v2/catalog/object")["object"]
    assert obj["id"] == "#item" and obj["type"] == "ITEM" and "version" not in obj
    assert obj["item_data"]["description"] == "Oil on linen"
    (variation,) = obj["item_data"]["variations"]
    assert variation["item_variation_data"] == {
        "item_id": "#item",
        "name": "Original",
        "pricing_type": "FIXED_PRICING",
        "price_money": {"amount": 117000, "currency": "USD"},
        "track_inventory": True,
    }


def test_create_listing_sets_stock_to_exactly_one_at_the_location():
    http = _listing_http()
    _list(http)
    (change,) = http.body("POST", "/inventory/changes/batch-create")["changes"]
    assert change["type"] == "PHYSICAL_COUNT"
    count = change["physical_count"]
    assert {k: count[k] for k in ("catalog_object_id", "state", "location_id", "quantity")} == {
        "catalog_object_id": "VAR1",
        "state": "IN_STOCK",
        "location_id": LOCATION,
        "quantity": "1",
    }
    assert count["occurred_at"].endswith("Z")


def test_create_listing_payment_link_references_the_catalog_variation():
    http = _listing_http()
    _list(http)
    body = http.body("POST", "/online-checkout/payment-links")
    assert "quick_pay" not in body
    assert body["order"] == {"location_id": LOCATION, "line_items": [{"catalog_object_id": "VAR1", "quantity": "1"}]}
    assert body["checkout_options"] == {"ask_for_shipping_address": True, "redirect_url": "https://art.example/sold/blue-hour"}
    assert body["payment_note"] == "blue-hour"


def test_create_listing_returns_all_ids_and_checkout_url():
    out = _list(_listing_http())
    assert out == {
        "item_id": "ITEM1",
        "variation_id": "VAR1",
        "payment_link_id": "PL1",
        "checkout_url": "https://square.link/u/abc",
        "order_id": "ORD1",
    }


def test_create_listing_idempotency_keys_are_unique_per_call():
    http = _listing_http()
    _list(http)
    _list(http)
    keys = [c["json"]["idempotency_key"] for c in http.calls if c["method"] == "POST"]
    assert len(keys) == 6 and len(set(keys)) == 6


def test_create_listing_with_shipping_fee_adds_it_to_checkout_options():
    http = _listing_http()
    _list(http, shipping_fee="$45")
    fee = http.body("POST", "/online-checkout/payment-links")["checkout_options"]["shipping_fee"]
    assert fee == {"name": "Shipping", "charge": {"amount": 4500, "currency": "USD"}}


@pytest.mark.parametrize("fee", [None, "", 0, "0"])
def test_create_listing_without_shipping_fee_has_none(fee):
    http = _listing_http()
    _list(http, shipping_fee=fee)
    assert "shipping_fee" not in http.body("POST", "/online-checkout/payment-links")["checkout_options"]


def test_create_listing_with_existing_ids_updates_item_and_replaces_link():
    http = _listing_http(upsert=UPDATED_ITEM)
    out = _list(http, variation_id="VAR0", payment_link_id="OLD", price=900)
    assert http.paths() == [
        ("GET", "/v2/catalog/object/VAR0?include_related_objects=true"),
        ("POST", "/v2/catalog/object"),
        ("POST", "/v2/inventory/changes/batch-create"),
        ("DELETE", "/v2/online-checkout/payment-links/OLD"),
        ("POST", "/v2/online-checkout/payment-links"),
    ]
    assert out["item_id"] == "ITEM0" and out["variation_id"] == "VAR0"


def test_create_listing_with_existing_variation_upserts_fetched_versions_without_duplicating():
    http = _listing_http(upsert=UPDATED_ITEM)
    _list(http, variation_id="VAR0", price=900)
    obj = http.body("POST", "/v2/catalog/object")["object"]
    assert (obj["id"], obj["version"]) == ("ITEM0", 1700)
    assert obj["item_data"]["name"] == "Blue Hour"
    assert obj["item_data"]["image_ids"] == ["IMG1"]  # full-replacement upsert must not drop dashboard edits
    (variation,) = obj["item_data"]["variations"]
    assert (variation["id"], variation["version"]) == ("VAR0", 1701)
    assert variation["item_variation_data"]["price_money"] == {"amount": 90000, "currency": "USD"}
    assert variation["item_variation_data"]["track_inventory"] is True


def test_create_listing_with_vanished_variation_creates_a_new_item():
    http = _listing_http(retrieve_status=404)
    out = _list(http, variation_id="GONE")
    assert http.body("POST", "/v2/catalog/object")["object"]["id"] == "#item"
    assert out["variation_id"] == "VAR1"


def test_create_listing_ignores_404_when_deleting_old_link():
    out = _list(_listing_http(delete_status=404), payment_link_id="OLD")
    assert out["payment_link_id"] == "PL1"


def test_create_listing_without_location_raises():
    with pytest.raises(ValueError, match="location_id"):
        SquareProvider().run_action("create_listing", {"slug": "a", "name": "A", "price": 5}, _ctx(location_id=""))


@pytest.mark.parametrize(
    "args", [{"name": "A", "price": 5}, {"slug": "a", "price": 5}, {"slug": "a", "name": "A"}, {"slug": "a", "name": "A", "price": "abc"}]
)
def test_create_listing_with_bad_input_raises_before_any_call(args):
    http = _listing_http()
    with pytest.raises(ValueError):
        SquareProvider().run_action("create_listing", args, _ctx(http))
    assert http.calls == []


def test_api_error_raises_value_error_with_square_detail():
    with pytest.raises(ValueError, match=r"HTTP 400: INVALID_VALUE: Invalid currency 'XYZ'\. \(field price_money\)"):
        _list(_listing_http(upsert_status=400))


# ---- environment and headers ----------------------------------------------------------------


@pytest.mark.parametrize(("env", "base"), [("sandbox", SANDBOX), ("production", PRODUCTION), (None, SANDBOX)])
def test_environment_selects_base_url(env, base):
    http = _listing_http()
    SquareProvider().run_action("list_locations", {}, _ctx(http, environment=env))
    assert http.calls[0]["url"] == f"{base}/v2/locations"


def test_unknown_environment_raises():
    with pytest.raises(ValueError, match="environment"):
        SquareProvider().run_action("list_locations", {}, _ctx(environment="staging"))


def test_every_call_sends_auth_and_version_headers():
    http = _listing_http(upsert=UPDATED_ITEM)
    _list(http, variation_id="VAR0", payment_link_id="OLD")
    for call in http.calls:
        assert call["headers"]["Authorization"] == "Bearer tok"
        assert call["headers"]["Square-Version"] == SQUARE_VERSION


# ---- close_listing --------------------------------------------------------------------------


@pytest.mark.parametrize(("status", "already"), [(200, False), (404, True)])
def test_close_listing_deletes_link(status, already):
    http = _listing_http(delete_status=status)
    out = SquareProvider().run_action("close_listing", {"payment_link_id": "PL1"}, _ctx(http))
    assert out == {"closed": True, "already": already}
    assert http.paths() == [("DELETE", "/v2/online-checkout/payment-links/PL1")]


def test_close_listing_server_error_raises():
    with pytest.raises(ValueError, match="HTTP 500"):
        SquareProvider().run_action("close_listing", {"payment_link_id": "PL1"}, _ctx(_listing_http(delete_status=500)))


def test_close_listing_without_id_raises():
    with pytest.raises(ValueError, match="payment_link_id"):
        SquareProvider().run_action("close_listing", {}, _ctx())


# ---- list_locations, check, dispatch --------------------------------------------------------


def test_list_locations_returns_id_name_status():
    out = SquareProvider().run_action("list_locations", {}, _ctx())
    assert out == {"locations": [{"id": LOCATION, "name": "Studio", "status": "ACTIVE"}, {"id": "LOC2", "name": "Fair", "status": "INACTIVE"}]}


def test_check_with_known_location_is_ok():
    assert SquareProvider().check(_ctx()) == ("ok", None)


def test_check_without_location_configured_is_ok():
    assert SquareProvider().check(_ctx(location_id="")) == ("ok", None)


def test_check_with_unknown_location_names_the_problem():
    status, error = SquareProvider().check(_ctx(location_id="NOPE"))
    assert status == "error" and "NOPE" in error and "LOC1 (Studio)" in error


def test_check_with_rejected_token_is_error():
    http = _StubHttp([("GET", "/v2/locations", 401, {"errors": [{"code": "UNAUTHORIZED", "detail": "Bad token"}]})])
    status, error = SquareProvider().check(_ctx(http))
    assert status == "error" and "Bad token" in error


def test_check_without_token_is_unconfigured():
    assert SquareProvider().check(_ctx(secret=None))[0] == "unconfigured"


def test_missing_secret_and_unknown_action_raise():
    with pytest.raises(ValueError):
        SquareProvider().run_action("list_locations", {}, _ctx(secret=None))
    with pytest.raises(NotImplementedError):
        SquareProvider().run_action("nope", {}, _ctx())


def test_provider_declares_no_content_and_is_registered():
    from marvin_integration_sdk import get_provider

    info = get_provider("square").info()
    assert info["content"] == [] and info["category"] == "destination"
    assert {a["key"] for a in info["actions"]} == {"list_locations", "create_listing", "close_listing"}
