"""What a workspace needs for Square selling, declared for the core to offer — never created here.

Applying (from the integration's card) creates only what is missing: the shop fields on the
workspace's own item type, the incoming webhook Square posts to, and six workflows. Webhooks and
workflows arrive switched off; turning them on after a review is the deliberate last step.

The loop:
  a published item with "Sell online" on (or a new price/shipping) → `create_listing` → ids + checkout URL stored on the entry
  → the site shows a Buy button. A sale — through the link, or rung up on a card reader as the
  catalog item — takes the stock to 0 → Square posts `inventory.count.updated` → the item's status
  becomes "sold" → its checkout link is closed and the site rebuilt. Taking an item out of the shop any
  other way — switching Sell online off, unpublishing, archiving — closes its link the same way.

Sites stay provider-neutral: the workflows store the Buy-button link as `checkout_url` (with
`checkout_provider: square`) and mark `checkout_closed` once sold — keys any commerce integration can
write. Square's own ids stay under `square_*`.

Closing is ordered so a Square failure can't leave the piece for sale: first `checkout_closed: true` and a
site rebuild (the Buy button goes), then `close_listing` (stock to 0, then the link deleted), and only once
that succeeds `square_link_closed: true`. The close workflows run while `square_link_closed` isn't true, so
a retry Marvin schedules for a failed close still passes their conditions after the button is gone.

Two parameters keep it general: `entry_type` (which type is the shop's items, default `artwork`) and
`integration` (this integration's slug in the workspace, default `square`).
"""

from marvin_integration_sdk import ContentBlueprint

CATEGORY = "Square"

ENTRY_TYPE_PARAM = {
    "key": "entry_type",
    "label": "Which entry type are the items for sale?",
    "kind": "entry_type",
    "default": "artwork",
    "help": "Its entries get a Sell online switch and a shipping fee.",
}
INTEGRATION_PARAM = {
    "key": "integration",
    "label": "Which Square connection",
    "kind": "integration",
    "default": "square",
    "help": "The workflows call this connection's actions.",
}

WEBHOOK_SLUG = "square-events"

SHOP_FIELDS = ContentBlueprint(
    kind="entry_fields",
    slug="square-shop-fields",
    name="Shop fields (Sell online, price, shipping)",
    description="Adds a Sell online switch, a price and a flat shipping fee to the item type. Fields it already has are left alone.",
    required=True,
    category=CATEGORY,
    parameters=(ENTRY_TYPE_PARAM,),
    payload={
        "entry_type": "{{entry_type}}",
        "fields": [
            {"key": "sellOnline", "label": "Sell online", "type": "boolean", "help": "List this item on Square with a Buy button."},
            {"key": "price", "label": "Price", "type": "text", "help": "e.g. $1,170"},
            {
                "key": "shippingFee",
                "label": "Shipping fee",
                "type": "text",
                "help": "Flat shipping charge added at checkout, e.g. $45. Blank = none.",
            },
        ],
    },
)

EVENTS_WEBHOOK = ContentBlueprint(
    kind="incoming_webhook",
    slug=WEBHOOK_SLUG,
    name="Square events",
    description="Where Square posts payment and inventory updates. Mint its token, paste the URL into your Square app's webhook subscription, and store the signature key as SQUARE_SIGNATURE_KEY.",
    required=True,
    category=CATEGORY,
    payload={
        "name": "Square events",
        "description": "Square webhook subscription: inventory.count.updated (and payment.updated).",
        "signature_scheme": "square",
        "signing_secret_ref": "SQUARE_SIGNATURE_KEY",
    },
)

LIST_ON_PUBLISH = ContentBlueprint(
    kind="workflow",
    slug="square-list-for-sale",
    name="Square: list for sale",
    description="When a published item has Sell online on — or its price or shipping changes — create (or refresh) its Square listing and store the checkout link on it.",
    required=True,
    category=CATEGORY,
    parameters=(ENTRY_TYPE_PARAM, INTEGRATION_PARAM),
    payload={
        "definition": {
            # entry_updated, not entry_published: publishing is an update too, and Sell online is often
            # switched on for a work that is already published. What it was listed at is stored below,
            # so a listing happens once per price/shipping (its own metadata write doesn't loop).
            "trigger": {"type": "event", "event": "entry_updated"},
            "conditions": [
                {"field": "entry.entry_type", "op": "eq", "value": "{{entry_type}}"},
                {"field": "entry.status", "op": "eq", "value": "published"},
                {"field": "entry.data.sellOnline", "op": "eq", "value": True},
                {"field": "entry.data.status", "op": "neq", "value": "sold"},
                {"field": "entry.data.price", "op": "exists"},
                {"field": "entry.metadata.square_listed_for", "op": "neq", "value": "${entry.data.price}|${entry.data.shippingFee}"},
            ],
            "actions": [
                {
                    "kind": "integration",
                    "id": "listing",
                    "integration": "{{integration}}",
                    "action": "create_listing",
                    "args": {
                        "slug": "${entry.slug}",
                        "name": "${entry.title}",
                        "price": "${entry.data.price}",
                        "shipping_fee": "${entry.data.shippingFee}",
                        "description": "${entry.summary}",
                        "variation_id": "${entry.metadata.square_variation_id}",
                        "payment_link_id": "${entry.metadata.square_payment_link_id}",
                        "image_url": "${entry.image}",
                    },
                },
                {
                    "kind": "entry",
                    "op": "set_metadata",
                    "metadata": {
                        "square_item_id": "${steps.listing.output.item_id}",
                        "square_variation_id": "${steps.listing.output.variation_id}",
                        "square_payment_link_id": "${steps.listing.output.payment_link_id}",
                        "square_order_id": "${steps.listing.output.order_id}",
                        "checkout_url": "${steps.listing.output.checkout_url}",
                        "checkout_provider": "square",
                        "checkout_closed": False,
                        "square_link_closed": False,
                        "square_listed_for": "${entry.data.price}|${entry.data.shippingFee}",
                    },
                },
                {"kind": "handler", "task": "request_site_rebuild", "config": {"reason": "square listing"}},
            ],
        }
    },
)

MARK_SOLD = ContentBlueprint(
    kind="workflow",
    slug="square-mark-sold",
    name="Square: mark sold",
    description=(
        "When Square reports an item's stock reached 0 — sold online or on a card reader — set its status to sold. "
        "Stock that Marvin zeroed itself while closing the listing is not a sale."
    ),
    required=True,
    category=CATEGORY,
    parameters=(ENTRY_TYPE_PARAM,),
    payload={
        "definition": {
            "trigger": {"type": "incoming_webhook", "webhook": WEBHOOK_SLUG},
            # A target, not an entity_query, so the item's own metadata can be checked: closing a listing sets
            # `checkout_closed` before it zeroes the stock, and that zero must not mark a withdrawn item sold.
            # A count for a catalog item no entry lists matches nothing and does nothing.
            "target": {
                "entity": "entry",
                "query": {
                    "entry_type": "{{entry_type}}",
                    "metadata": {"square_variation_id": "${event.payload.data.object.inventory_counts.0.catalog_object_id}"},
                },
            },
            "conditions": [
                {"field": "event.payload.type", "op": "eq", "value": "inventory.count.updated"},
                {"field": "event.payload.data.object.inventory_counts.0.state", "op": "eq", "value": "IN_STOCK"},
                {"field": "event.payload.data.object.inventory_counts.0.quantity", "op": "eq", "value": "0"},
                {"field": "entry.metadata.checkout_closed", "op": "neq", "value": True},
            ],
            "actions": [{"kind": "entry", "op": "set_data", "data": {"status": "sold"}}],
        }
    },
)


def _close_steps(reason: str) -> list[dict]:
    """Hide the Buy button before touching Square, so a close that fails (and is retried) never leaves the
    piece for sale on the site. `square_listed_for: closed` never equals a price|shipping, so putting the
    item back on sale lists it again."""
    return [
        {"kind": "entry", "op": "set_metadata", "metadata": {"checkout_closed": True, "square_listed_for": "closed"}},
        {"kind": "handler", "task": "request_site_rebuild", "config": {"reason": reason}},
        {
            "kind": "integration",
            "integration": "{{integration}}",
            "action": "close_listing",
            "args": {"payment_link_id": "${entry.metadata.square_payment_link_id}", "variation_id": "${entry.metadata.square_variation_id}"},
        },
        {"kind": "entry", "op": "set_metadata", "metadata": {"square_link_closed": True}},
    ]


# The marker that stops a repeat close. It is set only once Square confirms, so a retry of a failed close
# still passes; a re-listing sets it back to false, so the next sale closes the new link too.
LINK_OPEN = {"field": "entry.metadata.square_link_closed", "op": "neq", "value": True}


def _close_when(slug: str, name: str, description: str, event: str, conditions: list[dict], reason: str) -> ContentBlueprint:
    """A workflow that closes an item's open checkout link when it stops being for sale. Its conditions
    must also hold for a retry hours later: they describe the item still being out of the shop, so one
    put back on sale meanwhile drops the retry instead of closing its new link."""
    return ContentBlueprint(
        kind="workflow",
        slug=slug,
        name=name,
        description=description,
        required=True,
        category=CATEGORY,
        parameters=(ENTRY_TYPE_PARAM, INTEGRATION_PARAM),
        payload={
            "definition": {
                "trigger": {"type": "event", "event": event},
                "conditions": [
                    {"field": "entry.entry_type", "op": "eq", "value": "{{entry_type}}"},
                    *conditions,
                    {"field": "entry.metadata.square_payment_link_id", "op": "exists"},
                    LINK_OPEN,
                ],
                "actions": _close_steps(reason),
            }
        },
    )


CLOSE_WHEN_SOLD = _close_when(
    "square-close-when-sold",
    "Square: close the link when sold",
    "When an item's status becomes sold (by a sale or by hand), hide its Buy button, rebuild the site and close its Square checkout link. Runs once per item.",
    "entry_updated",
    [{"field": "entry.data.status", "op": "eq", "value": "sold"}],
    "square sale",
)


# Taking an item out of the shop any other way than a sale must close its link too — or a buyer could still
# pay for something switched off, unpublished or archived (e.g. a duplicate entry being retired).
CLOSE_WHEN_WITHDRAWN = _close_when(
    "square-close-when-withdrawn",
    "Square: close the link when Sell online is switched off",
    "When an item's Sell online is switched off, hide its Buy button, rebuild the site and close its Square checkout link. Switching it back on lists it again.",
    "entry_updated",
    [{"field": "entry.data.sellOnline", "op": "neq", "value": True}],
    "square listing withdrawn",
)
CLOSE_WHEN_UNPUBLISHED = _close_when(
    "square-close-when-unpublished",
    "Square: close the link when unpublished",
    "When an item is unpublished, hide its Buy button, rebuild the site and close its Square checkout link. Republishing lists it again.",
    "entry_unpublished",
    [{"field": "entry.status", "op": "neq", "value": "published"}],
    "square listing unpublished",
)
CLOSE_WHEN_ARCHIVED = _close_when(
    "square-close-when-archived",
    "Square: close the link when archived",
    "When an item is archived, hide its Buy button, rebuild the site and close its Square checkout link. Restoring and republishing lists it again.",
    "entry_archived",
    [{"field": "entry.status", "op": "eq", "value": "archived"}],
    "square listing archived",
)

CONTENT = (
    SHOP_FIELDS,
    EVENTS_WEBHOOK,
    LIST_ON_PUBLISH,
    MARK_SOLD,
    CLOSE_WHEN_SOLD,
    CLOSE_WHEN_WITHDRAWN,
    CLOSE_WHEN_UNPUBLISHED,
    CLOSE_WHEN_ARCHIVED,
)
