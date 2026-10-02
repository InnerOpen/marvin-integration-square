"""What a workspace needs for Square selling, declared for the core to offer — never created here.

Applying (from the integration's card) creates only what is missing: the shop fields on the
workspace's own item type, the incoming webhook Square posts to, and three workflows. Webhooks and
workflows arrive switched off; turning them on after a review is the deliberate last step.

The loop:
  publish an item with "Sell online" on → `create_listing` → ids + checkout URL stored on the entry
  → the site shows a Buy button. A sale — through the link, or rung up on a card reader as the
  catalog item — takes the stock to 0 → Square posts `inventory.count.updated` → the item's status
  becomes "sold" → its checkout link is closed and the site rebuilt.

Sites stay provider-neutral: the workflows store the Buy-button link as `checkout_url` (with
`checkout_provider: square`) and mark `checkout_closed` once sold — keys any commerce integration can
write. Square's own ids stay under `square_*`.

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
    "label": "This Square integration's slug",
    "kind": "text",
    "default": "square",
    "help": "Shown on the integration's card.",
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
    slug="square-list-on-publish",
    name="Square: list on publish",
    description="When an item with Sell online is published, create (or refresh) its Square listing and store the checkout link on it.",
    required=True,
    category=CATEGORY,
    parameters=(ENTRY_TYPE_PARAM, INTEGRATION_PARAM),
    payload={
        "definition": {
            "trigger": {"type": "event", "event": "entry_published"},
            "conditions": [
                {"field": "entry.entry_type", "op": "eq", "value": "{{entry_type}}"},
                {"field": "entry.data.sellOnline", "op": "eq", "value": True},
                {"field": "entry.data.status", "op": "neq", "value": "sold"},
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
    description="When Square reports an item's stock reached 0 — sold online or on a card reader — set its status to sold.",
    required=True,
    category=CATEGORY,
    parameters=(ENTRY_TYPE_PARAM,),
    payload={
        "definition": {
            "trigger": {"type": "incoming_webhook", "webhook": WEBHOOK_SLUG},
            "conditions": [
                {"field": "event.payload.type", "op": "eq", "value": "inventory.count.updated"},
                {"field": "event.payload.data.object.inventory_counts.0.state", "op": "eq", "value": "IN_STOCK"},
                {"field": "event.payload.data.object.inventory_counts.0.quantity", "op": "eq", "value": "0"},
            ],
            "actions": [
                {
                    "kind": "entry",
                    "op": "set_data",
                    "data": {"status": "sold"},
                    "entity_query": {
                        "entry_type": "{{entry_type}}",
                        "metadata": {"square_variation_id": "${event.payload.data.object.inventory_counts.0.catalog_object_id}"},
                    },
                },
            ],
        }
    },
)

CLOSE_WHEN_SOLD = ContentBlueprint(
    kind="workflow",
    slug="square-close-when-sold",
    name="Square: close the link when sold",
    description="When an item's status becomes sold (by a sale or by hand), close its Square checkout link and rebuild the site. Runs once per item.",
    required=True,
    category=CATEGORY,
    parameters=(ENTRY_TYPE_PARAM, INTEGRATION_PARAM),
    payload={
        "definition": {
            "trigger": {"type": "event", "event": "entry_updated"},
            "conditions": [
                {"field": "entry.entry_type", "op": "eq", "value": "{{entry_type}}"},
                {"field": "entry.data.status", "op": "eq", "value": "sold"},
                {"field": "entry.metadata.square_payment_link_id", "op": "exists"},
                # The marker set below: without it, every later edit of a sold item would close and rebuild again.
                {"field": "entry.metadata.checkout_closed", "op": "exists", "value": False},
            ],
            "actions": [
                {
                    "kind": "integration",
                    "integration": "{{integration}}",
                    "action": "close_listing",
                    "args": {"payment_link_id": "${entry.metadata.square_payment_link_id}"},
                },
                {"kind": "entry", "op": "set_metadata", "metadata": {"checkout_closed": True}},
                {"kind": "handler", "task": "request_site_rebuild", "config": {"reason": "square sale"}},
            ],
        }
    },
)

CONTENT = (SHOP_FIELDS, EVENTS_WEBHOOK, LIST_ON_PUBLISH, MARK_SOLD, CLOSE_WHEN_SOLD)
