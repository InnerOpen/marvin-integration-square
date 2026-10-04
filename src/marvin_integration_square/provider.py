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
import json
import uuid
from datetime import UTC, datetime
from typing import ClassVar
from urllib.parse import quote

from marvin_integration_sdk import (
    CATEGORY_DESTINATION,
    CredentialField,
    IntegrationContext,
    IntegrationProvider,
    ProviderAction,
    Response,
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


def _new_key() -> str:
    # Unique per call on purpose: Square replays the stored response for a repeated key, so a
    # deterministic key (e.g. derived from the slug) would hand a re-listing after a price change
    # the stale result of the first listing.
    return str(uuid.uuid4())


def _now_rfc3339() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class SquareError(ValueError):
    """A readable failure with a stable ``code`` (one of ``CODES``).

    Still a ValueError, so Marvin fails the workflow step with the message; a Marvin that reads the
    ``code`` hands it on as ``${error.code}``."""

    def __init__(self, message: str, code: str = CODE_UNKNOWN) -> None:
        super().__init__(message)
        self.code = code


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


def _http_error(resp: Response, what: str) -> SquareError:
    errors = _square_errors(resp)
    code = _code_for(resp.status_code, errors)
    detail = "; ".join(_describe_error(e) for e in errors) or resp.text[:ERROR_TEXT_LIMIT]
    hint = f" {HINTS[code]}" if code in HINTS else ""
    return SquareError(f"Square couldn't {what} (HTTP {resp.status_code}): {detail}{hint}", code)


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
        ),
        ProviderAction(
            key="close_listing",
            label="Close listing",
            description="Delete the checkout link (Square cancels its order). A link that is already gone counts as closed.",
            input_schema={
                "type": "object",
                "properties": {"payment_link_id": _ID},
                "required": ["payment_link_id"],
                "additionalProperties": False,
            },
            output_schema={"type": "object", "properties": {"closed": {"type": "boolean"}, "already": {"type": "boolean"}}},
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

    def _upsert_item(self, ctx: IntegrationContext, obj: dict, variation_id: str | None) -> tuple[str, str]:
        what = f'save catalog item "{(obj.get("item_data") or {}).get("name")}"'
        data = self._post(ctx, "/v2/catalog/object", {"idempotency_key": _new_key(), "object": obj}, what)
        saved = data.get("catalog_object") or {}
        variations = (saved.get("item_data") or {}).get("variations") or []
        if variation_id:
            variation = next((v for v in variations if v.get("id") == variation_id), None)
        else:
            variation = variations[0] if variations else None
        if not saved.get("id") or not variation or not variation.get("id"):
            raise SquareError("Square catalog upsert returned no item/variation id.")
        return saved["id"], variation["id"]

    def _set_stock_to_one(self, ctx: IntegrationContext, variation_id: str, location_id: str, name: str) -> None:
        change = {
            "type": "PHYSICAL_COUNT",
            "physical_count": {
                "catalog_object_id": variation_id,
                "state": "IN_STOCK",
                "location_id": location_id,
                "quantity": ONE_OF_A_KIND,
                # Docs show occurred_at in every example but do not say it is required — sent always (unverified).
                "occurred_at": _now_rfc3339(),
            },
        }
        self._post(ctx, "/v2/inventory/changes/batch-create", {"idempotency_key": _new_key(), "changes": [change]}, f'set the stock of "{name}" to 1')

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

    def _attach_image(self, ctx: IntegrationContext, item_id: str, image_url: str, name: str) -> str:
        """Upload the picture and make it the item's primary image, which the checkout page shows."""
        data, mime, ext = self._download_image(ctx, image_url)
        request = {
            "idempotency_key": _new_key(),
            "object_id": item_id,
            "is_primary": True,
            "image": {"type": "IMAGE", "id": "#image", "image_data": {"name": name, "caption": name}},
        }
        parts = [("request", "application/json", None, json.dumps(request).encode()), ("file", mime, f"image.{ext}", data)]
        image_id = (self._post_multipart(ctx, "/v2/catalog/images", parts, "upload catalog image").get("image") or {}).get("id")
        if not image_id:
            raise ValueError("Square returned no image id.")
        return image_id

    def _maybe_attach_image(self, ctx: IntegrationContext, item: dict | None, item_id: str, image_url: str, name: str) -> str | None:
        """Best effort: a missing picture must never cost the sale, so failures are logged, not raised.
        An item that already has a picture (an earlier listing, or one set in the dashboard) keeps it."""
        if not image_url or ((item or {}).get("item_data") or {}).get("image_ids"):
            return None
        try:
            return self._attach_image(ctx, item_id, image_url, name)
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

    def _create_link(self, ctx: IntegrationContext, variation_id: str, location_id: str, slug: str, shipping_cents: int, name: str) -> dict:
        body = {
            "idempotency_key": _new_key(),
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
        try:
            return handler(args or {}, ctx)
        except (SquareError, NotImplementedError):
            raise
        except OSError as e:  # timeouts, refused connections, DNS: Square couldn't be reached
            raise SquareError(f"Square {key} failed: couldn't reach Square ({type(e).__name__}: {e}); try again later.", CODE_UNAVAILABLE) from e
        except ValueError as e:
            raise SquareError(str(e)) from e
        except Exception as e:  # anything else must fail the step as a ValueError, not escape the workflow engine
            raise SquareError(f"Square {key} failed: {type(e).__name__}: {e}") from e

    # ---- actions ----------------------------------------------------------------------------

    def _action_list_locations(self, args: dict, ctx: IntegrationContext) -> dict:
        return {"locations": self._list_locations(ctx)}

    def _action_create_listing(self, args: dict, ctx: IntegrationContext) -> dict:
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

        existing = self._fetch_item_for_variation(ctx, old_variation_id) if old_variation_id else None
        if old_variation_id and existing is None:
            ctx.logger.warning("square: variation %s no longer exists; creating a new catalog item for %s", old_variation_id, slug)
        if existing is not None:
            obj, keep_variation = self._updated_item(existing, old_variation_id, name, description, money), old_variation_id
        else:
            obj, keep_variation = self._new_item(name, description, money), None
        item_id, variation_id = self._upsert_item(ctx, obj, keep_variation)
        image_id = self._maybe_attach_image(ctx, existing, item_id, image_url, name)

        self._set_stock_to_one(ctx, variation_id, location_id, name)
        if old_link_id:
            self._delete_link(ctx, old_link_id)
        link = self._create_link(ctx, variation_id, location_id, slug, shipping_cents, name)
        return {
            "item_id": item_id,
            "variation_id": variation_id,
            "payment_link_id": link["id"],
            "checkout_url": link["url"],
            "order_id": link.get("order_id"),
            "image_id": image_id,
        }

    def _action_close_listing(self, args: dict, ctx: IntegrationContext) -> dict:
        payment_link_id = str(args.get("payment_link_id") or "").strip()
        if not payment_link_id:
            raise SquareError("close_listing needs 'payment_link_id'.", CODE_INVALID)
        deleted = self._delete_link(ctx, payment_link_id)
        return {"closed": True, "already": not deleted}
