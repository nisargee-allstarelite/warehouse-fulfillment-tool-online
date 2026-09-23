"""Exercises the Flask routes with fake in-memory state (no real network,
no real Shopify calls) - checks login, session gating, /api/state's buckets
+ orders shape, toggle-pick, toggle-pick-all, and the packing slip page all
work end to end.
Run with: python3 _selftest_flask.py
"""
import os

os.environ.setdefault("SHOPIFY_CLIENT_ID", "test_client_id")
os.environ.setdefault("SHOPIFY_CLIENT_SECRET", "shpss_test")
os.environ.setdefault("ASE_STORE_DOMAIN", "test-ase.myshopify.com")
os.environ.setdefault("WL_STORE_DOMAIN", "test-wl.myshopify.com")
os.environ.setdefault("ONLINE_LOGIN_PASSWORD", "testpass")
os.environ.setdefault("ONLINE_LOGIN_USERNAME", "online")
os.environ.setdefault("FLASK_SECRET_KEY", "test")

import server

failures = 0


def check(label, condition):
    global failures
    if condition:
        print(f"OK   {label}")
    else:
        failures += 1
        print(f"FAIL {label}")


# Seed two fulfillment orders across both stores, one with a bin name set on
# both its line items, one waiting with no bin name yet (unassigned) plus an
# already-picked item, to exercise both the buckets grid and the orders list.
server.state["fulfillment_orders"] = {
    "gid://shopify/FulfillmentOrder/1": {
        "fo_id": "gid://shopify/FulfillmentOrder/1",
        "order_id": "gid://shopify/Order/1", "order_name": "#1001",
        "store_key": "ASE", "store_label": "All Star Elite",
        "created_at": "2026-09-01T00:00:00Z", "fulfill_by": None, "customer_name": "Jane D.",
        "order_admin_url": "https://test-ase.myshopify.com/admin/orders/1",
        "priority_score": 40,
        "line_items": [
            {"sku": "WTSN026-DNMSHT-06-BLACK-36", "title": "Watson Denim Shorts", "variant_title": "36 / Black", "qty": 1, "bin_name": "A11", "picked": False},
        ],
    },
    "gid://shopify/FulfillmentOrder/2": {
        "fo_id": "gid://shopify/FulfillmentOrder/2",
        "order_id": "gid://shopify/Order/2", "order_name": "#2002",
        "store_key": "WL", "store_label": "Watson Luxe",
        "created_at": "2026-09-01T00:05:00Z", "fulfill_by": None, "customer_name": "Sam R.",
        "order_admin_url": "https://test-wl.myshopify.com/admin/orders/2",
        "priority_score": 5,
        "line_items": [
            {"sku": "WTSN027-DNMSHT-03-BLACK-32", "title": "Watson Denim Shorts", "variant_title": "32 / Black", "qty": 2, "bin_name": "", "picked": False},
            {"sku": "", "title": "JORDAN BASKETBALL JERSEY", "variant_title": "L", "qty": 1, "bin_name": "", "picked": True},
        ],
    },
}

client = server.app.test_client()

# Not logged in -> redirected to login
r = client.get("/")
check("unauthenticated request redirected to /login", r.status_code == 302 and "/login" in r.headers["Location"])

# Wrong password -> rejected
r = client.post("/login", data={"username": "online", "password": "wrong"})
check("wrong password rejected", b"Incorrect username or password" in r.data)

# Correct login
r = client.post("/login", data={"username": "online", "password": "testpass"}, follow_redirects=True)
check("correct login succeeds", r.status_code == 200)

# /api/state - check the new shape: buckets (rows + unassigned) and orders,
# sorted by priority_score, no more batch_size/bins_in_use/waiting.
r = client.get("/api/state")
data = r.get_json()
check("total_orders counts both orders", data["total_orders"] == 2)
check("response has no leftover batch_size field", "batch_size" not in data)
check("response has no leftover bins_in_use field", "bins_in_use" not in data)
check("response has no leftover waiting field", "waiting" not in data)

check("buckets has one row for 'A'", [row["row"] for row in data["buckets"]["rows"]] == ["A"])
cell = data["buckets"]["rows"][0]["cells"][0]
check("bucket cell A11 holds the denim shorts SKU with qty 1", cell["bin_name"] == "A11" and cell["items"][0]["qty"] == 1)
check("unassigned has the two line items with no bin_name yet", len(data["buckets"]["unassigned"]) == 2)

check("orders is sorted by priority_score, highest first",
      [o["order_name"] for o in data["orders"]] == ["#1001", "#2002"])
order2 = next(o for o in data["orders"] if o["order_name"] == "#2002")
check("order #2002 shows 1 of 2 items picked", order2["item_count"] == 2 and order2["picked_count"] == 1)
check("order_admin_url is present for the Label button to link to",
      order2["order_admin_url"] == "https://test-wl.myshopify.com/admin/orders/2")

# toggle_pick the one line item in fo #1
r = client.post("/api/toggle_pick", json={
    "fo_id": "gid://shopify/FulfillmentOrder/1", "line_item_index": 0, "picked": True,
})
check("toggle_pick call succeeds", r.get_json()["status"] == "ok")

r = client.get("/api/state")
data = r.get_json()
order1 = next(o for o in data["orders"] if o["order_name"] == "#1001")
check("toggle_pick marks order #1001 fully picked", order1["all_picked"] is True)

# toggle_pick_all: check-all on order #2 should mark both its line items picked
r = client.post("/api/toggle_pick_all", json={"fo_id": "gid://shopify/FulfillmentOrder/2", "picked": True})
check("toggle_pick_all call succeeds", r.get_json()["status"] == "ok")

r = client.get("/api/state")
data = r.get_json()
order2 = next(o for o in data["orders"] if o["order_name"] == "#2002")
check("toggle_pick_all marks order #2002 fully picked", order2["all_picked"] is True and order2["picked_count"] == 2)

# toggle_pick with an unknown fo_id is rejected
r = client.post("/api/toggle_pick", json={"fo_id": "not-a-real-id", "line_item_index": 0, "picked": True})
check("toggle_pick with an unknown fo_id is rejected (400)", r.status_code == 400)

# packing slip page renders for the selected orders
r = client.get("/packing_slip?fo_ids=" + "gid://shopify/FulfillmentOrder/1,gid://shopify/FulfillmentOrder/2")
check("/packing_slip renders for selected orders", r.status_code == 200)
check("/packing_slip includes both order numbers", b"#1001" in r.data and b"#2002" in r.data)

# packing slip page renders (empty state) when nothing is selected
r = client.get("/packing_slip")
check("/packing_slip renders with no orders selected", r.status_code == 200 and b"No orders selected" in r.data)

# history page renders without error (reconcile UI was removed; read-only now)
r = client.get("/history")
check("/history renders", r.status_code == 200)

r = client.get("/api/shipping_history")
check("/api/shipping_history responds with an entries list", "entries" in r.get_json())

print(f"\n{'ALL FLASK SELFTESTS PASSED' if failures == 0 else f'{failures} FAILURE(S)'}")
if failures:
    raise SystemExit(f"{failures} selftest failure(s)")
