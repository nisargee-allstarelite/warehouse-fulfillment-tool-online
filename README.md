# Online Orders (Shopify) Warehouse Fulfillment Dashboard

Live dashboard that polls both Shopify stores (All Star Elite, Watson
Luxe) for open orders assigned to the Warehouse location, assigns each
one a numbered bin, groups everything into a pick sheet (Category -> SKU
-> which bins need it), and lets you buy real shipping labels for a bin,
a range of bins, or specific bins at once.

This is the Shopify sibling of the existing TikTok warehouse tool
(`~/Desktop/warehouse_shipment_management`) - same overall shape
(background poller, state saved to disk so a crash doesn't lose
anything, a start-a-job/poll-for-it pattern for the slow real-money
operation so a request can never time out and get double-submitted), but
built specifically around Shopify's own data and its own label-purchase
API rather than reusing TikTok's code directly.

## Setup

```bash
# 1. Create a virtual environment
python3 -m venv venv
source venv/bin/activate        # on Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set up your credentials
cp .env.example .env
# then open .env and fill in your real Shopify values for BOTH stores

# 4. Run it
python3 server.py
```

Open **http://localhost:5001** (note: different port from the TikTok
tool, so both can run on the same machine at once if you ever want to).
Leave the terminal running - that's what keeps the background poller
alive. Log in with the username/password you set in `.env`
(`ONLINE_LOGIN_USERNAME` / `ONLINE_LOGIN_PASSWORD` - defaults to username
`online`), separate from the TikTok tool's login.

## Getting Shopify credentials

Shopify retired the old "Custom App -> static token" flow for new apps.
Since ASE and WL are both Nisargee's own stores (same Shopify
organization), this tool uses the **client credentials grant** instead -
the officially supported path for an app that only acts on your own
org's stores, with no merchant install/OAuth redirect needed:

1. Create an app in the [Dev Dashboard](https://dev.shopify.com/dashboard) (or reuse one)
2. On the app's **Versions** tab, create a new version with these Admin
   API scopes (comma-separated): `read_orders,read_fulfillments,write_fulfillments,read_locations,read_products,read_customers,read_merchant_managed_fulfillment_orders,write_merchant_managed_fulfillment_orders`
   (note: requesting a `write_x` scope automatically includes the matching
   `read_x` access, so Shopify's token response may only list the `write_`
   ones - that's expected, not missing anything)

   **Important:** use `read_merchant_managed_fulfillment_orders` /
   `write_merchant_managed_fulfillment_orders`, NOT
   `read_assigned_fulfillment_orders` / `write_assigned_fulfillment_orders`.
   The "assigned" scopes are only for apps registered as a third-party
   fulfillment service; since the Warehouse is a normal, merchant-owned
   location, the wrong scope here doesn't error - `fulfillmentOrders`
   just silently comes back empty for every order, which is a well-known
   Shopify gotcha (confirmed against Shopify's community forum).
3. Click **Release** - since this is your own org's store, it applies
   immediately, no separate merchant approval step
4. Install the app on **both** ASE and WL from the same Dev Dashboard app
5. On the app's Credentials page, copy the **Client ID** and **Secret**
   (the secret starts with `shpss_`) - these are shared across both
   stores, since it's one app installed twice
6. For each store, find its real API domain: Shopify admin -> Settings ->
   Domains -> the one tagged "Shopify managed domain", ending in
   `.myshopify.com`. **This will not necessarily match the store's name**
   (e.g. ASE's turned out to be a random string, not "allstarelite") -
   don't assume, go check it
7. **Shopify Shipping must already be turned on** for both stores (the
   real "Buy shipping label" button on an order page) - the
   `shippingLabelPurchase` API this tool uses only works if the shop has
   accepted Shopify Shipping's terms of service.

`shopify_api.py` exchanges the Client ID + Secret for a real, short-lived
(24h) access token per store automatically, and refreshes it before it
expires - see `.env.example` for exactly which variables to set.

## Project structure

```
.
├── server.py           Flask app + background poller + API routes
├── shopify_api.py       Shopify Admin GraphQL - both stores, order fetch, label purchase
├── categorize.py         Category lookup (reuses the TikTok tool's category
│                          tables, but points them at Shopify's clean SKU/
│                          product-title fields instead of parsing notes)
├── bins.py               Bin pool - assigns/frees/reassigns bin numbers
├── shipping.py           Buys labels, builds combined PDF, shipping history log, reconcile
├── templates/
│   ├── index.html         Dashboard: pick sheet + bins (auto-refreshing)
│   ├── history.html       Shipping History page
│   └── login.html         Login (own username/password)
├── .env.example
├── .env                   Your real credentials (gitignored, never commit)
├── requirements.txt
├── labels/                Combined label PDFs land here (gitignored)
└── state.json             Auto-generated cache of orders/bins (gitignored)
```

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `SHOPIFY_CLIENT_ID` / `SHOPIFY_CLIENT_SECRET` | yes | Shared app credentials from the Dev Dashboard - used to fetch a real access token per store automatically |
| `ASE_STORE_DOMAIN` | yes | All Star Elite's real `.myshopify.com` domain (Settings > Domains) |
| `WL_STORE_DOMAIN` | yes | Watson Luxe's real `.myshopify.com` domain (Settings > Domains) |
| `ASE_WAREHOUSE_LOCATION_ID` / `WL_WAREHOUSE_LOCATION_ID` | no | Only needed if name-matching "warehouse" is ambiguous or undesired |
| `ONLINE_LOGIN_USERNAME` | no (default `online`) | This tool's own login username |
| `ONLINE_LOGIN_PASSWORD` | yes | This tool's own login password |
| `FLASK_SECRET_KEY` | yes | Random string for session signing |
| `POLL_INTERVAL_SECONDS` | no (default 180) | How often to re-poll both stores |
| `PORT` | no (default 5001) | Local server port |
| `NOTIFY_CUSTOMER_ON_LABEL` | no (default false) | Whether Shopify emails the customer when a label is bought |

## How bins + batching work

There are a fixed 30 bins active at once (`ACTIVE_BATCH_SIZE` in `.env`).
Which 30 orders get a bin - and which waiting order gets the next one
the moment a bin frees up - is decided by `batching.py`, using the same
approach real warehouses call "wave picking": every open order gets a
priority score, and the top 30 scores are active.

Score = hours the order has been waiting, + extra weight if Shopify gave
it a real ship-by deadline, + a bonus for sharing products with other
open orders (same SKU counts more than just same category), capped so a
product cluster can jump ahead of slightly-older orders but can never
beat something that's been waiting far longer. This is a simplified,
standard "seed algorithm + priority rule" - the kind used in real WMS
wave planning - simplified because we don't have aisle/travel-distance
to optimize for, just bins.

Important: an order that already has a bin keeps it until it actually
ships - the score only decides who gets the NEXT bin, never evicts one
already in use (its items might already be sitting there mid-pick). The
dashboard's "Waiting queue" panel shows who's next and why (age vs.
deadline vs. product match), so the ordering is never a mystery. Bins can
still be moved manually if two orders need to swap physical spots.

## Known limitations / things to verify on a real order before trusting this at scale

- **The exact fields Shopify returns for a purchased label (the PDF URL,
  tracking number) are taken from Shopify's published docs, not
  confirmed against a real response** - this environment has no live
  Shopify store access to test against. Buy ONE label for a real test
  order first and check `shopify_api.py`'s `POLL_LABEL_RESULT_QUERY` /
  `poll_shipping_label_result()` still line up before trusting a bigger
  batch.
- **Whether a purchased label automatically marks the fulfillment order
  as fulfilled on Shopify's side is assumed, not confirmed.** If it
  doesn't, add an explicit `fulfillmentCreate` call after a successful
  purchase in `shipping.py`.
- **Package/weight defaults**: this deliberately doesn't specify a
  package or weight (same philosophy as the TikTok tool - trust the
  platform's own defaults, verify on real orders before assuming more
  control is needed). If Shopify Shipping needs a package type
  configured first, that'll show up as an error on the very first real
  purchase attempt.
- Matching "picked" checkmarks across a re-poll is done by comparing
  SKU + variant + quantity - if an order's line items change shape
  between polls (rare), its checkmarks reset rather than risk showing a
  stale/wrong picked state.
- The "warehouse location" is found by matching "warehouse" in a
  location's name. If a store's location is actually named something
  else, set `ASE_WAREHOUSE_LOCATION_ID` / `WL_WAREHOUSE_LOCATION_ID`
  directly instead.

## Deploying later

Same as the TikTok tool - already reads `PORT` from the environment and
binds to `0.0.0.0`, so it's ready to deploy as-is to Render, Railway,
Fly.io, etc. Set the same environment variables in the hosting
platform's dashboard instead of a local `.env` file.
