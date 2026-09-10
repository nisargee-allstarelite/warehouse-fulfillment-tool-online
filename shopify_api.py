"""
Shared Shopify Admin API functions - GraphQL requests against BOTH stores
(All Star Elite and Watson Luxe), warehouse-location lookup, fetching open
warehouse orders (as FulfillmentOrders, not raw Orders - see note below),
and purchasing real shipping labels via Shopify Shipping.

Credentials are loaded from .env (see .env.example) - never hardcoded.

Shopify retired the old "create a Custom App, get a static token" flow for
new apps. Since both stores are Nisargee's own (same Shopify organization),
this uses the client credentials grant instead: one shared Client ID +
Client Secret (from the app's Dev Dashboard page), exchanged on demand for
a short-lived (24h) access token per store - no merchant install/OAuth
redirect needed. See _get_access_token() below; it fetches and caches a
token per store, refreshing automatically a few minutes before it expires,
so nothing above this file ever has to think about it.

IMPORTANT / needs live verification once real credentials + a real store
are available (flagged in README too): the exact field names returned by
ShippingLabelPurchaseResult / ShippingLabel below are taken from Shopify's
published docs, but this code has never been run against a live store
from here (no store access in this environment). Test the label-purchase
path on ONE order first before trusting it on a whole batch.
"""

import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

API_VERSION = "2026-07"

CLIENT_ID = os.environ.get("SHOPIFY_CLIENT_ID")
CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET")


def _store_config(prefix, label):
    domain = os.environ.get(f"{prefix}_STORE_DOMAIN")
    location_id = os.environ.get(f"{prefix}_WAREHOUSE_LOCATION_ID")  # optional override
    return {
        "key": prefix,
        "label": label,
        "domain": domain,
        "warehouse_location_id": location_id,
    }


STORES = [
    _store_config("ASE", "All Star Elite"),
    _store_config("WL", "Watson Luxe"),
]

_missing = [f"{s['key']}_STORE_DOMAIN" for s in STORES if not s["domain"]]
if not CLIENT_ID or not CLIENT_SECRET:
    _missing.append("SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET")
if _missing:
    raise RuntimeError(
        f"Missing required environment variables for: {', '.join(_missing)}. "
        f"Copy .env.example to .env and fill in your real values."
    )

# Cached once found - {store_key: location_gid}
_warehouse_location_cache = {}

# Access tokens from the client credentials grant expire after 24h (Shopify's
# own fixed limit for this grant type - see .env.example). Cached per store
# as {"token": ..., "expires_at": epoch_seconds}, refreshed automatically a
# few minutes before expiry so a request never gets caught using a dead one.
_token_cache = {}
_TOKEN_REFRESH_BUFFER_SECONDS = 300


def _get_access_token(store):
    cached = _token_cache.get(store["key"])
    if cached and cached["expires_at"] > time.time():
        return cached["token"]

    url = f"https://{store['domain']}/admin/oauth/access_token"
    resp = requests.post(
        url,
        data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    token = payload["access_token"]
    expires_in = payload.get("expires_in", 3600)
    _token_cache[store["key"]] = {
        "token": token,
        "expires_at": time.time() + expires_in - _TOKEN_REFRESH_BUFFER_SECONDS,
    }
    return token


def graphql_request(store, query, variables=None):
    url = f"https://{store['domain']}/admin/api/{API_VERSION}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": _get_access_token(store),
        "Content-Type": "application/json",
    }
    resp = requests.post(url, json={"query": query, "variables": variables or {}}, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data and data["errors"]:
        raise RuntimeError(f"[{store['label']}] GraphQL error: {data['errors']}")
    return data["data"]


LOCATIONS_QUERY = """
query GetLocations {
  locations(first: 50) {
    edges { node { id name } }
  }
}
"""


def get_warehouse_location_id(store):
    """Finds the location whose name contains 'warehouse' (case-insensitive),
    unless {PREFIX}_WAREHOUSE_LOCATION_ID is set in .env to override/skip
    the name-based lookup entirely. Cached per store for the life of the
    process - call refresh_warehouse_locations() if a location gets
    renamed while the server is running (unlikely, but possible)."""
    if store["warehouse_location_id"]:
        return store["warehouse_location_id"]

    cached = _warehouse_location_cache.get(store["key"])
    if cached:
        return cached

    data = graphql_request(store, LOCATIONS_QUERY)
    locations = [edge["node"] for edge in data["locations"]["edges"]]
    matches = [loc for loc in locations if "warehouse" in loc["name"].lower()]

    if not matches:
        names = ", ".join(loc["name"] for loc in locations) or "(no locations found)"
        raise RuntimeError(
            f"[{store['label']}] Couldn't find a location with 'warehouse' in its name. "
            f"Locations found: {names}. Set {store['key']}_WAREHOUSE_LOCATION_ID in .env "
            f"to point at the right one directly (find its ID in Settings > Locations)."
        )
    if len(matches) > 1:
        names = ", ".join(loc["name"] for loc in matches)
        raise RuntimeError(
            f"[{store['label']}] More than one location matches 'warehouse': {names}. "
            f"Set {store['key']}_WAREHOUSE_LOCATION_ID in .env to disambiguate."
        )

    location_id = matches[0]["id"]
    _warehouse_location_cache[store["key"]] = location_id
    return location_id


OPEN_ORDERS_QUERY = """
query GetOpenOrders($cursor: String, $filterQuery: String!) {
  orders(first: 50, after: $cursor, query: $filterQuery) {
    edges {
      cursor
      node {
        id
        name
        createdAt
        sourceName
        customer { firstName lastName }
        fulfillmentOrders(first: 5) {
          edges {
            node {
              id
              status
              fulfillBy
              assignedLocation { location { id name } }
              lineItems(first: 100) {
                edges {
                  node {
                    id
                    sku
                    productTitle
                    remainingQuantity
                    lineItem { variantTitle }
                  }
                }
              }
            }
          }
        }
      }
    }
    pageInfo { hasNextPage }
  }
}
"""


def fetch_open_warehouse_fulfillment_orders(store):
    """
    Returns a list of dicts, one per OPEN fulfillment order assigned to
    this store's Warehouse location, from a non-POS sales channel. This is
    the actual unit of work (not the parent Order) - Shopify's own
    shippingLabelPurchase mutation operates on a fulfillment order, and an
    order can in principle be split across multiple locations, so scoping
    everything to the fulfillment order handles that correctly too.

    Each dict: {
        "fo_id": "...", "order_id": "...", "order_name": "#1023",
        "store_key": "ASE", "store_label": "All Star Elite",
        "created_at": "...", "customer_name": "Jane D.",
        "line_items": [{"sku": "...", "title": "...", "variant_title": "...", "qty": 2}, ...],
    }

    Excludes POS orders only (sourceName == "pos") - every other sales
    channel (Shopney, the online store, etc.) is included, per Nisargee's
    "online includes everything except POS" rule.
    """
    warehouse_location_id = get_warehouse_location_id(store)
    results = []
    cursor = None

    # TEST_ONLY_NAMES (optional): when set (comma-separated order names,
    # e.g. "12468991,12469021,WL235411,WL235421"), only those exact orders
    # are fetched - used to hide real orders during testing so only the
    # test orders show up, regardless of when new real orders come in.
    # Unset it (or remove the line from .env) to go back to normal.
    filter_query = "fulfillment_status:unfulfilled"
    test_only_names = os.environ.get("TEST_ONLY_NAMES")
    if test_only_names:
        names = [n.strip() for n in test_only_names.split(",") if n.strip()]
        name_filter = " OR ".join(f"name:{n}" for n in names)
        filter_query += f" AND ({name_filter})"

    while True:
        data = graphql_request(store, OPEN_ORDERS_QUERY, {"cursor": cursor, "filterQuery": filter_query})
        orders_page = data["orders"]
        edges = orders_page["edges"]

        for edge in edges:
            order = edge["node"]
            source_name = (order.get("sourceName") or "").lower()
            if source_name == "pos":
                continue

            customer = order.get("customer") or {}
            customer_name = " ".join(
                p for p in [customer.get("firstName"), customer.get("lastName")] if p
            ).strip()

            for fo_edge in order["fulfillmentOrders"]["edges"]:
                fo = fo_edge["node"]
                if fo["status"] != "OPEN":
                    continue
                assigned_location = (fo.get("assignedLocation") or {}).get("location") or {}
                if assigned_location.get("id") != warehouse_location_id:
                    continue

                line_items = []
                for li_edge in fo["lineItems"]["edges"]:
                    li = li_edge["node"]
                    if li["remainingQuantity"] <= 0:
                        continue
                    line_items.append({
                        "sku": li.get("sku") or "",
                        "title": li.get("productTitle") or "",
                        "variant_title": (li.get("lineItem") or {}).get("variantTitle") or "",
                        "qty": li["remainingQuantity"],
                    })

                if not line_items:
                    continue

                results.append({
                    "fo_id": fo["id"],
                    "order_id": order["id"],
                    "order_name": order["name"],
                    "store_key": store["key"],
                    "store_label": store["label"],
                    "created_at": order["createdAt"],
                    "fulfill_by": fo.get("fulfillBy"),  # real ship-by deadline, if Shopify set one - used by batching.py
                    "customer_name": customer_name,
                    "line_items": line_items,
                })

        if not orders_page["pageInfo"]["hasNextPage"] or not edges:
            break
        cursor = edges[-1]["cursor"]

    return results


def fetch_all_open_warehouse_fulfillment_orders():
    """Convenience: pulls both stores and returns one combined list."""
    all_results = []
    for store in STORES:
        all_results.extend(fetch_open_warehouse_fulfillment_orders(store))
    return all_results


def _store_by_key(store_key):
    for s in STORES:
        if s["key"] == store_key:
            return s
    raise ValueError(f"Unknown store_key: {store_key}")


PURCHASE_LABEL_MUTATION = """
mutation PurchaseLabel($input: ShippingLabelPurchaseInput!) {
  shippingLabelPurchase(shippingLabelPurchase: $input) {
    shippingLabelPurchaseResult {
      id
      status
    }
    userErrors { field message }
  }
}
"""

# Confirmed against Shopify's real 2026-07 schema (shipping-label test on a
# live order) - ShippingLabel itself has no "url"/"trackingNumber" fields;
# the PDF lives under shippingDocuments[].url and the tracking number under
# trackingInfo.number.
POLL_LABEL_RESULT_QUERY = """
query PollLabelResult($id: ID!) {
  node(id: $id) {
    ... on ShippingLabelPurchaseResult {
      id
      status
      errors { message }
      shippingLabels {
        id
        trackingInfo { number url company }
        shippingDocuments { url documentType }
      }
    }
  }
}
"""


def purchase_shipping_label(store_key, fulfillment_order_id, shipping_datetime_iso, notify_customer=False):
    """
    Starts a real label purchase for one fulfillment order using Shopify
    Shipping's own default rate selection (deliberately not overriding
    package/weight - same philosophy as the TikTok tool: trust the
    platform's defaults, verify on real orders before assuming more control
    is needed). Returns the ShippingLabelPurchaseResult id to poll, plus
    its initial status. Raises on a synchronous validation error
    (userErrors) - those mean the label was NOT started, nothing to poll.
    """
    store = _store_by_key(store_key)
    variables = {
        "input": {
            "fulfillmentOrderId": fulfillment_order_id,
            "shippingDatetime": shipping_datetime_iso,
            "notifyCustomer": notify_customer,
        }
    }
    data = graphql_request(store, PURCHASE_LABEL_MUTATION, variables)
    payload = data["shippingLabelPurchase"]
    if payload["userErrors"]:
        messages = "; ".join(e["message"] for e in payload["userErrors"])
        raise RuntimeError(messages)
    result = payload["shippingLabelPurchaseResult"]
    return result["id"], result["status"]


def poll_shipping_label_result(store_key, result_id, timeout_seconds=90, interval_seconds=2):
    """
    Polls a ShippingLabelPurchaseResult until it reaches PURCHASED or
    PURCHASE_FAILED (or times out - treated the same as a failure, but
    with a distinct message so it's obvious this was a timeout, not a
    real rejection, when someone's reading Shipping History later).

    Returns (success, data) where data is either
      {"tracking_number": ..., "label_url": ...}   on success, or
      {"error": "..."}                              on failure.
    """
    store = _store_by_key(store_key)
    deadline = time.time() + timeout_seconds

    while True:
        data = graphql_request(store, POLL_LABEL_RESULT_QUERY, {"id": result_id})
        node = data.get("node") or {}
        status = node.get("status")

        if status == "PURCHASED":
            labels = node.get("shippingLabels") or []
            if not labels:
                return False, {"error": "Marked PURCHASED but no label was returned - check manually."}
            label = labels[0]
            tracking_info = label.get("trackingInfo") or {}
            documents = label.get("shippingDocuments") or []
            # Prefer a document explicitly typed as the printable label; fall
            # back to the first document if that type isn't present.
            label_doc = next((d for d in documents if d.get("documentType") == "LABEL"), None) \
                or (documents[0] if documents else None)
            if not label_doc or not label_doc.get("url"):
                return False, {"error": "Marked PURCHASED but no downloadable label document was returned - check manually."}
            return True, {
                "tracking_number": tracking_info.get("number"),
                "label_url": label_doc["url"],
            }

        if status == "PURCHASE_FAILED":
            errors = node.get("errors") or []
            message = "; ".join(e.get("message", "") for e in errors) or "Purchase failed (no error detail returned)."
            return False, {"error": message}

        if time.time() > deadline:
            return False, {"error": f"Timed out after {timeout_seconds}s waiting for purchase to finish (still {status})."}

        time.sleep(interval_seconds)
