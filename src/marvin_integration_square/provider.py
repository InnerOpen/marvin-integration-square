"""Square provider: list a one-of-a-kind item in the catalog and sell it through a payment link.

A listing is three Square objects: a catalog ITEM with one ITEM_VARIATION (inventory tracked, so
an in-person sale on a card reader decrements the same stock), an inventory count of exactly 1 at
the configured location, and a payment link whose order references that variation by
``catalog_object_id``. A quick_pay link would be an ad-hoc amount with no tie to inventory, so a
painting could sell twice.

The provider is pure with respect to Marvin: it gets ``config``, the resolved ``secret`` (the
access token), a ``logger`` and a safe ``http`` client via ``ctx``, and returns dicts. Storing the
returned ids on the entry, and reacting to Square webhooks (including verifying their signatures),
is the core's job.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import ClassVar
from urllib.parse import quote

from marvin_integration_sdk import (
    CATEGORY_DESTINATION,
    CredentialField,
    ErrorPolicy,
    Handle,
    IntegrationContext,
    IntegrationError,
    IntegrationProvider,
    ProviderAction,
    Response,
    Retry,
    register_provider,
)

from .content import CONTENT
from .money import optional_fee_cents, price_cents

# Latest version in Square's API changelog when this was written (2026-10), and the version the
# reference examples for every endpoint used here are generated against. Pinned so a Square
# release can never change request/response shapes underneath a running workflow.
SQUARE_VERSION = "2026-09-16"
BASE_URLS = {
    "sandbox": "https://connect.squareupsandbox.com",
    "production": "https://connect.squareup.com",
}
DEFAULT_ENVIRONMENT = "sandbox"
DEFAULT_CURRENCY = "USD"

ONE_OF_A_KIND = "1"  # Square quantities are decimal strings
NONE_LEFT = "0"
VARIATION_NAME = "Original"
SHIPPING_FEE_NAME = "Shipping"
SLUG_PLACEHOLDER = "{slug}"
HTTP_NOT_FOUND = 404
HTTP_SERVER_ERROR = 500
ERROR_TEXT_LIMIT = 300

# The stable codes a failure carries (SquareError.code) — Square's error categories and codes folded
# to what someone does about them.
CODE_AUTH = "auth"  # the token is bad, expired, revoked, or lacks a permission: fix the connection
CODE_INVALID = "invalid"  # Square refused the request (a bad price, currency or missing field): fix the item
CODE_NOT_FOUND = "not_found"  # a Square object the item points at is gone
CODE_RATE_LIMITED = "rate_limited"  # too many calls: try again later
CODE_UNAVAILABLE = "unavailable"  # Square's side failed, or it couldn't be reached: try again later
CODE_CONFLICT = "conflict"  # the object changed in Square meanwhile, or an idempotency key clashed
CODE_CONFIG = "config"  # the connection is missing something before any call (token, location, environment)
CODE_UNKNOWN = "unknown"  # anything else, e.g. a response without the ids Square should return
CODES = (CODE_AUTH, CODE_INVALID, CODE_NOT_FOUND, CODE_RATE_LIMITED, CODE_UNAVAILABLE, CODE_CONFLICT, CODE_CONFIG, CODE_UNKNOWN)
CODE_BY_SQUARE_CODE = {
    "UNAUTHORIZED": CODE_AUTH,
    "ACCESS_TOKEN_EXPIRED": CODE_AUTH,
    "ACCESS_TOKEN_REVOKED": CODE_AUTH,
    "CLIENT_DISABLED": CODE_AUTH,
    "FORBIDDEN": CODE_AUTH,
    "INSUFFICIENT_SCOPES": CODE_AUTH,
    "NOT_FOUND": CODE_NOT_FOUND,
    "RATE_LIMITED": CODE_RATE_LIMITED,
    "VERSION_MISMATCH": CODE_CONFLICT,
    "IDEMPOTENCY_KEY_REUSED": CODE_CONFLICT,
    "CONFLICT": CODE_CONFLICT,
    "INTERNAL_SERVER_ERROR": CODE_UNAVAILABLE,
    "BAD_GATEWAY": CODE_UNAVAILABLE,
    "SERVICE_UNAVAILABLE": CODE_UNAVAILABLE,
    "GATEWAY_TIMEOUT": CODE_UNAVAILABLE,
}
CODE_BY_CATEGORY = {
    "AUTHENTICATION_ERROR": CODE_AUTH,
    "INVALID_REQUEST_ERROR": CODE_INVALID,
    "RATE_LIMIT_ERROR": CODE_RATE_LIMITED,
    "API_ERROR": CODE_UNAVAILABLE,
}
CODE_BY_STATUS = {
    400: CODE_INVALID,
    401: CODE_AUTH,
    403: CODE_AUTH,
    404: CODE_NOT_FOUND,
    409: CODE_CONFLICT,
    422: CODE_INVALID,
    429: CODE_RATE_LIMITED,
}
HINTS = {
    CODE_AUTH: "Check the Square access token, and that the connection's environment matches it.",
    CODE_RATE_LIMITED: "Square is limiting requests; try again in a few minutes.",
    CODE_UNAVAILABLE: "Square had a problem on its side; try again later.",
}
# CreateCatalogImage accepts JPEG, PJPEG, PNG and GIF up to 15 MB. Typed by magic bytes, not the
# download's Content-Type, which storage servers often get wrong.
IMAGE_MAX_BYTES = 15 * 1024 * 1024
IMAGE_SIGNATURES = ((b"\xff\xd8\xff", "image/jpeg", "jpg"), (b"\x89PNG\r\n\x1a\n", "image/png", "png"), (b"GIF8", "image/gif", "gif"))

_ID = {"type": "string"}
_AMOUNT = {"type": ["number", "string"], "description": "Dollars: a number (1170) or text ('$1,170')."}

# ---- how Marvin handles each code -----------------------------------------------------------
# Declared, never acted on here: Marvin applies the policy (review, notify, retry) to whatever workflow
# called the action. Lookup: action[code] > provider[code] > action["*"] > provider["*"].
ANY_CODE = "*"
_RETRY_LATER = Handle(retry=Retry(backoff=(300,)), then=Handle(review=True, notify=True))
ERROR_POLICY: ErrorPolicy = {
    CODE_AUTH: Handle(notify=True),
    CODE_CONFIG: Handle(notify=True),
    CODE_RATE_LIMITED: Handle(retry=Retry(backoff=(60, 300, 900, 3600)), then=Handle(notify=True)),
    CODE_UNAVAILABLE: Handle(retry=Retry(backoff=(120, 600, 1800, 7200, 21600)), then=Handle(notify=True, review=True)),
    CODE_CONFLICT: Handle(retry=Retry(backoff=(30, 120)), then=Handle(review=True)),
    CODE_INVALID: Handle(review=True),
    CODE_NOT_FOUND: Handle(review=True),
    CODE_UNKNOWN: _RETRY_LATER,
    ANY_CODE: _RETRY_LATER,
}


def _when_reconnected(attempts: int) -> Handle:
    """A broken connection won't fix itself on a timer: tell the admins now, retry once it is healthy again."""
    return Handle(notify=True, retry=Retry(backoff=(), on_recovery=True, max_attempts=attempts), then=Handle(review=True))


# A listing is worth one retry after reconnecting (a later edit lists it anyway); a link left open can
# still sell the piece, so closing keeps trying.
CREATE_LISTING_POLICY: ErrorPolicy = {CODE_AUTH: _when_reconnected(1), CODE_CONFIG: _when_reconnected(1)}
CLOSE_LISTING_POLICY: ErrorPolicy = {
    CODE_AUTH: _when_reconnected(10),
    CODE_CONFIG: _when_reconnected(10),
    CODE_NOT_FOUND: Handle(succeed=True),  # the link (or the item) is already gone: closed
}
# A read is run by hand and its error shown: no retries or alerts. Every code is listed because the
# provider's own entry for a code outranks an action's "*".
READ_ONLY_POLICY: ErrorPolicy = {code: Handle() for code in (*CODES, ANY_CODE)}


def _now_rfc3339() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _fingerprint(*parts) -> str:
    """Short stable digest of what a listing is made of — what makes resumed progress (and an
    idempotency key) still valid for this attempt's input."""
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


class SquareError(IntegrationError):
    """A readable failure with a stable ``code`` (one of ``CODES``), handled by ``ERROR_POLICY``.

    ``partial`` holds the steps that completed (Marvin hands it back as ``ctx.resume`` on a retry);
    ``retry_after`` is Square's Retry-After hint on a 429. Still a ValueError underneath, so a Marvin
    without error policies fails the step with the message as before."""

    def __init__(self, message: str, code: str = CODE_UNKNOWN, *, partial: dict | None = None, retry_after: float | None = None) -> None:
        super().__init__(message, code=code, partial=partial, retry_after=retry_after)


def _square_errors(resp: Response) -> list[dict]:
    try:
        body = resp.json()
    except ValueError:
        return []
    errors = body.get("errors") if isinstance(body, dict) else None
    return [e for e in errors if isinstance(e, dict)] if isinstance(errors, list) else []


def _code_for(status: int, errors: list[dict]) -> str:
    """Square's own code first (NOT_FOUND and VERSION_MISMATCH sit under INVALID_REQUEST_ERROR), then
    its category, then the HTTP status."""
    first = errors[0] if errors else {}
    by_status = CODE_UNAVAILABLE if status >= HTTP_SERVER_ERROR else CODE_BY_STATUS.get(status, CODE_UNKNOWN)
    return CODE_BY_SQUARE_CODE.get(first.get("code")) or CODE_BY_CATEGORY.get(first.get("category")) or by_status


def _describe_error(error: dict) -> str:
    text = ": ".join(str(error[k]) for k in ("code", "detail") if error.get(k)) or "unknown error"
    return f"{text} (field {error['field']})" if error.get("field") else text


def _retry_after(resp: Response) -> float | None:
    """Seconds from a Retry-After header (delta-seconds or an HTTP date), or None."""
    value = next((str(v).strip() for k, v in (resp.headers or {}).items() if k.lower() == "retry-after"), "")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _http_error(resp: Response, what: str) -> SquareError:
    errors = _square_errors(resp)
    code = _code_for(resp.status_code, errors)
    detail = "; ".join(_describe_error(e) for e in errors) or resp.text[:ERROR_TEXT_LIMIT]
    hint = f" {HINTS[code]}" if code in HINTS else ""
    retry_after = _retry_after(resp) if code == CODE_RATE_LIMITED else None
    return SquareError(f"Square couldn't {what} (HTTP {resp.status_code}): {detail}{hint}", code, retry_after=retry_after)


@register_provider
class SquareProvider(IntegrationProvider):
    slug = "square"
    name = "Square"
    description = "List one-of-a-kind items in your Square catalog (stock of 1) and sell them through a Square checkout link."
    category = CATEGORY_DESTINATION
    icon = "🟩"

    # What a workspace needs (fields on its item type, the events webhook, three workflows) —
    # declared for review and Apply on the integration's card; see content.py.
    content = CONTENT

    # Square signs `notification URL + body` (base64 HMAC-SHA256). Contributed to Marvin's webhook
    # signature schemes so the declared `square-events` webhook (and any other) can verify it.
    signature_schemes: ClassVar[dict[str, dict]] = {
        "square": {
            "encoding": "base64",
            "message": "{url}{body}",
            "header": "x-square-hmacsha256-signature",
            "notes": "Square: base64 of URL + body",
        }
    }

    # How Marvin handles each failure code; create_listing and close_listing override parts of it.
    error_policy: ClassVar[ErrorPolicy] = ERROR_POLICY

    credentials = (
        CredentialField(
            key="access_token",
            label="Access token",
            help="Square access token (personal access token from the Developer Console, or an OAuth token). Sandbox and production tokens differ.",
        ),
    )
    config_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "environment": {
                "type": "string",
                "title": "Environment",
                "enum": list(BASE_URLS),
                "default": DEFAULT_ENVIRONMENT,
                "description": "Must match the token: sandbox tokens only work against sandbox.",
            },
            "location_id": {
                "type": "string",
                "title": "Location ID",
                "description": "Where stock is held and orders are taken. Required for listing; run the list_locations action to find it.",
            },
            "currency": {
                "type": "string",
                "title": "Currency",
                "default": DEFAULT_CURRENCY,
                "description": "ISO 4217 code of the location's currency.",
            },
            "redirect_url": {
                "type": "string",
                "title": "Thank-you page URL",
                "description": "Where Square sends buyers after paying. {slug} is replaced with the item's slug.",
            },
        },
        "additionalProperties": False,
    }

    actions = (
        ProviderAction(
            key="list_locations",
            label="List locations",
            description="The account's Square locations — use it to find the location_id for the config.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema={
                "type": "object",
                "properties": {
                    "locations": {
                        "type": "array",
                        "items": {"type": "object", "properties": {"id": _ID, "name": _ID, "status": _ID}},
                    }
                },
            },
            cost_hint="free",
            error_policy=READ_ONLY_POLICY,
        ),
        ProviderAction(
            key="create_listing",
            label="Create or refresh listing",
            description=(
                "Upsert a catalog item with stock of exactly 1 and create a checkout link for it. Pass the ids from an "
                "earlier listing to update it in place and replace its old link."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "Entry slug; fills {slug} in the redirect URL and becomes the payment note."},
                    "name": {"type": "string"},
                    "price": _AMOUNT,
                    "shipping_fee": {**_AMOUNT, "description": "Optional flat shipping charge in dollars; blank or 0 means none."},
                    "description": {"type": "string"},
                    "variation_id": {**_ID, "description": "Existing variation from an earlier listing of the same item."},
                    "payment_link_id": {**_ID, "description": "Existing payment link to replace."},
                    "image_url": {
                        **_ID,
                        "description": "Public URL of the item's picture (JPEG, PNG or GIF). Uploaded to the catalog item so the checkout shows it; "
                        "skipped when the item already has a picture. A picture that can't be fetched never stops the listing.",
                    },
                },
                "required": ["slug", "name", "price"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"item_id": _ID, "variation_id": _ID, "payment_link_id": _ID, "checkout_url": _ID, "order_id": _ID, "image_id": _ID},
            },
            error_policy=CREATE_LISTING_POLICY,
        ),
        ProviderAction(
            key="close_listing",
            label="Close listing",
            description=(
                "Set the item's stock to 0, then delete its checkout link (Square cancels its order). Stock goes first so a "
                "link that can't be deleted can't sell anything; a link that is already gone counts as closed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "payment_link_id": _ID,
                    "variation_id": {
                        **_ID,
                        "description": "The listing's variation, whose stock is set to 0 first. Without it only the link is deleted.",
                    },
                },
                "required": ["payment_link_id"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"closed": {"type": "boolean"}, "already": {"type": "boolean"}, "stock_zeroed": {"type": "boolean"}},
            },
            error_policy=CLOSE_LISTING_POLICY,
        ),
    )

    # ---- plumbing ---------------------------------------------------------------------------

    @staticmethod
    def _cfg(ctx: IntegrationContext) -> dict:
        return ctx.config or {}

    def _base(self, ctx: IntegrationContext) -> str:
        env = (self._cfg(ctx).get("environment") or DEFAULT_ENVIRONMENT).strip().lower()
        if env not in BASE_URLS:
            raise SquareError(f"Unknown Square environment {env!r}; use 'sandbox' or 'production'.", CODE_CONFIG)
        return BASE_URLS[env]

    @staticmethod
    def _headers(ctx: IntegrationContext) -> dict[str, str]:
        return {"Authorization": f"Bearer {ctx.secret}", "Square-Version": SQUARE_VERSION, "Content-Type": "application/json"}

    def _location_id(self, ctx: IntegrationContext) -> str:
        location_id = (self._cfg(ctx).get("location_id") or "").strip()
        if not location_id:
            raise SquareError("No Square location_id configured; run list_locations and add one to the integration config.", CODE_CONFIG)
        return location_id

    def _currency(self, ctx: IntegrationContext) -> str:
        return (self._cfg(ctx).get("currency") or DEFAULT_CURRENCY).strip().upper()

    @staticmethod
    def _ok_json(resp: Response, what: str) -> dict:
        if not resp.ok:
            raise _http_error(resp, what)
        return resp.json() if resp.content else {}

    def _get(self, ctx: IntegrationContext, path: str) -> Response:
        return ctx.http.get(f"{self._base(ctx)}{path}", headers=self._headers(ctx))

    def _post(self, ctx: IntegrationContext, path: str, body: dict, what: str) -> dict:
        resp = ctx.http.post(f"{self._base(ctx)}{path}", json=body, headers=self._headers(ctx))
        return self._ok_json(resp, what)

    def _post_multipart(self, ctx: IntegrationContext, path: str, parts: list[tuple[str, str, str | None, bytes]], what: str) -> dict:
        """POST multipart/form-data. Each part is (name, content type, filename or None, bytes)."""
        boundary = uuid.uuid4().hex
        body = bytearray()
        for name, content_type, filename, data in parts:
            disposition = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
            body += f"--{boundary}\r\nContent-Disposition: {disposition}\r\nContent-Type: {content_type}\r\n\r\n".encode()
            body += data + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        headers = {**self._headers(ctx), "Content-Type": f"multipart/form-data; boundary={boundary}"}
        return self._ok_json(ctx.http.post(f"{self._base(ctx)}{path}", data=bytes(body), headers=headers), what)

    def _delete(self, ctx: IntegrationContext, path: str) -> Response:
        return ctx.http.delete(f"{self._base(ctx)}{path}", headers=self._headers(ctx))

    # ---- Square calls -----------------------------------------------------------------------

    def _list_locations(self, ctx: IntegrationContext) -> list[dict]:
        data = self._ok_json(self._get(ctx, "/v2/locations"), "list locations")
        return [{"id": loc.get("id"), "name": loc.get("name"), "status": loc.get("status")} for loc in data.get("locations") or []]

    def _fetch_item_for_variation(self, ctx: IntegrationContext, variation_id: str) -> dict | None:
        """The full ITEM (with its variations and their versions) that owns `variation_id`, or None
        if the variation no longer exists."""
        resp = self._get(ctx, f"/v2/catalog/object/{quote(variation_id, safe='')}?include_related_objects=true")
        if resp.status_code == HTTP_NOT_FOUND:
            return None
        data = self._ok_json(resp, f"look up variation {variation_id} from the earlier listing")
        item_id = ((data.get("object") or {}).get("item_variation_data") or {}).get("item_id")
        item = next((o for o in data.get("related_objects") or [] if o.get("type") == "ITEM" and o.get("id") == item_id), None)
        if item is None and item_id:
            item = self._ok_json(self._get(ctx, f"/v2/catalog/object/{quote(item_id, safe='')}"), f"look up catalog item {item_id}").get("object")
        if not item:
            raise SquareError(f"Square variation {variation_id} has no parent item.")
        return item

    @staticmethod
    def _new_item(name: str, description: str, money: dict) -> dict:
        # "#"-prefixed ids are client placeholders; Square swaps in real ids on create.
        item_data = {
            "name": name,
            "variations": [
                {
                    "id": "#variation",
                    "type": "ITEM_VARIATION",
                    "item_variation_data": {
                        "item_id": "#item",
                        "name": VARIATION_NAME,
                        "pricing_type": "FIXED_PRICING",
                        "price_money": money,
                        "track_inventory": True,
                    },
                }
            ],
        }
        if description:
            item_data["description"] = description
        return {"id": "#item", "type": "ITEM", "item_data": item_data}

    @staticmethod
    def _updated_item(item: dict, variation_id: str, name: str, description: str, money: dict) -> dict:
        # Upsert is full replacement, so edit the fetched object rather than building a fresh one:
        # images, categories and anything else set in the Square dashboard survive, and every
        # object keeps the `version` Square needs to accept the update.
        item = copy.deepcopy(item)
        item_data = item.setdefault("item_data", {})
        item_data["name"] = name
        if description:
            # A fetched item carries description_html (and a read-only plaintext copy) alongside
            # description; drop them so the CMS text is not overridden by the dashboard's old copy.
            # Which field Square prefers when both are sent is not stated in the docs (unverified).
            item_data.pop("description_html", None)
            item_data.pop("description_plaintext", None)
            item_data["description"] = description
        variation = next((v for v in item_data.get("variations") or [] if v.get("id") == variation_id), None)
        if variation is None:
            raise SquareError(f"Square item {item.get('id')} does not contain variation {variation_id}.")
        vdata = variation.setdefault("item_variation_data", {})
        vdata.update({"pricing_type": "FIXED_PRICING", "price_money": money, "track_inventory": True})
        return item

    def _upsert_item(self, ctx: IntegrationContext, obj: dict, variation_id: str | None, key: str) -> tuple[str, str]:
        what = f'save catalog item "{(obj.get("item_data") or {}).get("name")}"'
        data = self._post(ctx, "/v2/catalog/object", {"idempotency_key": key, "object": obj}, what)
        saved = data.get("catalog_object") or {}
        variations = (saved.get("item_data") or {}).get("variations") or []
        if variation_id:
            variation = next((v for v in variations if v.get("id") == variation_id), None)
        else:
            variation = variations[0] if variations else None
        if not saved.get("id") or not variation or not variation.get("id"):
            raise SquareError("Square catalog upsert returned no item/variation id.")
        return saved["id"], variation["id"]

    def _set_stock(self, ctx: IntegrationContext, variation_id: str, location_id: str, quantity: str, what: str) -> None:
        # Docs show occurred_at in every example but do not say it is required — sent always (unverified).
        occurred_at = _now_rfc3339()
        change = {
            "type": "PHYSICAL_COUNT",
            "physical_count": {
                "catalog_object_id": variation_id,
                "state": "IN_STOCK",
                "location_id": location_id,
                "quantity": quantity,
                "occurred_at": occurred_at,
            },
        }
        # occurred_at is part of the key because it is part of the body: Square refuses a reused key with a
        # different body. Setting a count is idempotent by nature, so a fresh key per attempt costs nothing.
        key = ctx.idempotency_key("stock", variation_id, location_id, quantity, occurred_at)
        self._post(ctx, "/v2/inventory/changes/batch-create", {"idempotency_key": key, "changes": [change]}, what)

    @staticmethod
    def _download_image(ctx: IntegrationContext, image_url: str) -> tuple[bytes, str, str]:
        """The picture's bytes, MIME type and file extension. Raises ValueError when it can't be used."""
        resp = ctx.http.get(image_url)
        if not resp.ok:
            raise ValueError(f"HTTP {resp.status_code} fetching {image_url}")
        if len(resp.content) > IMAGE_MAX_BYTES:
            raise ValueError(f"{image_url} is larger than Square's 15 MB limit")
        kind = next(((mime, ext) for magic, mime, ext in IMAGE_SIGNATURES if resp.content.startswith(magic)), None)
        if kind is None:
            raise ValueError(f"{image_url} is not a JPEG, PNG or GIF")
        return resp.content, *kind

    def _attach_image(self, ctx: IntegrationContext, item_id: str, image_url: str, name: str, key: str) -> str:
        """Upload the picture and make it the item's primary image, which the checkout page shows."""
        data, mime, ext = self._download_image(ctx, image_url)
        request = {
            "idempotency_key": key,
            "object_id": item_id,
            "is_primary": True,
            "image": {"type": "IMAGE", "id": "#image", "image_data": {"name": name, "caption": name}},
        }
        parts = [("request", "application/json", None, json.dumps(request).encode()), ("file", mime, f"image.{ext}", data)]
        image_id = (self._post_multipart(ctx, "/v2/catalog/images", parts, "upload catalog image").get("image") or {}).get("id")
        if not image_id:
            raise ValueError("Square returned no image id.")
        return image_id

    def _maybe_attach_image(self, ctx: IntegrationContext, item: dict | None, item_id: str, image_url: str, name: str, key: str) -> str | None:
        """Best effort: a missing picture must never cost the sale, so failures are logged, not raised.
        An item that already has a picture (an earlier listing, or one set in the dashboard) keeps it."""
        if not image_url or ((item or {}).get("item_data") or {}).get("image_ids"):
            return None
        try:
            return self._attach_image(ctx, item_id, image_url, name, key)
        except Exception as e:  # noqa: BLE001 — see docstring
            ctx.logger.warning("square: listed %s without a picture: %s", item_id, e)
            return None

    def _delete_link(self, ctx: IntegrationContext, payment_link_id: str) -> bool:
        """Delete a payment link. True if it was deleted, False if it was already gone."""
        resp = self._delete(ctx, f"/v2/online-checkout/payment-links/{quote(payment_link_id, safe='')}")
        if resp.status_code == HTTP_NOT_FOUND:
            return False
        self._ok_json(resp, f"delete payment link {payment_link_id}")
        return True

    def _checkout_options(self, ctx: IntegrationContext, slug: str, shipping_cents: int) -> dict:
        options: dict = {"ask_for_shipping_address": True}
        redirect = (self._cfg(ctx).get("redirect_url") or "").strip()
        if redirect:
            options["redirect_url"] = redirect.replace(SLUG_PLACEHOLDER, quote(slug, safe=""))
        if shipping_cents > 0:
            options["shipping_fee"] = {"name": SHIPPING_FEE_NAME, "charge": {"amount": shipping_cents, "currency": self._currency(ctx)}}
        return options

    def _create_link(self, ctx: IntegrationContext, variation_id: str, location_id: str, slug: str, shipping_cents: int, name: str, key: str) -> dict:
        body = {
            "idempotency_key": key,
            # Referencing the catalog variation (not quick_pay) is what ties an online sale to the
            # same inventory the card reader decrements.
            "order": {"location_id": location_id, "line_items": [{"catalog_object_id": variation_id, "quantity": ONE_OF_A_KIND}]},
            "checkout_options": self._checkout_options(ctx, slug, shipping_cents),
            "payment_note": slug,
        }
        link = self._post(ctx, "/v2/online-checkout/payment-links", body, f'create the checkout link for "{name}"').get("payment_link") or {}
        if not link.get("id") or not link.get("url"):
            raise SquareError("Square returned a payment link with no id or url.")
        return link

    # ---- lifecycle --------------------------------------------------------------------------

    def check(self, ctx: IntegrationContext) -> tuple[str, str | None]:
        if not ctx.secret:
            return ("unconfigured", "Missing access token.")
        try:
            locations = self._list_locations(ctx)
        except Exception as e:  # noqa: BLE001 — any failure is reported on the card, never raised
            return ("error", str(e))
        location_id = (self._cfg(ctx).get("location_id") or "").strip()
        if location_id and location_id not in {loc["id"] for loc in locations}:
            known = ", ".join(f"{loc['id']} ({loc['name']})" for loc in locations) or "none"
            return ("error", f"Location {location_id} is not one of this account's locations: {known}. Check the environment matches the token.")
        return ("ok", None)

    def run_action(self, key: str, args: dict, ctx: IntegrationContext) -> dict:
        handler = {
            "list_locations": self._action_list_locations,
            "create_listing": self._action_create_listing,
            "close_listing": self._action_close_listing,
        }.get(key)
        if handler is None:
            raise NotImplementedError(f"square has no action '{key}'")
        if not ctx.secret:
            raise SquareError("No Square access token configured.", CODE_CONFIG)
        # The steps an action completes, recorded as it goes. On failure it rides on the error as `partial`
        # so the retry (ctx.resume) skips them. Just the input fingerprint is no progress worth keeping.
        progress: dict = {}

        def partial() -> dict | None:
            return dict(progress) if set(progress) - {"for"} else None

        try:
            return handler(args or {}, ctx, progress)
        except NotImplementedError:
            raise
        except SquareError as e:
            e.partial = e.partial or partial()
            raise
        except OSError as e:  # timeouts, refused connections, DNS: Square couldn't be reached
            message = f"Square {key} failed: couldn't reach Square ({type(e).__name__}: {e}); try again later."
            raise SquareError(message, CODE_UNAVAILABLE, partial=partial()) from e
        except ValueError as e:
            raise SquareError(str(e), partial=partial()) from e
        except Exception as e:  # anything else must fail the step as a ValueError, not escape the workflow engine
            raise SquareError(f"Square {key} failed: {type(e).__name__}: {e}", partial=partial()) from e

    # ---- actions ----------------------------------------------------------------------------

    def _action_list_locations(self, args: dict, ctx: IntegrationContext, progress: dict) -> dict:
        return {"locations": self._list_locations(ctx)}

    @staticmethod
    def _resume_listing(ctx: IntegrationContext, fingerprint: str, progress: dict) -> None:
        """Carry an earlier attempt's progress into this one.

        The catalog item it saved is always reused, so a retry never makes a duplicate item. Its other
        steps count as done only if the listing's input is unchanged; if the price (say) changed since,
        they are redone, and the link that attempt made is retired with the old one — it sells at the old price."""
        resume = ctx.resume if isinstance(ctx.resume, dict) else {}
        progress.update({k: resume[k] for k in ("item_id", "variation_id") if resume.get(k)})
        retire = [link for link in resume.get("retire_link_ids") or [] if link]
        if resume.get("for") == fingerprint:
            progress.update({k: resume[k] for k in ("saved", "image_id", "stock_set", "payment_link_id", "checkout_url", "order_id") if k in resume})
        elif resume.get("payment_link_id"):
            retire.append(resume["payment_link_id"])
        if retire:
            progress["retire_link_ids"] = retire
        progress["for"] = fingerprint

    def _action_create_listing(self, args: dict, ctx: IntegrationContext, progress: dict) -> dict:
        slug, name = str(args.get("slug") or "").strip(), str(args.get("name") or "").strip()
        if not slug or not name:
            raise SquareError("create_listing needs 'slug' and 'name'.", CODE_INVALID)
        if "price" not in args:
            raise SquareError(f'"{name}" has no price to list it at.', CODE_INVALID)
        try:
            money = {"amount": price_cents(args["price"]), "currency": self._currency(ctx)}
            shipping_cents = optional_fee_cents(args.get("shipping_fee"))
        except ValueError as e:
            raise SquareError(f'"{name}" can\'t be listed: {e}', CODE_INVALID) from e
        description = str(args.get("description") or "").strip()
        old_variation_id = str(args.get("variation_id") or "").strip()
        old_link_id = str(args.get("payment_link_id") or "").strip()
        image_url = str(args.get("image_url") or "").strip()
        location_id = self._location_id(ctx)
        fingerprint = _fingerprint(slug, name, description, money, image_url, location_id, self._checkout_options(ctx, slug, shipping_cents))
        self._resume_listing(ctx, fingerprint, progress)

        # 1. The catalog item (and its picture, best effort). Keys repeat across one retry chain for the same
        # body, so a request Square already did (its answer lost) is replayed, not done twice.
        if not progress.get("saved"):
            update_variation = progress.get("variation_id") or old_variation_id
            existing = self._fetch_item_for_variation(ctx, update_variation) if update_variation else None
            if update_variation and existing is None:
                ctx.logger.warning("square: variation %s no longer exists; creating a new catalog item for %s", update_variation, slug)
            if existing is not None:
                obj, keep_variation = self._updated_item(existing, update_variation, name, description, money), update_variation
            else:
                obj, keep_variation = self._new_item(name, description, money), None
            key = ctx.idempotency_key("item", fingerprint, keep_variation or "new", (existing or {}).get("version"))
            item_id, variation_id = self._upsert_item(ctx, obj, keep_variation, key)
            progress.update(item_id=item_id, variation_id=variation_id, saved=True)
            progress["image_id"] = self._maybe_attach_image(
                ctx, existing, item_id, image_url, name, ctx.idempotency_key("image", fingerprint, item_id)
            )
        item_id, variation_id = progress["item_id"], progress["variation_id"]

        # 2. Stock of exactly 1.
        if not progress.get("stock_set"):
            self._set_stock(ctx, variation_id, location_id, ONE_OF_A_KIND, f'set the stock of "{name}" to 1')
            progress["stock_set"] = True

        # 3. The new link — before the old one goes, so the item is never left without a way to buy it.
        if not progress.get("payment_link_id"):
            key = ctx.idempotency_key("link", fingerprint, variation_id)
            link = self._create_link(ctx, variation_id, location_id, slug, shipping_cents, name, key)
            progress.update(payment_link_id=link["id"], checkout_url=link["url"], order_id=link.get("order_id"))

        # 4. Retire the old link, and any an earlier attempt made for different input.
        for link_id in dict.fromkeys([old_link_id, *progress.get("retire_link_ids", [])]):
            if link_id and link_id != progress["payment_link_id"]:
                self._delete_link(ctx, link_id)
                progress["retire_link_ids"] = [x for x in progress.get("retire_link_ids", []) if x != link_id]
        return {
            "item_id": item_id,
            "variation_id": variation_id,
            "payment_link_id": progress["payment_link_id"],
            "checkout_url": progress["checkout_url"],
            "order_id": progress.get("order_id"),
            "image_id": progress.get("image_id"),
        }

    def _action_close_listing(self, args: dict, ctx: IntegrationContext, progress: dict) -> dict:
        payment_link_id = str(args.get("payment_link_id") or "").strip()
        if not payment_link_id:
            raise SquareError("close_listing needs 'payment_link_id'.", CODE_INVALID)
        variation_id = str(args.get("variation_id") or "").strip()
        resume = ctx.resume if isinstance(ctx.resume, dict) else {}
        progress["for"] = payment_link_id
        if resume.get("for") == payment_link_id and resume.get("stock_zeroed"):
            progress["stock_zeroed"] = True

        # Stock first: if the link then can't be deleted, it has nothing left to sell.
        if variation_id and not progress.get("stock_zeroed"):
            try:
                self._set_stock(ctx, variation_id, self._location_id(ctx), NONE_LEFT, f"set the stock of variation {variation_id} to 0")
            except SquareError as e:
                if e.code != CODE_NOT_FOUND:
                    raise
                ctx.logger.info("square: variation %s is gone; nothing left to sell", variation_id)
            progress["stock_zeroed"] = True
        elif not variation_id:
            ctx.logger.warning("square: closing %s without a variation_id, so its stock is left as it is", payment_link_id)

        deleted = self._delete_link(ctx, payment_link_id)
        return {"closed": True, "already": not deleted, "stock_zeroed": bool(progress.get("stock_zeroed"))}
