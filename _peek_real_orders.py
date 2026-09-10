"""
Read-only diagnostic - NOT part of the shipped app. Looks at real
unfulfilled, non-POS orders on both stores (no location filter this
time) so we can see: (a) what location they're actually assigned to
right now, and (b) how well their real SKUs/titles categorize using
categorize.py. Doesn't create, change, or fulfill anything.

Run with: python3 _peek_real_orders.py
"""
import shopify_api
import categorize

QUERY = """
query PeekOrders($cursor: String) {
  orders(first: 20, after: $cursor, query: "fulfillment_status:unfulfilled") {
    edges {
      cursor
      node {
        name
        sourceName
        fulfillmentOrders(first: 5) {
          edges {
            node {
              status
              assignedLocation { location { name } }
              lineItems(first: 20) {
                edges {
                  node {
                    sku
                    productTitle
                    remainingQuantity
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

for store in shopify_api.STORES:
    print(f"\n===== {store['label']} ({store['domain']}) =====")
    data = shopify_api.graphql_request(store, QUERY)
    edges = data["orders"]["edges"]
    if not edges:
        print("  (no unfulfilled orders at all right now)")
        continue

    location_counts = {}
    shown = 0
    for edge in edges:
        order = edge["node"]
        source = order.get("sourceName") or "(none)"
        for fo_edge in order["fulfillmentOrders"]["edges"]:
            fo = fo_edge["node"]
            loc_name = ((fo.get("assignedLocation") or {}).get("location") or {}).get("name") or "(none)"
            location_counts[loc_name] = location_counts.get(loc_name, 0) + 1

            if shown < 8:  # only print full detail for the first few, to keep this readable
                print(f"\n  Order {order['name']}  |  channel: {source}  |  fulfillment status: {fo['status']}  |  assigned location: {loc_name}")
                for li_edge in fo["lineItems"]["edges"]:
                    li = li_edge["node"]
                    if li["remainingQuantity"] <= 0:
                        continue
                    sku = li.get("sku") or ""
                    title = li.get("productTitle") or ""
                    cat = categorize.categorize(sku, title)
                    print(f"      - sku={sku!r:35} title={title!r:40} qty={li['remainingQuantity']:<3} -> category: {cat}")
                shown += 1

    print(f"\n  Location breakdown across all fetched fulfillment orders: {location_counts}")
