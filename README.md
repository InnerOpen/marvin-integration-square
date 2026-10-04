# marvin-integration-square

A [Marvin](https://github.com/InnerOpen/marvin) integration that sells **one-of-a-kind items** (an
original painting, say) through Square. Category: *destination*.

Listing an item creates three things in Square:

1. a **catalog item** with one variation, inventory tracked,
2. an **inventory count of exactly 1** at your location,
3. a **payment link** (Square-hosted checkout) whose order points at that catalog variation.

Because the link references the catalog variation rather than an ad-hoc amount, an online sale and
an in-person sale on a Square card reader draw from the same stock of 1.

## Install

On a Marvin host:

```bash
uv pip install marvin-integration-square   # or add to your Marvin image
# restart Marvin → "Square" appears in Settings → Integrations
```

No changes to Marvin core or its frontend. Marvin discovers the provider through the
`marvin.integrations` entry point this package declares.

## Configure

1. In the [Square Developer Console](https://developer.squareup.com/apps), create an application.
   On its **Credentials** page, copy the **sandbox** access token to test with. Use the
   **production** token when you go live. The two are not interchangeable: a sandbox token only
   works against the sandbox environment.
2. In Marvin: **Settings → Integrations → Square → Configure**, paste the token and set
   `environment` to match it (`sandbox` or `production`). Save.
3. Run the **`list_locations`** action and copy the `id` of the location that should hold the stock
   and take the orders into `location_id`. The card turns `ok` once that location is found.
4. Set `redirect_url` to your thank-you page. `{slug}` is replaced with the item's slug, e.g.
   `https://example.com/sold/{slug}`.

## Credential, config & actions

| | |
|---|---|
| **Credential** | `access_token`: Square personal access token or OAuth token |
| **Config** | `environment` (`sandbox` \| `production`, default `sandbox`), `location_id` (required for listing), `currency` (`USD`), `redirect_url` (may contain `{slug}`) |
| **Action** | `list_locations`: `{}` → `{ "locations": [{ "id", "name", "status" }] }` |
| **Action** | `create_listing`: `{ "slug", "name", "price", "shipping_fee"?, "description"?, "variation_id"?, "payment_link_id"? }` → `{ "item_id", "variation_id", "payment_link_id", "checkout_url", "order_id" }` |
| **Action** | `close_listing`: `{ "payment_link_id" }` → `{ "closed": true, "already": bool }` |

**Prices** take a number in dollars (`1170`, `1170.5`) or text as an editor would type it
(`"$1,170"`). The result is converted to integer cents. Zero, negative, or ambiguous prices are
rejected. Text such as `"1.170,00"` or `"$1.170"` fails rather than listing the item at $1.17. A `shipping_fee` above zero is added to the checkout as a flat shipping charge. Blank or
`0` means no shipping charge.

**Re-listing.** Pass the `variation_id` and `payment_link_id` stored from an earlier listing, for
example after a price change. The provider then updates the existing catalog item in place, keeping
its version, images and anything else edited in the Square dashboard. It resets the stock to 1,
deletes the old link (a link that is already gone is fine), and creates a new one. The checkout URL
changes, so store the new one. If the stored variation no longer exists in Square, a new item is
created.

**Closing.** `close_listing` deletes the payment link, and Square cancels the order behind it. A
link that is already gone returns `already: true`. Anything else that fails raises an error.

Every write uses a fresh idempotency key. A key derived from the slug would make Square replay the
first listing's stored response when the item is re-listed at a new price.

### Errors

Every failure raises a `SquareError`. It is a `ValueError`, so Marvin fails the workflow step and
records the run with the message. The message says what was being done and names the item, and the
field when Square says which one, e.g. `Square couldn't save catalog item "Blue Hour" (HTTP 400):
INVALID_VALUE: Invalid currency 'XYZ'. (field price_money)`. Each error also carries a stable `code`.
Marvin copies it to the failed step as `${error.code}`:

| `code` | When | What to do |
|---|---|---|
| `auth` | `AUTHENTICATION_ERROR`, HTTP 401/403: the token is wrong, expired or revoked, lacks a permission, or belongs to the other environment | Fix the connection's token or environment |
| `invalid` | `INVALID_REQUEST_ERROR`, HTTP 400/422, or a price/shipping fee that isn't an amount, or a missing slug/name/price | Fix the item (price, currency, text) |
| `not_found` | Square's `NOT_FOUND`: an object the item points at is gone | Look at the item's stored `square_*` ids |
| `rate_limited` | `RATE_LIMITED`, HTTP 429 | Try again in a few minutes |
| `unavailable` | `API_ERROR`, HTTP 5xx, or Square couldn't be reached (timeout, refused connection) | Try again later |
| `conflict` | `VERSION_MISMATCH` (the item was edited in Square meanwhile), `IDEMPOTENCY_KEY_REUSED`, HTTP 409 | Try again; it re-reads the item first |
| `config` | No access token, no `location_id`, or an unknown `environment`, caught before any call | Finish configuring the connection |
| `unknown` | Anything else, e.g. a response missing the ids Square should return | Read the message |

Square's specific code decides first (`NOT_FOUND` and `VERSION_MISMATCH` are filed under
`INVALID_REQUEST_ERROR`), then its category, then the HTTP status. A 404 when re-listing a vanished
variation, or when closing a link that is already gone, is not an error (see above).

## What a workspace gets — declared, applied from the integration's card

Sites never depend on Square: the Buy button reads the provider-neutral `checkout_url` /
`checkout_closed` metadata, which any commerce integration's workflows can write.

Nothing is created on install. The integration's card lists what it needs; **Apply** creates only
what is missing (Marvin's blueprint contract), and webhooks/workflows arrive **switched off**. Two
parameters: `entry_type` (the type whose entries are for sale, default `artwork`) and `integration`
(this integration's slug in the workspace, default `square`). Declared in `content.py`:

| Kind | Slug | What it does |
|---|---|---|
| fields | `square-shop-fields` | Adds `sellOnline` (boolean), `price` (text) and `shippingFee` (text) to the item type — fields it already has are left alone. |
| incoming webhook | `square-events` | Square posts here. Signature scheme `square`, key in the workspace secret `SQUARE_SIGNATURE_KEY`. |
| workflow | `square-list-for-sale` | Any update of a published item with Sell online on, a price, not sold, and a price/shipping that differs from what it was listed at (`square_listed_for`): `create_listing` → store the ids below → rebuild the site. Covers first listing, switching Sell online on later, and price changes. |
| workflow | `square-mark-sold` | `inventory.count.updated`, `IN_STOCK` quantity `0` → find the item by `square_variation_id` → status `sold`. Covers a sale through the link *and* a card-reader sale rung up as the catalog item. |
| workflow | `square-close-when-sold` | Item status becomes `sold` (by a sale or by hand) → `close_listing` → mark `checkout_closed` → rebuild. Runs once per item. |
| workflow | `square-close-when-withdrawn` | Sell online switched off → `close_listing` → mark `checkout_closed` → rebuild. Switching it back on lists it again. |
| workflow | `square-close-when-unpublished` | Item unpublished → `close_listing` → mark → rebuild. Republishing lists it again. |
| workflow | `square-close-when-archived` | Item archived (e.g. a duplicate retired) → `close_listing` → mark → rebuild. |

Metadata the workflows keep on each item:

| Metadata key | From |
|---|---|
| `square_item_id` | `item_id` |
| `square_variation_id` | `variation_id` (passed back as `variation_id` when re-listing) |
| `square_payment_link_id` | `payment_link_id` (passed back when re-listing, and to `close_listing`) |
| `square_order_id` | `order_id` |
| `checkout_url` | `checkout_url` — the Buy button's link (provider-neutral key a site reads) |
| `checkout_provider` | `square` |
| `checkout_closed` | set once the link is closed (provider-neutral) |

### After Apply

1. Integration card: paste the access token; set `environment`, `location_id` (run `list_locations`) and `redirect_url`.
2. Incoming webhook **Square events**: mint its token; in your Square app add a webhook subscription to
   that URL for `inventory.count.updated`; store the subscription's signature key as the workspace secret
   `SQUARE_SIGNATURE_KEY`; set the webhook's signed URL to the exact URL you gave Square; enable it.
3. An outgoing webhook subscribed to `webhook_triggered` that calls the site's deploy hook (the
   workflows' `request_site_rebuild` step fires it).
4. Review the six workflows and switch them on. Retire an item by archiving it (its link closes); deleting an entry can't close its link.

Webhook signature verification happens in Marvin core. This integration contributes the
`square` signature scheme (base64 HMAC-SHA256 over the notification URL + body, header
`x-square-hmacsha256-signature`) through `signature_schemes`, so it appears in every incoming
webhook's scheme picker while the integration is installed.

## Develop

```bash
uv run --extra dev pytest
uv run --extra dev ruff check .
```

The provider depends only on `marvin-integration-sdk` (0.3+, for `http.delete`), not on Marvin core,
so tests run standalone against a stub HTTP client. No test calls Square. `money.py` is pure Python
and holds the price parsing. The `Square-Version` header is pinned in `provider.py`
(`SQUARE_VERSION`). Bump it deliberately after reading Square's changelog.
