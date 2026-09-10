"""Exercises the Flask routes with fake in-memory state (no real network,
no real Shopify calls) - checks login, session gating, the wave-picking
batching (active vs waiting), pick-sheet/bins building, toggle-pick, and
reassign-bin all work end to end.
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
# Small batch size for this test, so it's easy to exercise "waiting" too.
os.environ.setdefault("ACTIVE_BATCH_SIZE", "2")

import server

failures = 0


def check(label, condition):
    global failures
    if condition:
        print(f"OK   {label}")
    else:
        failures += 1
        print(f"FAIL {label}")


# Seed THREE fulfillment orders across both stores with overlapping SKUs,
# but ACTIVE_BATCH_SIZE=2 - so one must end up waiting. Manually assign
# bins to the first two (as if a previous poll had already run) and leave
# the third with no bin, to check the dashboard shows it as "waiting".
server.bin_pool.assign("gid://shopify/FulfillmentOrder/1")
server.bin_pool.assign("gid://shopify/FulfillmentOrder/2")

server.state["fulfillment_orders"] = {
    "gid://shopify/FulfillmentOrder/1": {
        "fo_id": "gid://shopify/FulfillmentOrder/1",
        "order_id": "gid://shopify/Order/1", "order_name": "#1001",
        "store_key": "ASE", "store_label": "All Star Elite",
        "created_at": "2026-09-01T00:00:00Z", "fulfill_by": None, "customer_name": "Jane D.",
        "bin": 1, "in_batch": True, "priority_score": 10.0,
        "line_items": [
            {"sku": "WTSN026-DNMSHT-06-BLACK-36", "title": "Watson Denim Shorts", "variant_title": "36 / Black", "qty": 1, "category": "Denim Shorts", "picked": False},
        ],
    },
    "gid://shopify/FulfillmentOrder/2": {
        "fo_id": "gid://shopify/FulfillmentOrder/2",
        "order_id": "gid://shopify/Order/2", "order_name": "#2002",
        "store_key": "WL", "store_label": "Watson Luxe",
        "created_at": "2026-09-01T00:05:00Z", "fulfill_by": None, "customer_name": "Sam R.",
        "bin": 2, "in_batch": True, "priority_score": 9.0,
        "line_items": [
            {"sku": "WTSN027-DNMSHT-03-BLACK-32", "title": "Watson Denim Shorts", "variant_title": "32 / Black", "qty": 2, "category": "Denim Shorts", "picked": False},
            {"sku": "", "title": "JORDAN BASKETBALL JERSEY", "variant_title": "L", "qty": 1, "category": "Basketball Jersey", "picked": False},
        ],
    },
    "gid://shopify/FulfillmentOrder/3": {
        "fo_id": "gid://shopify/FulfillmentOrder/3",
        "order_id": "gid://shopify/Order/3", "order_name": "#3003",
        "store_key": "ASE", "store_label": "All Star Elite",
        "created_at": "2026-09-01T00:10:00Z", "fulfill_by": None, "customer_name": "Lee K.",
        "bin": None, "in_batch": False, "priority_score": 1.0,
        "line_items": [
            {"sku": "WTSN026-HOOD-01-BLACK-M", "title": "Watson Hoodie", "variant_title": "M / Black", "qty": 1, "category": "Hoodie", "picked": False},
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

# /api/state - check pick sheet + bins only include ACTIVE orders, and
# the waiting order shows up in the waiting queue instead.
r = client.get("/api/state")
data = r.get_json()
check("total_orders counts all three (active + waiting)", data["total_orders"] == 3)
check("batch_size reflects ACTIVE_BATCH_SIZE=2", data["batch_size"] == 2)
check("bins_in_use is 2", data["bins_in_use"] == 2)

denim = next(c for c in data["pick_sheet"] if c["category"] == "Denim Shorts")
check("pick sheet's Denim Shorts groups the two ACTIVE bins' SKUs", denim["sku_count"] == 2)
check("pick sheet never includes the waiting order's Hoodie category",
      not any(c["category"] == "Hoodie" for c in data["pick_sheet"]))

check("bins summary only lists the 2 active bins", len(data["bins"]) == 2 and {b["bin"] for b in data["bins"]} == {1, 2})
check("waiting queue lists the 1 waiting order", data["waiting"]["count"] == 1 and data["waiting"]["top"][0]["order_name"] == "#3003")

# toggle_pick the one line item in fo #1 -> its bin should become "ready"
r = client.post("/api/toggle_pick", json={
    "fo_id": "gid://shopify/FulfillmentOrder/1", "line_item_index": 0, "picked": True,
})
check("toggle_pick call succeeds", r.get_json()["status"] == "ok")

r = client.get("/api/state")
data = r.get_json()
bin1 = next(b for b in data["bins"] if b["bin"] == 1)
check("toggle_pick marks bin #1 as fully picked / ready", bin1["all_picked"] is True)

# reassign_bin: move fo #2 from bin 2 to bin... there's no free bin (both
# in use) except bin 2 itself is fo #2's own bin - so instead verify the
# waiting order (#3, no bin yet) correctly CANNOT be reassigned.
r = client.post("/api/reassign_bin", json={"fo_id": "gid://shopify/FulfillmentOrder/3", "new_bin": 1})
check("reassigning a WAITING order (no bin yet) is rejected", r.status_code == 400)

# reassign fo #1 onto fo #2's occupied bin -> rejected
r = client.post("/api/reassign_bin", json={"fo_id": "gid://shopify/FulfillmentOrder/1", "new_bin": 2})
check("reassign onto an occupied bin is rejected", r.status_code == 400)

# buy_labels hard block: bin #2 (fo #2) still has an unpicked item (its
# jersey line item was never toggled) - buying its label must be blocked
# entirely, with a clear message, and must NOT start a purchase job.
r = client.post("/api/buy_labels", json={"bins": [2]})
check("buying a label for a not-fully-picked bin is blocked (400)", r.status_code == 400)
check("block message names the bin so it's obvious what to finish", "bin #2" in r.get_json().get("error", ""))

# history page renders without error
r = client.get("/history")
check("/history renders", r.status_code == 200)

print(f"\n{'ALL FLASK SELFTESTS PASSED' if failures == 0 else f'{failures} FAILURE(S)'}")
if failures:
    raise SystemExit(f"{failures} selftest failure(s)")
