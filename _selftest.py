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
from bins import BinPool

failures = 0


def check(label, condition):
    global failures
    if condition:
        print(f"OK   {label}")
    else:
        failures += 1
        print(f"FAIL {label}")


# --- categorize.py, against real SKUs/titles from Nisargee's export screenshot ---
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

# --- bins.py (fixed pool, max_bins=5 for this test) ---
pool = BinPool(max_bins=5)
b1 = pool.assign("fo1")
b2 = pool.assign("fo2")
b3 = pool.assign("fo3")
check("first three assigns get bins 1,2,3", [b1, b2, b3] == [1, 2, 3])

pool.release("fo2")
b4 = pool.assign("fo4")
check("freed bin 2 gets reused for the next assign", b4 == 2)

try:
    pool.reassign("fo1", b3)  # bin 3 is taken by fo3 - should raise
    check("reassign onto an occupied bin raises", False)
except ValueError:
    check("reassign onto an occupied bin raises", True)

pool.reassign("fo1", 5)
check("reassign moves fo1 to bin 5", pool.bin_for("fo1") == 5)
check("fo1's old bin (1) is now free", 1 in pool.free)

try:
    pool.reassign("fo3", 99)  # out of range for max_bins=5
    check("reassign beyond max_bins raises", False)
except ValueError:
    check("reassign beyond max_bins raises", True)

pool.assign("fo5")  # bin 1 (freed)
pool.assign("fo6")  # last remaining free slot
check("pool reports full at max_bins", not pool.has_capacity())
try:
    pool.assign("fo7")
    check("assign() past capacity raises", False)
except RuntimeError:
    check("assign() past capacity raises", True)

# --- batching.py ---
now = datetime.now(timezone.utc)


def make_order(fo_id, hours_old, skus, category="Denim Shorts", fulfill_by=None):
    return {
        "fo_id": fo_id,
        "created_at": (now - timedelta(hours=hours_old)).isoformat(),
        "fulfill_by": fulfill_by,
        "line_items": [{"sku": sku, "category": category} for sku in skus],
    }


# Older order should score higher than a much younger, unrelated order.
orders = {
    "old_lonely": make_order("old_lonely", hours_old=50, skus=["SKU-A"]),
    "new_lonely": make_order("new_lonely", hours_old=1, skus=["SKU-B"]),
}
scores = batching.score_all(orders, now=now)
check("plain age: older lonely order scores higher than a new lonely one",
      scores["old_lonely"] > scores["new_lonely"])

# A cluster of matching SKUs should let a younger order catch up to (but
# per the cap, not blow past) a much older, unrelated order.
orders2 = {
    "old_unrelated": make_order("old_unrelated", hours_old=100, skus=["SKU-Z"]),
    "young_1": make_order("young_1", hours_old=1, skus=["SKU-MATCH"]),
    "young_2": make_order("young_2", hours_old=1, skus=["SKU-MATCH"]),
    "young_3": make_order("young_3", hours_old=1, skus=["SKU-MATCH"]),
}
scores2 = batching.score_all(orders2, now=now)
check("matching cluster boosts young orders above their own bare age",
      scores2["young_1"] > 1.0)
check("but the bonus cap still keeps a far-older unrelated order on top",
      scores2["old_unrelated"] > scores2["young_1"])

# A real ship-by deadline should raise priority even for a young order.
orders3 = {
    "no_deadline": make_order("no_deadline", hours_old=2, skus=["SKU-C"]),
    "urgent_deadline": make_order("urgent_deadline", hours_old=2, skus=["SKU-D"],
                                   fulfill_by=(now - timedelta(hours=1)).isoformat()),  # already overdue
}
scores3 = batching.score_all(orders3, now=now)
check("an overdue ship-by deadline raises priority over a plain young order",
      scores3["urgent_deadline"] > scores3["no_deadline"])

# choose_new_active_orders: never touches already-active orders, fills
# only the available slots, picks highest scores first.
pool_orders = {
    "active_1": make_order("active_1", hours_old=1, skus=["X"]),   # already active despite being young
    "wait_old": make_order("wait_old", hours_old=40, skus=["Y"]),
    "wait_new": make_order("wait_new", hours_old=1, skus=["Z"]),
}
chosen = batching.choose_new_active_orders(pool_orders, currently_active_ids={"active_1"}, available_slots=1, now=now)
check("choose_new_active_orders picks the highest-scoring WAITING order (not the active one)",
      chosen == ["wait_old"])

chosen_none = batching.choose_new_active_orders(pool_orders, currently_active_ids={"active_1"}, available_slots=0, now=now)
check("choose_new_active_orders returns nothing when there are no free slots", chosen_none == [])

print(f"\n{'ALL PASSED' if failures == 0 else f'{failures} FAILURE(S)'}")

# --- import server.py (checks it wires together without crashing) ---
import server  # noqa: E402
check("server.py imported cleanly and BATCH_SIZE is wired from batching.py",
      server.BATCH_SIZE == batching.ACTIVE_BATCH_SIZE)
print("bins in use after test moves:", server.bin_pool.in_use_count())

if failures:
    raise SystemExit(f"{failures} selftest failure(s)")
