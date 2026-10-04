"""Tests for the Square provider — API calls through a stub http helper, no network."""

import json
import logging
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest
from marvin_integration_sdk import Handle, IntegrationContext, IntegrationError, Response, Retry, resolve_policy

from marvin_integration_square import SquareProvider
from marvin_integration_square.provider import (
    CODE_AUTH,
    CODE_CONFIG,
    CODE_CONFLICT,
    CODE_INVALID,
    CODE_NOT_FOUND,
    CODE_RATE_LIMITED,
    CODE_UNAVAILABLE,
    CODE_UNKNOWN,
    CODES,
    SQUARE_VERSION,
    SquareError,
)

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

    def _answer(self, method, url, body=None, headers=None, data=None):
        self.calls.append({"method": method, "url": url, "json": body, "headers": headers, "data": data})
        for route_method, needle, status, payload, *response_headers in self.routes:
            if route_method == method and needle in url:
                content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                return Response(status_code=status, content=content, headers=response_headers[0] if response_headers else {})
        return Response(status_code=599, content=b'{"errors":[{"detail":"no stub route"}]}')

    def get(self, url, *, headers=None, timeout=15):
        return self._answer("GET", url, headers=headers)

    def post(self, url, *, json=None, data=None, headers=None, timeout=15):
        return self._answer("POST", url, json, headers, data)

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


def _ctx(http=None, secret="tok", resume=None, seed=None, **config):
    cfg = {"environment": "sandbox", "location_id": LOCATION, "redirect_url": "https://art.example/sold/{slug}", **config}
    return IntegrationContext(config=cfg, secret=secret, logger=_LOG, http=http or _listing_http(), resume=resume, idempotency_seed=seed)


def _list(http, resume=None, seed=None, **args):
    base = {"slug": "blue-hour", "name": "Blue Hour", "price": "$1,170"}
    return SquareProvider().run_action("create_listing", {**base, **args}, _ctx(http, resume=resume, seed=seed))


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
        "image_id": None,
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


def test_create_listing_with_existing_ids_updates_item_and_replaces_link_new_one_first():
    http = _listing_http(upsert=UPDATED_ITEM)
    out = _list(http, variation_id="VAR0", payment_link_id="OLD", price=900)
    # The old link goes only once the new one exists, so the item is never left without a way to buy it.
    assert http.paths() == [
        ("GET", "/v2/catalog/object/VAR0?include_related_objects=true"),
        ("POST", "/v2/catalog/object"),
        ("POST", "/v2/inventory/changes/batch-create"),
        ("POST", "/v2/online-checkout/payment-links"),
        ("DELETE", "/v2/online-checkout/payment-links/OLD"),
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


def test_api_error_raises_value_error_naming_the_item_and_field():
    expected = r"Square couldn't save catalog item \"Blue Hour\" \(HTTP 400\): INVALID_VALUE: Invalid currency 'XYZ'\. \(field price_money\)"
    with pytest.raises(ValueError, match=expected):
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
    assert out == {"closed": True, "already": already, "stock_zeroed": False}
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


def test_provider_is_registered_with_its_actions():
    from marvin_integration_sdk import get_provider

    info = get_provider("square").info()
    assert info["category"] == "destination"
    assert {a["key"] for a in info["actions"]} == {"list_locations", "create_listing", "close_listing"}


def test_provider_declares_fields_webhook_and_workflows():
    from marvin_integration_sdk import get_provider

    content = get_provider("square").info()["content"]
    assert [(c["kind"], c["slug"]) for c in content] == [
        ("entry_fields", "square-shop-fields"),
        ("incoming_webhook", "square-events"),
        ("workflow", "square-list-for-sale"),
        ("workflow", "square-mark-sold"),
        ("workflow", "square-close-when-sold"),
        ("workflow", "square-close-when-withdrawn"),
        ("workflow", "square-close-when-unpublished"),
        ("workflow", "square-close-when-archived"),
    ]
    assert all(c["required"] for c in content)


def test_declared_workflows_only_call_actions_the_provider_has():
    from marvin_integration_square.content import CONTENT

    actions = {a.key for a in SquareProvider.actions}
    called = [
        step["action"]
        for blueprint in CONTENT
        if blueprint.kind == "workflow"
        for step in blueprint.payload["definition"]["actions"]
        if step["kind"] == "integration"
    ]
    assert called and set(called) <= actions


def test_mark_sold_workflow_listens_on_the_declared_webhook():
    from marvin_integration_square.content import CONTENT, WEBHOOK_SLUG

    by_slug = {b.slug: b for b in CONTENT}
    assert by_slug["square-mark-sold"].payload["definition"]["trigger"] == {"type": "incoming_webhook", "webhook": WEBHOOK_SLUG}
    assert "token" not in by_slug[WEBHOOK_SLUG].payload


def test_contributes_the_square_signature_scheme():
    from marvin_integration_square.content import CONTENT, WEBHOOK_SLUG

    scheme = SquareProvider.signature_schemes["square"]
    assert scheme["message"] == "{url}{body}" and scheme["encoding"] == "base64"
    assert {b.slug: b for b in CONTENT}[WEBHOOK_SLUG].payload["signature_scheme"] == "square"


# ---- picture --------------------------------------------------------------------------------

PICTURE_URL = "https://cms.example/assets/blue-hour.jpg"
JPEG = b"\xff\xd8\xff\xe0" + b"pixels"
IMAGE_UPLOADED = {"image": {"type": "IMAGE", "id": "IMG9", "image_data": {"url": "https://items-images.example/IMG9.jpg"}}}


def _picture_http(picture=JPEG, picture_status=200, upload_status=200, upsert=NEW_ITEM):
    http = _listing_http(upsert=upsert)
    http.routes[:0] = [
        ("GET", PICTURE_URL, picture_status, picture),
        ("POST", "/v2/catalog/images", upload_status, IMAGE_UPLOADED if upload_status == 200 else SQUARE_400),
    ]
    return http


def _upload(http):
    return next(c for c in http.calls if "/v2/catalog/images" in c["url"])


def test_create_listing_with_image_url_uploads_it_as_the_items_primary_image():
    http = _picture_http()
    out = _list(http, image_url=PICTURE_URL)
    body = _upload(http)["data"]
    request = json.loads(body.split(b'name="request"', 1)[1].split(b"\r\n\r\n", 1)[1].split(b"\r\n--", 1)[0])
    assert out["image_id"] == "IMG9"
    assert (request["object_id"], request["is_primary"], request["image"]["id"]) == ("ITEM1", True, "#image")
    assert b'name="file"; filename="image.jpg"\r\nContent-Type: image/jpeg\r\n\r\n' + JPEG in body


def test_create_listing_image_upload_is_multipart_with_matching_boundary():
    http = _picture_http()
    _list(http, image_url=PICTURE_URL)
    call = _upload(http)
    boundary = call["headers"]["Content-Type"].split("boundary=", 1)[1]
    assert call["headers"]["Content-Type"].startswith("multipart/form-data; ")
    assert call["data"].startswith(f"--{boundary}\r\n".encode()) and call["data"].endswith(f"--{boundary}--\r\n".encode())


def test_create_listing_keeps_an_existing_items_picture():
    http = _picture_http(upsert=UPDATED_ITEM)
    out = _list(http, variation_id="VAR0", image_url=PICTURE_URL)
    assert out["image_id"] is None
    assert not any(PICTURE_URL in c["url"] or "/v2/catalog/images" in c["url"] for c in http.calls)


@pytest.mark.parametrize(
    "http",
    [
        _picture_http(picture_status=404),
        _picture_http(picture=b"RIFF....WEBPVP8 "),
        _picture_http(upload_status=400),
    ],
    ids=["unreachable", "unsupported-type", "square-rejects"],
)
def test_create_listing_with_unusable_picture_still_lists(http):
    out = _list(http, image_url=PICTURE_URL)
    assert out["checkout_url"] == "https://square.link/u/abc" and out["image_id"] is None


@pytest.mark.parametrize("image_url", [None, "", "  "])
def test_create_listing_without_image_url_fetches_nothing(image_url):
    http = _picture_http()
    _list(http, image_url=image_url)
    assert not any("/v2/catalog/images" in c["url"] for c in http.calls)


def test_list_for_sale_workflow_passes_the_entrys_picture():
    from marvin_integration_square.content import CONTENT

    (workflow,) = [b for b in CONTENT if b.slug == "square-list-for-sale"]
    step = next(s for s in workflow.payload["definition"]["actions"] if s.get("action") == "create_listing")
    assert step["args"]["image_url"] == "${entry.image}"


@pytest.mark.parametrize(
    ("slug", "event"),
    [
        ("square-close-when-withdrawn", "entry_updated"),
        ("square-close-when-unpublished", "entry_unpublished"),
        ("square-close-when-archived", "entry_archived"),
    ],
)
def test_taking_an_item_out_of_the_shop_closes_its_link(slug, event):
    # A duplicate being archived, or Sell online switched off, must not leave a payable link behind.
    from marvin_integration_square.content import CONTENT

    (workflow,) = [b for b in CONTENT if b.slug == slug]
    definition = workflow.payload["definition"]
    assert definition["trigger"] == {"type": "event", "event": event}
    assert [a.get("action") for a in definition["actions"] if a["kind"] == "integration"] == ["close_listing"]
    marker = next(a for a in definition["actions"] if a.get("op") == "set_metadata")["metadata"]
    # Same marker as close-when-sold: the Buy button goes, and switching back on lists it again.
    assert marker == {"checkout_closed": True, "square_listed_for": "closed"}
    fields = {c["field"] for c in definition["conditions"]}
    assert {"entry.metadata.square_payment_link_id", "entry.metadata.square_link_closed"} <= fields


def test_switching_sell_online_off_is_what_withdraws_it():
    from marvin_integration_square.content import CLOSE_WHEN_WITHDRAWN

    conditions = CLOSE_WHEN_WITHDRAWN.payload["definition"]["conditions"]
    assert {"field": "entry.data.sellOnline", "op": "neq", "value": True} in conditions


# ---- failures carry a stable code -----------------------------------------------------------


def _square_error(category, code, detail="Refused.", field=None):
    error = {"category": category, "code": code, "detail": detail}
    return {"errors": [{**error, "field": field} if field else error]}


def _failing(method, needle, status, body):
    """The listing stubs, with one call answering `status` / `body` instead."""
    http = _listing_http()
    http.routes.insert(0, (method, needle, status, body))
    return http


def _list_fails_with(http, **args) -> SquareError:
    with pytest.raises(SquareError) as raised:
        _list(http, **args)
    return raised.value


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (401, _square_error("AUTHENTICATION_ERROR", "UNAUTHORIZED"), CODE_AUTH),
        (401, _square_error("AUTHENTICATION_ERROR", "ACCESS_TOKEN_EXPIRED"), CODE_AUTH),
        (403, _square_error("AUTHENTICATION_ERROR", "INSUFFICIENT_SCOPES"), CODE_AUTH),
        (403, {"errors": []}, CODE_AUTH),
        (400, _square_error("INVALID_REQUEST_ERROR", "INVALID_VALUE", field="price_money"), CODE_INVALID),
        (400, _square_error("INVALID_REQUEST_ERROR", "MISSING_REQUIRED_PARAMETER", field="object.item_data.name"), CODE_INVALID),
        (400, _square_error("INVALID_REQUEST_ERROR", "SOMETHING_NEW"), CODE_INVALID),
        (422, b"", CODE_INVALID),
        (404, _square_error("INVALID_REQUEST_ERROR", "NOT_FOUND"), CODE_NOT_FOUND),
        (429, _square_error("RATE_LIMIT_ERROR", "RATE_LIMITED"), CODE_RATE_LIMITED),
        (429, b"", CODE_RATE_LIMITED),
        (500, _square_error("API_ERROR", "INTERNAL_SERVER_ERROR"), CODE_UNAVAILABLE),
        (503, _square_error("API_ERROR", "SERVICE_UNAVAILABLE"), CODE_UNAVAILABLE),
        (502, b"<html>Bad Gateway</html>", CODE_UNAVAILABLE),
        (400, _square_error("INVALID_REQUEST_ERROR", "VERSION_MISMATCH"), CODE_CONFLICT),
        (400, _square_error("INVALID_REQUEST_ERROR", "IDEMPOTENCY_KEY_REUSED"), CODE_CONFLICT),
        (409, b"", CODE_CONFLICT),
        (402, _square_error("PAYMENT_METHOD_ERROR", "CARD_DECLINED"), CODE_UNKNOWN),
    ],
)
def test_square_http_failure_maps_to_its_stable_code(status, body, code):
    error = _list_fails_with(_failing("POST", "/v2/catalog/object", status, body))
    assert (error.code, f"(HTTP {status})" in str(error)) == (code, True)


def test_error_codes_are_the_documented_stable_strings():
    assert CODES == ("auth", "invalid", "not_found", "rate_limited", "unavailable", "conflict", "config", "unknown")


def test_square_error_is_still_a_value_error_for_the_engine():
    assert issubclass(SquareError, IntegrationError) and issubclass(SquareError, ValueError)
    error = SquareError("x", CODE_CONFLICT, partial={"a": 1}, retry_after=5)
    assert (error.code, error.partial, error.retry_after) == (CODE_CONFLICT, {"a": 1}, 5)


def test_auth_failure_says_to_check_the_token_and_environment():
    error = _list_fails_with(_failing("POST", "/v2/catalog/object", 401, _square_error("AUTHENTICATION_ERROR", "UNAUTHORIZED")))
    assert "Check the Square access token" in str(error)


def test_stock_failure_names_the_item_and_field():
    http = _failing("POST", "/v2/inventory/changes/batch-create", 400, _square_error("INVALID_REQUEST_ERROR", "INVALID_VALUE", field="quantity"))
    error = _list_fails_with(http)
    assert error.code == CODE_INVALID
    assert str(error).startswith('Square couldn\'t set the stock of "Blue Hour" to 1')
    assert str(error).endswith("INVALID_VALUE: Refused. (field quantity)")


def test_payment_link_failure_names_the_item():
    error = _list_fails_with(_failing("POST", "/v2/online-checkout/payment-links", 500, _square_error("API_ERROR", "INTERNAL_SERVER_ERROR")))
    assert (error.code, str(error).startswith('Square couldn\'t create the checkout link for "Blue Hour"')) == (CODE_UNAVAILABLE, True)


def test_replacing_the_old_link_failing_names_the_link():
    error = _list_fails_with(_failing("DELETE", "/v2/online-checkout/payment-links/", 409, b""), payment_link_id="OLD")
    assert (error.code, "delete payment link OLD" in str(error)) == (CODE_CONFLICT, True)


def test_unexpected_square_response_is_unknown():
    error = _list_fails_with(_failing("POST", "/v2/catalog/object", 200, {"catalog_object": {}}))
    assert (error.code, "returned no item/variation id" in str(error)) == (CODE_UNKNOWN, True)


@pytest.mark.parametrize(("price", "fee"), [("abc", None), (0, None), ("$1,170", "lots")])
def test_unusable_price_or_shipping_is_invalid_and_names_the_item(price, fee):
    http = _listing_http()
    error = _list_fails_with(http, price=price, shipping_fee=fee)
    assert (error.code, str(error).startswith('"Blue Hour" can\'t be listed'), http.calls) == (CODE_INVALID, True, [])


def test_missing_price_is_invalid():
    with pytest.raises(SquareError, match="has no price") as raised:
        SquareProvider().run_action("create_listing", {"slug": "a", "name": "A"}, _ctx())
    assert raised.value.code == CODE_INVALID


@pytest.mark.parametrize(
    ("ctx_kwargs", "match"),
    [({"secret": None}, "access token"), ({"location_id": ""}, "location_id"), ({"environment": "staging"}, "environment")],
)
def test_connection_missing_something_is_config(ctx_kwargs, match):
    with pytest.raises(SquareError, match=match) as raised:
        SquareProvider().run_action("create_listing", {"slug": "a", "name": "A", "price": 5}, _ctx(**ctx_kwargs))
    assert raised.value.code == CODE_CONFIG


class _Unreachable(_StubHttp):
    def post(self, url, **kwargs):
        raise TimeoutError("timed out")


def test_a_network_error_fails_the_step_as_unavailable():
    error = _list_fails_with(_Unreachable(_listing_http().routes))
    assert (error.code, "couldn't reach Square (TimeoutError: timed out)" in str(error)) == (CODE_UNAVAILABLE, True)


class _Broken(_StubHttp):
    def post(self, url, **kwargs):
        raise RuntimeError("boom")


def test_any_other_exception_fails_the_step_as_a_coded_value_error():
    error = _list_fails_with(_Broken(_listing_http().routes))
    assert (error.code, "Square create_listing failed: RuntimeError: boom" in str(error)) == (CODE_UNKNOWN, True)


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [(500, b"", CODE_UNAVAILABLE), (401, _square_error("AUTHENTICATION_ERROR", "ACCESS_TOKEN_REVOKED"), CODE_AUTH)],
)
def test_close_listing_failure_is_coded_and_names_the_link(status, body, code):
    http = _StubHttp([("DELETE", "/v2/online-checkout/payment-links/", status, body)])
    with pytest.raises(SquareError, match="delete payment link PL1") as raised:
        SquareProvider().run_action("close_listing", {"payment_link_id": "PL1"}, _ctx(http))
    assert raised.value.code == code


def test_close_listing_without_id_is_invalid():
    with pytest.raises(SquareError) as raised:
        SquareProvider().run_action("close_listing", {}, _ctx())
    assert raised.value.code == CODE_INVALID


def test_list_locations_failure_is_coded():
    http = _StubHttp([("GET", "/v2/locations", 429, _square_error("RATE_LIMIT_ERROR", "RATE_LIMITED"))])
    with pytest.raises(SquareError, match="list locations") as raised:
        SquareProvider().run_action("list_locations", {}, _ctx(http))
    assert raised.value.code == CODE_RATE_LIMITED


# ---- error policy ---------------------------------------------------------------------------

_REVIEW = Handle(review=True)
_RETRY_LATER = Handle(retry=Retry(backoff=(300,)), then=Handle(review=True, notify=True))


def _when_reconnected(attempts):
    return Handle(notify=True, retry=Retry(backoff=(), on_recovery=True, max_attempts=attempts), then=_REVIEW)


@pytest.mark.parametrize(
    ("action", "code", "handle"),
    [
        ("create_listing", CODE_AUTH, _when_reconnected(1)),
        ("create_listing", CODE_CONFIG, _when_reconnected(1)),
        ("close_listing", CODE_AUTH, _when_reconnected(10)),
        ("close_listing", CODE_CONFIG, _when_reconnected(10)),
        ("create_listing", CODE_RATE_LIMITED, Handle(retry=Retry(backoff=(60, 300, 900, 3600)), then=Handle(notify=True))),
        ("close_listing", CODE_RATE_LIMITED, Handle(retry=Retry(backoff=(60, 300, 900, 3600)), then=Handle(notify=True))),
        ("create_listing", CODE_UNAVAILABLE, Handle(retry=Retry(backoff=(120, 600, 1800, 7200, 21600)), then=Handle(notify=True, review=True))),
        ("close_listing", CODE_UNAVAILABLE, Handle(retry=Retry(backoff=(120, 600, 1800, 7200, 21600)), then=Handle(notify=True, review=True))),
        ("create_listing", CODE_CONFLICT, Handle(retry=Retry(backoff=(30, 120)), then=_REVIEW)),
        ("close_listing", CODE_CONFLICT, Handle(retry=Retry(backoff=(30, 120)), then=_REVIEW)),
        ("create_listing", CODE_INVALID, _REVIEW),
        ("close_listing", CODE_INVALID, _REVIEW),
        ("create_listing", CODE_NOT_FOUND, _REVIEW),
        ("close_listing", CODE_NOT_FOUND, Handle(succeed=True)),
        ("create_listing", CODE_UNKNOWN, _RETRY_LATER),
        ("close_listing", CODE_UNKNOWN, _RETRY_LATER),
        ("create_listing", "something_new", _RETRY_LATER),
        ("close_listing", "something_new", _RETRY_LATER),
    ],
)
def test_error_policy_resolves_per_action_and_code(action, code, handle):
    assert resolve_policy(SquareProvider, action, code) == handle


@pytest.mark.parametrize("code", [*CODES, "something_new"])
def test_list_locations_failures_plainly_fail(code):
    # Every code is listed on the action: the provider's entry for a code would otherwise outrank an action "*".
    assert resolve_policy(SquareProvider, "list_locations", code) == Handle()


def test_error_policy_is_registered_and_shown_in_the_catalog():
    from marvin_integration_sdk import get_provider

    policy = get_provider("square").info()["error_policy"]
    assert set(policy["provider"]) == {*CODES, "*"}
    assert set(policy["actions"]["close_listing"]) == {CODE_AUTH, CODE_CONFIG, CODE_NOT_FOUND}
    assert set(policy["actions"]["create_listing"]) == {CODE_AUTH, CODE_CONFIG}
    assert policy["actions"]["close_listing"][CODE_NOT_FOUND]["summary"] == "treat as success"
    assert policy["provider"][CODE_UNAVAILABLE]["summary"] == "retry 5× (2m, 10m, 30m, 2h, 6h), then notify admins, send to review"


def _rate_limited(headers):
    return _StubHttp([("GET", "/v2/locations", 429, _square_error("RATE_LIMIT_ERROR", "RATE_LIMITED"), headers)])


def _locations_error(http) -> SquareError:
    with pytest.raises(SquareError) as raised:
        SquareProvider().run_action("list_locations", {}, _ctx(http))
    return raised.value


@pytest.mark.parametrize(
    ("headers", "expected"), [({"Retry-After": "120"}, 120.0), ({"retry-after": "7.5"}, 7.5), ({}, None), ({"Retry-After": "soon"}, None)]
)
def test_rate_limited_carries_square_retry_after(headers, expected):
    error = _locations_error(_rate_limited(headers))
    assert (error.code, error.retry_after) == (CODE_RATE_LIMITED, expected)


def test_retry_after_as_an_http_date():
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=600), usegmt=True)
    assert 590 <= _locations_error(_rate_limited({"Retry-After": when})).retry_after <= 600


def test_retry_after_is_only_read_on_rate_limiting():
    http = _StubHttp([("GET", "/v2/locations", 503, b"", {"Retry-After": "120"})])
    assert _locations_error(http).retry_after is None


# ---- closing safely: stock first, then the link ----------------------------------------------


def _close(http, resume=None, **args):
    return SquareProvider().run_action("close_listing", {"payment_link_id": "PL1", "variation_id": "VAR1", **args}, _ctx(http, resume=resume))


def _close_fails_with(http, resume=None) -> SquareError:
    with pytest.raises(SquareError) as raised:
        _close(http, resume)
    return raised.value


def test_close_listing_zeroes_stock_before_deleting_the_link():
    http = _listing_http()
    out = _close(http)
    assert http.paths() == [("POST", "/v2/inventory/changes/batch-create"), ("DELETE", "/v2/online-checkout/payment-links/PL1")]
    (change,) = http.body("POST", "/inventory/changes/batch-create")["changes"]
    count = change["physical_count"]
    assert (change["type"], count["catalog_object_id"], count["state"], count["location_id"], count["quantity"]) == (
        "PHYSICAL_COUNT",
        "VAR1",
        "IN_STOCK",
        LOCATION,
        "0",
    )
    assert out == {"closed": True, "already": False, "stock_zeroed": True}


def test_close_listing_link_failure_after_zeroing_keeps_the_progress():
    error = _close_fails_with(_listing_http(delete_status=500))
    assert (error.code, error.partial) == (CODE_UNAVAILABLE, {"for": "PL1", "stock_zeroed": True})


def test_close_listing_resume_skips_the_zeroed_stock():
    http = _listing_http()
    out = _close(http, resume={"for": "PL1", "stock_zeroed": True})
    assert http.paths() == [("DELETE", "/v2/online-checkout/payment-links/PL1")]
    assert out["stock_zeroed"] is True


def test_close_listing_ignores_progress_saved_for_another_link():
    http = _listing_http()
    _close(http, resume={"for": "PL0", "stock_zeroed": True})
    assert http.paths()[0] == ("POST", "/v2/inventory/changes/batch-create")


def test_close_listing_link_already_gone_after_zeroing_is_success():
    out = _close(_listing_http(delete_status=404))
    assert out == {"closed": True, "already": True, "stock_zeroed": True}


def test_close_listing_with_the_variation_gone_still_deletes_the_link():
    http = _failing("POST", "/v2/inventory/changes/batch-create", 404, _square_error("INVALID_REQUEST_ERROR", "NOT_FOUND"))
    assert _close(http)["closed"] is True
    assert http.paths()[-1] == ("DELETE", "/v2/online-checkout/payment-links/PL1")


def test_close_listing_stock_failure_leaves_the_link_and_no_progress():
    http = _failing("POST", "/v2/inventory/changes/batch-create", 503, b"")
    error = _close_fails_with(http)
    assert (error.code, error.partial, "set the stock of variation VAR1 to 0" in str(error)) == (CODE_UNAVAILABLE, None, True)
    assert not any(c["method"] == "DELETE" for c in http.calls)


# ---- create_listing: partial progress and resume ---------------------------------------------

LINK_PATHS = [("POST", "/v2/online-checkout/payment-links"), ("DELETE", "/v2/online-checkout/payment-links/OLD")]


def test_create_listing_link_failure_keeps_the_item_and_never_deletes_the_old_link():
    http = _failing("POST", "/v2/online-checkout/payment-links", 503, b"")
    error = _list_fails_with(http, payment_link_id="OLD")
    assert error.code == CODE_UNAVAILABLE
    assert {k: error.partial[k] for k in ("item_id", "variation_id", "saved", "stock_set")} == {
        "item_id": "ITEM1",
        "variation_id": "VAR1",
        "saved": True,
        "stock_set": True,
    }
    assert "payment_link_id" not in error.partial
    assert not any(c["method"] == "DELETE" for c in http.calls)


def test_create_listing_upsert_failure_has_no_progress():
    error = _list_fails_with(_listing_http(upsert_status=400))
    assert error.partial is None


def test_create_listing_resume_after_stock_failure_reuses_the_item():
    first = _list_fails_with(_failing("POST", "/v2/inventory/changes/batch-create", 503, b""), payment_link_id="OLD")
    http = _listing_http()
    out = _list(http, resume=first.partial, payment_link_id="OLD")
    assert http.paths() == [("POST", "/v2/inventory/changes/batch-create"), *LINK_PATHS]  # no second catalog item
    assert (out["item_id"], out["variation_id"], out["payment_link_id"]) == ("ITEM1", "VAR1", "PL1")


def test_create_listing_resume_that_only_needs_the_old_link_deleted_does_just_that():
    first = _list_fails_with(_failing("DELETE", "/v2/online-checkout/payment-links/", 503, b""), payment_link_id="OLD")
    assert (first.partial["payment_link_id"], first.partial["checkout_url"]) == ("PL1", "https://square.link/u/abc")
    http = _listing_http()
    out = _list(http, resume=first.partial, payment_link_id="OLD")
    assert http.paths() == [("DELETE", "/v2/online-checkout/payment-links/OLD")]
    assert out == {
        "item_id": "ITEM1",
        "variation_id": "VAR1",
        "payment_link_id": "PL1",
        "checkout_url": "https://square.link/u/abc",
        "order_id": "ORD1",
        "image_id": None,
    }


def test_create_listing_resume_after_a_price_change_updates_the_same_item_and_retires_its_link():
    first = _list_fails_with(_failing("DELETE", "/v2/online-checkout/payment-links/", 503, b""), payment_link_id="OLD")
    http = _listing_http(upsert=UPDATED_ITEM)
    http.routes.insert(0, ("POST", "/v2/online-checkout/payment-links", 200, {"payment_link": {"id": "PL2", "url": "https://square.link/u/new"}}))
    first.partial["variation_id"] = "VAR0"  # the stubbed catalog answers for VAR0
    out = _list(http, resume=first.partial, payment_link_id="OLD", price=900)
    assert http.body("POST", "/v2/catalog/object")["object"]["id"] == "ITEM0"  # updated in place, not a new "#item"
    deleted = [path for method, path in http.paths() if method == "DELETE"]
    assert deleted == ["/v2/online-checkout/payment-links/OLD", "/v2/online-checkout/payment-links/PL1"]  # PL1 sold at the old price
    assert out["payment_link_id"] == "PL2"


def test_create_listing_retiring_a_stale_link_that_fails_keeps_it_for_the_next_try():
    stale = {"for": "older-input", "item_id": "ITEM0", "variation_id": "VAR0", "payment_link_id": "STALE"}
    http = _listing_http(upsert=UPDATED_ITEM)
    http.routes.insert(0, ("DELETE", "/payment-links/STALE", 503, b""))
    with pytest.raises(SquareError) as raised:
        _list(http, resume=stale)
    assert raised.value.partial["retire_link_ids"] == ["STALE"]
    assert raised.value.partial["payment_link_id"] == "PL1"


class _UnreachableStock(_StubHttp):
    def post(self, url, **kwargs):
        if "/inventory/" in url:
            raise TimeoutError("timed out")
        return super().post(url, **kwargs)


def test_create_listing_network_error_midway_keeps_the_progress():
    error = _list_fails_with(_UnreachableStock(_listing_http().routes))
    assert (error.code, error.partial["item_id"], error.partial.get("stock_set")) == (CODE_UNAVAILABLE, "ITEM1", None)


# ---- idempotency keys -----------------------------------------------------------------------


def _keys(http):
    return {c["url"].split(".com", 1)[1]: (c["json"] or {}).get("idempotency_key") for c in http.calls if c["method"] == "POST" and c["json"]}


def test_idempotency_keys_repeat_within_one_retry_chain():
    first, second = _listing_http(), _listing_http()
    _list(first, seed="chain-1")
    _list(second, seed="chain-1")
    for path in ("/v2/catalog/object", "/v2/online-checkout/payment-links"):
        assert _keys(first)[path] == _keys(second)[path]


def test_idempotency_keys_differ_between_chains_and_after_a_price_change():
    base, other_chain, new_price = _listing_http(), _listing_http(), _listing_http()
    _list(base, seed="chain-1")
    _list(other_chain, seed="chain-2")
    _list(new_price, seed="chain-1", price=900)
    for path in ("/v2/catalog/object", "/v2/online-checkout/payment-links"):
        assert len({_keys(base)[path], _keys(other_chain)[path], _keys(new_price)[path]}) == 3


def _upload_key(http):
    request = _upload(http)["data"].split(b'name="request"', 1)[1].split(b"\r\n\r\n", 1)[1].split(b"\r\n--", 1)[0]
    return json.loads(request)["idempotency_key"]


def test_image_upload_key_repeats_within_one_retry_chain():
    first, second = _picture_http(), _picture_http()
    _list(first, seed="chain-1", image_url=PICTURE_URL)
    _list(second, seed="chain-1", image_url=PICTURE_URL)
    assert _upload_key(first) == _upload_key(second)


# ---- workflows: close order and the square_link_closed marker --------------------------------

CLOSE_SLUGS = ("square-close-when-sold", "square-close-when-withdrawn", "square-close-when-unpublished", "square-close-when-archived")


def _workflow(slug):
    from marvin_integration_square.content import CONTENT

    return next(b for b in CONTENT if b.slug == slug).payload["definition"]


@pytest.mark.parametrize("slug", CLOSE_SLUGS)
def test_close_workflows_hide_the_buy_button_before_calling_square(slug):
    actions = _workflow(slug)["actions"]
    assert [(a["kind"], a.get("op") or a.get("task") or a.get("action")) for a in actions] == [
        ("entry", "set_metadata"),
        ("handler", "request_site_rebuild"),
        ("integration", "close_listing"),
        ("entry", "set_metadata"),
    ]
    assert actions[0]["metadata"] == {"checkout_closed": True, "square_listed_for": "closed"}
    assert actions[2]["args"] == {
        "payment_link_id": "${entry.metadata.square_payment_link_id}",
        "variation_id": "${entry.metadata.square_variation_id}",
    }
    assert actions[3]["metadata"] == {"square_link_closed": True}


@pytest.mark.parametrize("slug", CLOSE_SLUGS)
def test_close_workflows_key_on_square_link_closed_so_a_retry_still_runs(slug):
    conditions = _workflow(slug)["conditions"]
    assert {"field": "entry.metadata.square_link_closed", "op": "neq", "value": True} in conditions
    # checkout_closed is already true when a retry re-checks the conditions; keying on it would drop the retry.
    assert not any(c["field"] == "entry.metadata.checkout_closed" for c in conditions)


@pytest.mark.parametrize(
    ("slug", "condition"),
    [
        ("square-close-when-unpublished", {"field": "entry.status", "op": "neq", "value": "published"}),
        ("square-close-when-archived", {"field": "entry.status", "op": "eq", "value": "archived"}),
    ],
)
def test_a_pending_close_retry_is_dropped_once_the_item_is_back(slug, condition):
    # A republished item gets a new link; a retry of the old close must not then pass and close it.
    assert condition in _workflow(slug)["conditions"]


def test_listing_resets_square_link_closed_so_the_next_sale_closes_the_new_link():
    marker = next(a for a in _workflow("square-list-for-sale")["actions"] if a.get("op") == "set_metadata")["metadata"]
    assert (marker["checkout_closed"], marker["square_link_closed"]) == (False, False)


def test_mark_sold_ignores_stock_marvin_zeroed_while_closing():
    definition = _workflow("square-mark-sold")
    assert definition["target"]["query"]["metadata"] == {"square_variation_id": "${event.payload.data.object.inventory_counts.0.catalog_object_id}"}
    assert {"field": "entry.metadata.checkout_closed", "op": "neq", "value": True} in definition["conditions"]
    assert definition["actions"] == [{"kind": "entry", "op": "set_data", "data": {"status": "sold"}}]
