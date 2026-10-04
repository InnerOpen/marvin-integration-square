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
| **Action** | `create_listing`: `{ "slug", "name", "price", "shipping_fee"?, "description"?, "variation_id"?, "payment_link_id"?, "image_url"? }` → `{ "item_id", "variation_id", "payment_link_id", "checkout_url", "order_id", "image_id" }` |
| **Action** | `close_listing`: `{ "payment_link_id", "variation_id"? }` → `{ "closed": true, "already": bool, "stock_zeroed": bool }` |

**Prices** take a number in dollars (`1170`, `1170.5`) or text as an editor would type it
(`"$1,170"`). The result is converted to integer cents. Zero, negative, or ambiguous prices are
rejected. Text such as `"1.170,00"` or `"$1.170"` fails rather than listing the item at $1.17. A `shipping_fee` above zero is added to the checkout as a flat shipping charge. Blank or
`0` means no shipping charge.

**Re-listing.** Pass the `variation_id` and `payment_link_id` stored from an earlier listing, for
example after a price change. The provider then updates the existing catalog item in place, keeping
its version, images and anything else edited in the Square dashboard. It resets the stock to 1,
creates the new link, and only then deletes the old one (a link that is already gone is fine), so the
item is never left without a way to buy it. The checkout URL changes, so store the new one. If the
stored variation no longer exists in Square, a new item is created.

**Closing.** `close_listing` first sets the variation's stock to 0, then deletes the payment link
(Square cancels the order behind it). Stock goes first so that if the link can't be deleted, it has
nothing left to sell. A link that is already gone returns `already: true`; a variation that is
already gone has no stock to zero. Without a `variation_id` only the link is deleted.

**Partial progress.** Listing is several Square calls, so a failure partway through carries the steps
that completed on the error (`partial`: the catalog item's ids, whether the stock was set, the new
link). Marvin hands that back as `ctx.resume` when it retries, and the retry continues where it
stopped: it reuses the same catalog item (never a duplicate), skips what is done, and a retry that
only has the old link left to delete does just that. If the item's price or text changed in between,
the completed steps are redone on the same catalog item and the link the failed attempt made is
deleted too. Closing works the same way: a retry after the stock was zeroed only deletes the link.

**Idempotency keys** come from `ctx.idempotency_key(...)`: the same on every attempt of one retry
chain for the same request, so a request Square already did (its answer lost to a timeout) is replayed
instead of creating a second item or link. A new chain, or a changed price, gets new keys, so a
re-listing never receives the first listing's stored response. Without a seed from Marvin they are random.

### Errors

Every failure raises a `SquareError`, the SDK's `IntegrationError` (still a `ValueError`). The message
says what was being done and names the item, and the field when Square says which one, e.g. `Square
couldn't save catalog item "Blue Hour" (HTTP 400): INVALID_VALUE: Invalid currency 'XYZ'. (field
price_money)`. It carries a stable `code`, the completed steps as `partial`, and on a 429 Square's
`Retry-After` as `retry_after` (seconds), which Marvin honours when it schedules the retry.

The integration declares how Marvin handles each code (`error_policy`, SDK 0.5). Marvin applies it to
any workflow step that calls the action: *review* flags the item for a person, *notify* alerts the
workspace admins, *retry* tries the step again later (re-checking the workflow's conditions first), and
*then* is what happens once the retries run out. Review, notify and success happen straight away.

| `code` | When | `create_listing` | `close_listing` |
|---|---|---|---|
| `auth` | `AUTHENTICATION_ERROR`, HTTP 401/403: the token is wrong, expired or revoked, lacks a permission, or belongs to the other environment | Notify; retry once when the connection is healthy again; then review | Notify; retry when the connection is healthy again, up to 10×; then review |
| `config` | No access token, no `location_id`, or an unknown `environment`, caught before any call | Same as `auth` | Same as `auth` |
| `rate_limited` | `RATE_LIMITED`, HTTP 429 | Retry after 1m, 5m, 15m, 1h; then notify | Same |
| `unavailable` | `API_ERROR`, HTTP 5xx, or Square couldn't be reached (timeout, refused connection) | Retry after 2m, 10m, 30m, 2h, 6h; then notify and review | Same |
| `conflict` | `VERSION_MISMATCH` (the item was edited in Square meanwhile), `IDEMPOTENCY_KEY_REUSED`, HTTP 409 | Retry after 30s, 2m (it re-reads the item first); then review | Same |
| `invalid` | `INVALID_REQUEST_ERROR`, HTTP 400/422, or a price/shipping fee that isn't an amount, or a missing slug/name/price | Review: fix the item | Review |
| `not_found` | Square's `NOT_FOUND`: an object the item points at is gone | Review: look at the item's stored `square_*` ids | Success: already gone, so closed |
| `unknown` | Anything else, e.g. a response missing the ids Square should return (also any code not listed) | Retry after 5m; then review and notify | Same |

`list_locations` is run by hand, so every code there just fails with the message. Square's specific
code decides first (`NOT_FOUND` and `VERSION_MISMATCH` are filed under `INVALID_REQUEST_ERROR`), then
its category, then the HTTP status. A 404 when re-listing a vanished variation, or when closing a link
that is already gone, is not an error (see above).

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
| workflow | `square-mark-sold` | `inventory.count.updated`, `IN_STOCK` quantity `0` → the item with that `square_variation_id`, unless its checkout is already closed → status `sold`. Covers a sale through the link *and* a card-reader sale rung up as the catalog item; the 0 that closing a listing sets is not a sale. |
| workflow | `square-close-when-sold` | Item status becomes `sold` (by a sale or by hand) → close safely (below). Runs once per item. |
| workflow | `square-close-when-withdrawn` | Sell online switched off → close safely. Switching it back on lists it again. |
| workflow | `square-close-when-unpublished` | Item unpublished → close safely. Republishing lists it again. |
| workflow | `square-close-when-archived` | Item archived (e.g. a duplicate retired) → close safely. |

**Closing safely.** Each close workflow runs four steps, in this order:

1. mark `checkout_closed: true` (and `square_listed_for: closed`, so putting the item back on sale lists it again);
2. request a site rebuild — the Buy button disappears whatever Square does next;
3. `close_listing` — stock to 0, then the link deleted;
4. mark `square_link_closed: true`.

The workflows run while `square_link_closed` isn't true. If step 3 fails, Marvin's retry re-checks
that condition and still finds the link open, even though the site already hides the button; keying on
`checkout_closed` would drop the retry. Each workflow's other conditions describe the item still being
out of the shop (sold, Sell online off, not published, archived), so an item put back on sale meanwhile
drops the pending retry instead of closing its new link.

Metadata the workflows keep on each item:

| Metadata key | From |
|---|---|
| `square_item_id` | `item_id` |
| `square_variation_id` | `variation_id` (passed back as `variation_id` when re-listing) |
| `square_payment_link_id` | `payment_link_id` (passed back when re-listing, and to `close_listing`) |
| `square_order_id` | `order_id` |
| `checkout_url` | `checkout_url` — the Buy button's link (provider-neutral key a site reads) |
| `checkout_provider` | `square` |
| `checkout_closed` | set when the item leaves the shop, before Square is called: hides the Buy button (provider-neutral) |
| `square_link_closed` | set once Square confirms the link is closed; `false` again when the item is re-listed |

### After Apply

1. Integration card: paste the access token; set `environment`, `location_id` (run `list_locations`) and `redirect_url`.
2. Incoming webhook **Square events**: mint its token; in your Square app add a webhook subscription to
   that URL for `inventory.count.updated`; store the subscription's signature key as the workspace secret
   `SQUARE_SIGNATURE_KEY`; set the webhook's signed URL to the exact URL you gave Square; enable it.
3. An outgoing webhook subscribed to `webhook_triggered` that calls the site's deploy hook (the
   workflows' `request_site_rebuild` step fires it).
4. Review the six workflows and switch them on. Retire an item by archiving it (its link closes); deleting an entry can't close its link.

**Upgrading from 0.2.** Apply never changes a workflow that already exists, so the six workflows
(`square-list-for-sale`, `square-mark-sold` and the four close workflows) show as out of date on the
integration's card: click **Update** on each to get the new close order. Items closed under 0.2 have
no `square_link_closed`, so the next edit of a sold or withdrawn one runs its close once more; that is
harmless (the link is already gone, and the stock goes to 0).

Webhook signature verification happens in Marvin core. This integration contributes the
`square` signature scheme (base64 HMAC-SHA256 over the notification URL + body, header
`x-square-hmacsha256-signature`) through `signature_schemes`, so it appears in every incoming
webhook's scheme picker while the integration is installed.

## Develop

```bash
uv run --extra dev pytest
uv run --extra dev ruff check .
```

The provider depends only on `marvin-integration-sdk` (0.5+, for error policies), not on Marvin core,
so tests run standalone against a stub HTTP client. No test calls Square. `money.py` is pure Python
and holds the price parsing. The `Square-Version` header is pinned in `provider.py`
(`SQUARE_VERSION`). Bump it deliberately after reading Square's changelog.
