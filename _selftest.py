"""Quick sanity check - not part of the shipped app. Run with:
python3 _selftest.py
"""
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("SHOPIFY_CLIENT_ID", "test_client_id")
os.environ.setdefault("SHOPIFY_CLIENT_SECRET", "shpss_test")
os.environ.setdefault("ASE_STORE_DOMAIN", "test-ase.myshopify.com")
os.environ.setdefault("WL_STORE_DOMAIN", "test-wl.myshopify.com")
os.environ.setdefault("ONLINE_LOGIN_PASSWORD", "test")
os.environ.setdefault("FLASK_SECRET_KEY", "test")

import categorize
import batching

failures = 0


def check(label, condition):
    global failures
    if condition:
        print(f"OK   {label}")
    else:
        failures += 1
        print(f"FAIL {label}")


# --- categorize.py, against real SKUs/titles from Nisargee's export screenshot ---
# (categorize.py is no longer used by server.py, but it's harmless leftover -
# kept working and tested in case it's wired back in somewhere.)
cases = [
    ("WTSN026-CRDNMSHT-01-KHAKI-38", "", "Crochet Denim Shorts"),
    ("WTSN026-CRODNMSHT-01-KHAKI-40", "", "Crochet Denim Shorts"),
    ("WTSN026-CUTSHT-07-BLACK-XL", "", "Cutoff Tshirt"),
    ("WTSN026-DNMSHT-06-BLACK-36", "", "Denim Shorts"),
    ("WTSN027-DNMSHT-03-BLACK-32", "", "Denim Shorts"),
    ("DENIM-CHAIN-C-6-OS", "Pearls of Icelyn Pant Chain / One Size", "Denim Chain"),
    ("SOME-UNKNOWN-CODE-1", "WATSON BONES SNEAKERS (BUMBLEBEE) - Size 11.5", "Bones"),
    ("", "JORDAN BASKETBALL JERSEY", "Basketball Jersey"),
    ("", "totally unrecognized item name", categorize.OTHERS),
]
for sku, title, expected in cases:
    got = categorize.categorize(sku, title)
    check(f"categorize sku={sku!r} title={title!r} -> {got!r}", got == expected)

# --- batching.py: simple age + deadline-boost priority score ---
now = datetime.now(timezone.utc)


def make_order(fo_id, hours_old, fulfill_by=None):
    return {
        "fo_id": fo_id,
        "created_at": (now - timedelta(hours=hours_old)).isoformat(),
        "fulfill_by": fulfill_by,
    }


# Older order should score higher than a much younger order - no more
# SKU/category overlap bonus, so age (plus deadline) is the whole story.
orders = {
    "old": make_order("old", hours_old=50),
    "new": make_order("new", hours_old=1),
}
scores = batching.score_all(orders, now=now)
check("plain age: older order scores higher than a newer one",
      scores["old"] > scores["new"])

check("compute_priority_score matches score_all for the same order",
      batching.compute_priority_score(orders["old"], now=now) == scores["old"])

# A real ship-by deadline should raise priority even for a young order.
orders2 = {
    "no_deadline": make_order("no_deadline", hours_old=2),
    "urgent_deadline": make_order("urgent_deadline", hours_old=2,
                                   fulfill_by=(now - timedelta(hours=1)).isoformat()),  # already overdue
}
scores2 = batching.score_all(orders2, now=now)
check("an overdue ship-by deadline raises priority over a plain young order",
      scores2["urgent_deadline"] > scores2["no_deadline"])

# A distant future deadline (outside the urgency window) shouldn't boost at all.
orders3 = {
    "no_deadline": make_order("no_deadline", hours_old=2),
    "far_deadline": make_order("far_deadline", hours_old=2,
                                fulfill_by=(now + timedelta(days=30)).isoformat()),
}
scores3 = batching.score_all(orders3, now=now)
check("a far-off deadline (outside the urgency window) gives no boost",
      scores3["far_deadline"] == scores3["no_deadline"])

# Missing created_at shouldn't blow up - just scores as age 0.
orphan_score = batching.compute_priority_score({"fo_id": "x", "created_at": None, "fulfill_by": None}, now=now)
check("an order with no created_at doesn't crash and scores 0", orphan_score == 0.0)

print(f"\n{'ALL PASSED' if failures == 0 else f'{failures} FAILURE(S)'}")

# --- import server.py (checks it wires together without crashing) ---
import server  # noqa: E402

# --- server.py: _parse_bin_name ---
check("_parse_bin_name splits 'A11' into row A, box 11", server._parse_bin_name("A11") == ("A", 11))
check("_parse_bin_name handles multi-letter rows like 'AB3'", server._parse_bin_name("AB3") == ("AB", 3))
check("_parse_bin_name treats blank as unassigned", server._parse_bin_name("") is None)
check("_parse_bin_name treats None as unassigned", server._parse_bin_name(None) is None)
check("_parse_bin_name treats malformed text as unassigned", server._parse_bin_name("garbage") is None)
check("_parse_bin_name treats a bare number as unassigned (no row letter)", server._parse_bin_name("11") is None)

# --- server.py: build_buckets / build_orders_list, against fake in-memory state ---
server.state["fulfillment_orders"] = {
    "fo1": {
        "fo_id": "fo1", "order_id": "gid://shopify/Order/1", "order_name": "#1001",
        "store_key": "ASE", "store_label": "All Star Elite",
        "created_at": (now - timedelta(hours=40)).isoformat(), "fulfill_by": None,
        "customer_name": "Jane D.", "order_admin_url": "https://ase.myshopify.com/admin/orders/1",
        "priority_score": 40,
        "line_items": [
            {"sku": "SKU-A", "title": "Denim Shorts", "variant_title": "36 / Black", "qty": 2, "bin_name": "A11", "picked": False},
        ],
    },
    "fo2": {
        "fo_id": "fo2", "order_id": "gid://shopify/Order/2", "order_name": "#2002",
        "store_key": "WL", "store_label": "Watson Luxe",
        "created_at": (now - timedelta(hours=5)).isoformat(), "fulfill_by": None,
        "customer_name": "Sam R.", "order_admin_url": "https://wl.myshopify.com/admin/orders/2",
        "priority_score": 5,
        "line_items": [
            {"sku": "SKU-A", "title": "Denim Shorts", "variant_title": "36 / Black", "qty": 1, "bin_name": "A11", "picked": False},
            {"sku": "SKU-B", "title": "Jersey", "variant_title": "L", "qty": 1, "bin_name": "", "picked": True},
        ],
    },
}

buckets = server.build_buckets()
check("build_buckets creates one row for 'A'", [r["row"] for r in buckets["rows"]] == ["A"])
a_row = buckets["rows"][0]
check("build_buckets creates one cell for box 11", [c["bin_name"] for c in a_row["cells"]] == ["A11"])
a11_items = a_row["cells"][0]["items"]
check("A11's SKU-A quantity is aggregated across both orders (2 + 1 = 3)",
      a11_items[0]["sku"] == "SKU-A" and a11_items[0]["qty"] == 3)
check("the item with no bin_name lands in unassigned",
      len(buckets["unassigned"]) == 1 and buckets["unassigned"][0]["sku"] == "SKU-B")

orders_list = server.build_orders_list()
check("build_orders_list sorts by priority_score, highest first",
      [o["order_name"] for o in orders_list] == ["#1001", "#2002"])
fo2_result = next(o for o in orders_list if o["order_name"] == "#2002")
check("build_orders_list counts picked items correctly (1 of 2 picked)",
      fo2_result["item_count"] == 2 and fo2_result["picked_count"] == 1 and fo2_result["all_picked"] is False)
fo1_result = next(o for o in orders_list if o["order_name"] == "#1001")
check("build_orders_list marks an order all_picked only when every item is picked",
      fo1_result["all_picked"] is False)
check("build_orders_list carries order_admin_url through for the Label button",
      fo1_result["order_admin_url"] == "https://ase.myshopify.com/admin/orders/1")

if failures:
    raise SystemExit(f"{failures} selftest failure(s)")
