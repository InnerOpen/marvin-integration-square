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
ERROR_TEXT_LIMIT = 300

_ID = {"type": "string"}
_AMOUNT = {"type": ["number", "string"], "description": "Dollars: a number (1170) or text ('$1,170')."}


def _new_key() -> str:
    # Unique per call on purpose: Square replays the stored response for a repeated key, so a
    # deterministic key (e.g. derived from the slug) would hand a re-listing after a price change
    # the stale result of the first listing.
    return str(uuid.uuid4())


def _now_rfc3339() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _square_errors(resp: Response) -> str:
    """Square's `errors[]` as one readable line, falling back to the raw body."""
    try:
        errors = (resp.json() or {}).get("errors") or []
    except ValueError:
        errors = []
    return "; ".join(_describe_error(e) for e in errors if isinstance(e, dict)) or resp.text[:ERROR_TEXT_LIMIT]


def _describe_error(error: dict) -> str:
    text = ": ".join(str(error[k]) for k in ("code", "detail") if error.get(k)) or "unknown error"
    return f"{text} (field {error['field']})" if error.get("field") else text


@register_provider
class SquareProvider(IntegrationProvider):
    slug = "square"
    name = "Square"
    description = "List one-of-a-kind items in your Square catalog (stock of 1) and sell them through a Square checkout link."
    category = CATEGORY_DESTINATION
    icon = "🟩"

    # The artwork entry type belongs to the site's workspace, not to Square, so nothing is
    # declared here. The fields and metadata keys the workflows expect are documented in the README.
    content = ()

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
                },
                "required": ["slug", "name", "price"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"item_id": _ID, "variation_id": _ID, "payment_link_id": _ID, "checkout_url": _ID, "order_id": _ID},
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
            raise ValueError(f"Unknown Square environment {env!r}; use 'sandbox' or 'production'.")
        return BASE_URLS[env]

    @staticmethod
    def _headers(ctx: IntegrationContext) -> dict[str, str]:
        return {"Authorization": f"Bearer {ctx.secret}", "Square-Version": SQUARE_VERSION, "Content-Type": "application/json"}

    def _location_id(self, ctx: IntegrationContext) -> str:
        location_id = (self._cfg(ctx).get("location_id") or "").strip()
        if not location_id:
            raise ValueError("No Square location_id configured; run list_locations and add one to the integration config.")
        return location_id

    def _currency(self, ctx: IntegrationContext) -> str:
        return (self._cfg(ctx).get("currency") or DEFAULT_CURRENCY).strip().upper()

    @staticmethod
    def _ok_json(resp: Response, what: str) -> dict:
        if not resp.ok:
            raise ValueError(f"Square {what} failed: HTTP {resp.status_code}: {_square_errors(resp)}")
        return resp.json() if resp.content else {}

    def _get(self, ctx: IntegrationContext, path: str) -> Response:
        return ctx.http.get(f"{self._base(ctx)}{path}", headers=self._headers(ctx))

    def _post(self, ctx: IntegrationContext, path: str, body: dict, what: str) -> dict:
        resp = ctx.http.post(f"{self._base(ctx)}{path}", json=body, headers=self._headers(ctx))
        return self._ok_json(resp, what)

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
        data = self._ok_json(resp, "retrieve catalog variation")
        item_id = ((data.get("object") or {}).get("item_variation_data") or {}).get("item_id")
        item = next((o for o in data.get("related_objects") or [] if o.get("type") == "ITEM" and o.get("id") == item_id), None)
        if item is None and item_id:
            item = self._ok_json(self._get(ctx, f"/v2/catalog/object/{quote(item_id, safe='')}"), "retrieve catalog item").get("object")
        if not item:
            raise ValueError(f"Square variation {variation_id} has no parent item.")
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
            raise ValueError(f"Square item {item.get('id')} does not contain variation {variation_id}.")
        vdata = variation.setdefault("item_variation_data", {})
        vdata.update({"pricing_type": "FIXED_PRICING", "price_money": money, "track_inventory": True})
        return item

    def _upsert_item(self, ctx: IntegrationContext, obj: dict, variation_id: str | None) -> tuple[str, str]:
        data = self._post(ctx, "/v2/catalog/object", {"idempotency_key": _new_key(), "object": obj}, "catalog upsert")
        saved = data.get("catalog_object") or {}
        variations = (saved.get("item_data") or {}).get("variations") or []
        if variation_id:
            variation = next((v for v in variations if v.get("id") == variation_id), None)
        else:
            variation = variations[0] if variations else None
        if not saved.get("id") or not variation or not variation.get("id"):
            raise ValueError("Square catalog upsert returned no item/variation id.")
        return saved["id"], variation["id"]

    def _set_stock_to_one(self, ctx: IntegrationContext, variation_id: str, location_id: str) -> None:
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
        self._post(ctx, "/v2/inventory/changes/batch-create", {"idempotency_key": _new_key(), "changes": [change]}, "inventory count")

    def _delete_link(self, ctx: IntegrationContext, payment_link_id: str) -> bool:
        """Delete a payment link. True if it was deleted, False if it was already gone."""
        resp = self._delete(ctx, f"/v2/online-checkout/payment-links/{quote(payment_link_id, safe='')}")
        if resp.status_code == HTTP_NOT_FOUND:
            return False
        self._ok_json(resp, "delete payment link")
        return True

    def _checkout_options(self, ctx: IntegrationContext, slug: str, shipping_cents: int) -> dict:
        options: dict = {"ask_for_shipping_address": True}
        redirect = (self._cfg(ctx).get("redirect_url") or "").strip()
        if redirect:
            options["redirect_url"] = redirect.replace(SLUG_PLACEHOLDER, quote(slug, safe=""))
        if shipping_cents > 0:
            options["shipping_fee"] = {"name": SHIPPING_FEE_NAME, "charge": {"amount": shipping_cents, "currency": self._currency(ctx)}}
        return options

    def _create_link(self, ctx: IntegrationContext, variation_id: str, location_id: str, slug: str, shipping_cents: int) -> dict:
        body = {
            "idempotency_key": _new_key(),
            # Referencing the catalog variation (not quick_pay) is what ties an online sale to the
            # same inventory the card reader decrements.
            "order": {"location_id": location_id, "line_items": [{"catalog_object_id": variation_id, "quantity": ONE_OF_A_KIND}]},
            "checkout_options": self._checkout_options(ctx, slug, shipping_cents),
            "payment_note": slug,
        }
        link = self._post(ctx, "/v2/online-checkout/payment-links", body, "create payment link").get("payment_link") or {}
        if not link.get("id") or not link.get("url"):
            raise ValueError("Square returned a payment link with no id or url.")
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
            raise ValueError("No Square access token configured.")
        return handler(args or {}, ctx)

    # ---- actions ----------------------------------------------------------------------------

    def _action_list_locations(self, args: dict, ctx: IntegrationContext) -> dict:
        return {"locations": self._list_locations(ctx)}

    def _action_create_listing(self, args: dict, ctx: IntegrationContext) -> dict:
        slug, name = str(args.get("slug") or "").strip(), str(args.get("name") or "").strip()
        if not slug or not name:
            raise ValueError("create_listing needs 'slug' and 'name'.")
        if "price" not in args:
            raise ValueError("create_listing needs 'price'.")
        money = {"amount": price_cents(args["price"]), "currency": self._currency(ctx)}
        shipping_cents = optional_fee_cents(args.get("shipping_fee"))
        description = str(args.get("description") or "").strip()
        old_variation_id = str(args.get("variation_id") or "").strip()
        old_link_id = str(args.get("payment_link_id") or "").strip()
        location_id = self._location_id(ctx)

        existing = self._fetch_item_for_variation(ctx, old_variation_id) if old_variation_id else None
        if old_variation_id and existing is None:
            ctx.logger.warning("square: variation %s no longer exists; creating a new catalog item for %s", old_variation_id, slug)
        if existing is not None:
            obj, keep_variation = self._updated_item(existing, old_variation_id, name, description, money), old_variation_id
        else:
            obj, keep_variation = self._new_item(name, description, money), None
        item_id, variation_id = self._upsert_item(ctx, obj, keep_variation)

        self._set_stock_to_one(ctx, variation_id, location_id)
        if old_link_id:
            self._delete_link(ctx, old_link_id)
        link = self._create_link(ctx, variation_id, location_id, slug, shipping_cents)
        return {
            "item_id": item_id,
            "variation_id": variation_id,
            "payment_link_id": link["id"],
            "checkout_url": link["url"],
            "order_id": link.get("order_id"),
        }

    def _action_close_listing(self, args: dict, ctx: IntegrationContext) -> dict:
        payment_link_id = str(args.get("payment_link_id") or "").strip()
        if not payment_link_id:
            raise ValueError("close_listing needs 'payment_link_id'.")
        deleted = self._delete_link(ctx, payment_link_id)
        return {"closed": True, "already": not deleted}
