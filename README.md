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

Square API errors raise a `ValueError` carrying Square's `errors[]` code and detail, so Marvin
records a failed execution with the reason.

## What the site's workflows expect

This provider declares **no workspace content**. The artwork entry type belongs to the site's
workspace, not to Square. The workflows that call it expect these artwork fields:

| Field | Type | Used for |
|---|---|---|
| `sellOnline` | boolean | Only artworks with this set are listed on publish. |
| `shippingFee` | text | Passed as `shipping_fee` (e.g. `"$45"`; blank = no shipping charge). |

They store the `create_listing` output on the entry under these metadata keys:

| Metadata key | From |
|---|---|
| `square_item_id` | `item_id` |
| `square_variation_id` | `variation_id` (passed back as `variation_id` when re-listing) |
| `square_payment_link_id` | `payment_link_id` (passed back when re-listing, and to `close_listing`) |
| `square_order_id` | `order_id` |
| `square_checkout_url` | `checkout_url`, the URL for the Buy button |

## Webhooks (handled by Marvin core)

Marking an item sold is driven by Square webhooks that **Marvin core** receives. This provider has
no webhook handler. Webhook signature verification (`x-square-hmacsha256-signature`) also happens
in core. The workflows use:

- **`payment.updated`** with `status` `COMPLETED`: match the payment's `order_id` against
  `square_order_id`. This covers an online sale through the link.
- **`inventory.count.updated`** with a quantity of `0`: match `catalog_object_id` against
  `square_variation_id`. This covers a sale anywhere, including the card reader.

Either one marks the artwork sold and calls `close_listing` with `square_payment_link_id`.

## Develop

```bash
uv run --extra dev pytest
uv run --extra dev ruff check .
```

The provider depends only on `marvin-integration-sdk` (0.3+, for `http.delete`), not on Marvin core,
so tests run standalone against a stub HTTP client. No test calls Square. `money.py` is pure Python
and holds the price parsing. The `Square-Version` header is pinned in `provider.py`
(`SQUARE_VERSION`). Bump it deliberately after reading Square's changelog.
