# Square

Sells **one-of-a-kind items** (an original painting, say) through Square. Listing an item creates a Square catalog
item with one variation and a stock of exactly 1, plus a Square-hosted checkout link. The link's order points at that
catalog variation, so an online sale and a sale rung up on a Square card reader draw from the same single piece of
stock: it cannot sell twice. Square is a *destination*: Marvin sends listings out and hears about sales back.

## Connect it

1. In the Square Developer Console create an application. On its **Credentials** page copy the **sandbox** access token
   to test with; use the **production** token to go live. A sandbox token only works against the sandbox environment
   and a production token only against production.
2. In Marvin open **Settings > Integrations > Square > Configure**, paste the token as the **Access token** (a personal
   access token or an OAuth token both work) and set **Environment** to match it (`sandbox` is the default).
3. Run the **List locations** action and copy the `id` of the location that should hold the stock and take the orders
   into **Location ID**. The card turns `ok` once that location is found; if the id isn't one of the account's
   locations the card shows the ones that are, and usually means the environment doesn't match the token.
4. Set **Currency** (ISO 4217, default `USD`) to the location's currency, and **Thank-you page URL** to where buyers
   land after paying. `{slug}` in it is replaced with the item's slug, e.g. `https://example.com/sold/{slug}`.

Nothing is created in Marvin on install. The integration's card lists what a workspace needs (shop fields on the item
type, the `square-events` incoming webhook and six workflows); **Apply** creates only what is missing, and webhooks and
workflows arrive switched off until someone reviews and enables them.

## Actions

**List locations** (`list_locations`): no arguments. Returns `{locations: [{id, name, status}]}`. Run it by hand to
find the `location_id`.

**Create or refresh listing** (`create_listing`): `slug`, `name` and `price` are required.

- `price` is dollars as a number (`1170`, `1170.5`) or text as an editor would type it (`"$1,170"`). It is converted to
  integer cents. Zero, negative and ambiguous prices (`"1.170,00"`, `"$1.170"`) are rejected rather than guessed.
- `shipping_fee` (optional) is a flat shipping charge in dollars added at checkout. Blank or `0` means none.
- `description` (optional) goes on the catalog item. `image_url` (optional) is a public URL of the item's picture
  (JPEG, PNG or GIF up to 15 MB); it is uploaded so the checkout shows it, skipped if the item already has a picture,
  and a picture that can't be fetched never stops the listing.
- `variation_id` and `payment_link_id` are the ids from an earlier listing. Pass them to **re-list**: the existing
  catalog item is updated in place (keeping its version, pictures and anything edited in the Square dashboard), stock
  is set back to 1, the new link is created and only then the old one is deleted, so the item is never left without a
  way to buy it. If the stored variation no longer exists in Square (deleted, or from the other environment), a new
  item is created instead.
- Returns `item_id`, `variation_id`, `payment_link_id`, `checkout_url`, `order_id` and `image_id`. Store them on the
  entry: the next re-list and `close_listing` need `variation_id` and `payment_link_id`, and **the checkout URL changes
  on every re-list**.

**Close listing** (`close_listing`): `payment_link_id` is required; `variation_id` is optional. It sets the stock to 0
first, then deletes the link (Square cancels the order behind it), so a link that can't be deleted has nothing left to
sell. Without `variation_id` only the link is deleted and the stock is left as it is. Returns `closed`, `already`
(the link was already gone) and `stock_zeroed`.

A failed `create_listing` or `close_listing` that got partway reports what it completed; Marvin hands that back when it
retries, so a retry carries on where it stopped and never makes a duplicate catalog item.

## How the sell-artwork flow fits together

The integration's blueprints wire this loop on the item type (default `artwork`, parameter `entry_type`). Sites stay
provider-neutral: the Buy button reads `checkout_url` and `checkout_closed` in the entry's metadata.

1. **Listing.** The *Square: list for sale* workflow runs on any update of a published item with **Sell online** on, a
   price, and a status other than `sold`, whose price or shipping fee differs from what it was last listed at
   (`square_listed_for`, stored as `price|shipping`). It calls `create_listing` with the ids already on the entry,
   stores `square_item_id`, `square_variation_id`, `square_payment_link_id`, `square_order_id` and `checkout_url`
   (with `checkout_provider: square`, `checkout_closed: false`) and requests a site rebuild. It covers a first listing,
   turning Sell online on later, and a price or shipping change.
2. **Sold, online or in person.** Square posts `inventory.count.updated` to the `square-events` incoming webhook. When
   the count for an item's variation is `IN_STOCK` with quantity `0` and the item's checkout isn't already closed, the
   *Square: mark sold* workflow sets its `status` to `sold`. This covers a purchase through the link and a card-reader
   sale rung up as the catalog item. The zero that closing a listing sets is not a sale. Square can send more than one
   event for one sale; only one matches.
3. **Closing.** A separate workflow closes the link when an item becomes `sold` (by a sale or by hand), when Sell online
   is switched off, when it is unpublished, and when it is archived. Each one first marks `checkout_closed: true` and
   rebuilds the site (the Buy button goes whatever Square does next), then runs `close_listing`, then marks
   `square_link_closed: true`. Putting the item back on sale lists it again. Deleting an entry can't close its link:
   archive it instead.
4. **Re-listing everything.** The *Re-list every item for sale on Square* recipe in the Workflow Library overwrites each
   for-sale item's `square_listed_for`, so *Square: list for sale* lists every one again. Use it after switching from
   sandbox to production, or after changing the token or location. An id Square no longer finds is replaced by a new
   item (see `variation_id` above). It needs *Square: list for sale* switched on, and a manual run covers up to 250
   entries, so run it again if there are more.

## Set up the webhook

In the Square Developer Console add a webhook subscription for `inventory.count.updated` pointing at the
`square-events` incoming webhook URL (mint its token in Marvin first). Square shows the subscription's **signature
key**: store it as the workspace secret `SQUARE_SIGNATURE_KEY` and set the webhook's signed URL to the exact URL you
gave Square. Marvin verifies each post with the `square` scheme (base64 HMAC-SHA256 over the notification URL plus the
body, header `x-square-hmacsha256-signature`). Also point an outgoing webhook at the site's deploy hook so the
workflows' site rebuild requests reach the host.

## What it can't do

- It sells exactly one of each item: stock is always 1, never a larger quantity or several variations.
- It doesn't issue refunds, print shipping labels or track delivery.
- It doesn't read Square's orders or payments, only inventory counts through the webhook, and it doesn't create the
  webhook subscription in Square for you.
- It doesn't run an OAuth sign-in: paste a token that already works.
- One connection is one Square account, one environment and one location.
- Taking an item off sale by deleting its entry leaves the link open.

## Errors

Every failure names the item and carries a code. Marvin applies the integration's policy to any workflow step that
calls the action (review, admin alert, retry later); the entry page and Review Queue show it.

| code | Means | What to do |
|---|---|---|
| `auth` | The token is wrong, expired or revoked, lacks a permission, or belongs to the other environment (HTTP 401/403). | Fix the token and check the environment matches it. Marvin alerts admins and retries when the connection is healthy again. |
| `config` | No access token, no `location_id`, or an unknown environment, caught before any call. | Complete the connection settings. Handled like `auth`. |
| `rate_limited` | Square is limiting requests (HTTP 429), often after re-listing many items at once. | Nothing: retried after 1 minute, 5, 15 and an hour, then admins are told. |
| `unavailable` | Square failed on its side (5xx) or couldn't be reached. | Nothing at first: retried over several hours, then admins are told and the entry goes to review. |
| `conflict` | The item was edited in Square meanwhile (version mismatch), an idempotency key clashed, or HTTP 409. | Retried twice after re-reading the item, then sent to review. |
| `invalid` | Square refused the request (HTTP 400/422), or the price or shipping fee isn't an amount, or the slug, name or price is missing. | The entry goes to review: fix the item. |
| `not_found` | An object the item points at is gone from Square. | `create_listing`: sent to review; check the entry's stored `square_*` ids. `close_listing`: counts as closed. |
| `unknown` | Anything else, e.g. a response missing the ids Square should return. | Retried after 5 minutes, then review and an admin alert. |

`list_locations` is run by hand, so its errors are only shown.
