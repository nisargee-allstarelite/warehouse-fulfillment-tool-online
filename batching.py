"""
Decides which open orders are "active" (holding one of the fixed bins)
and which are still waiting for a bin to free up - and, when a bin does
free up, which waiting order gets it next.

This is the standard warehouse "wave picking" pattern - a priority rule
(ship deadline / order age) combined with a seed/SKU-affinity batching
rule (cluster orders that share products) - simplified for our case:
real warehouses also optimize physical walking distance between aisles,
which doesn't apply here since bins are just numbered slots, not aisles
with variable distances between them.

Priority score for one order =
    hours it's been waiting
  + a deadline boost, if Shopify gave this order a real ship-by date
    (see DEADLINE_URGENCY_WINDOW_HOURS)
  + a bonus for sharing products with other currently-open orders:
      +SKU_MATCH_BONUS_HOURS for each other open order with the exact
        same SKU, or +CATEGORY_MATCH_BONUS_HOURS for each other open
        order in the same category (SKU match takes priority over
        category match for the same pair - no double counting)
      capped at MATCH_BONUS_CAP_HOURS total, so a product cluster can
      jump ahead of slightly-older orders but can never beat something
      that's been waiting far longer.

IMPORTANT: scores only decide who gets the NEXT available bin. An order
that already holds a bin keeps it until it ships - this module never
evicts a bin that's already in use, since its items may already be
physically sitting there mid-pick. See choose_new_active_orders().
"""

import os
from datetime import datetime, timezone

ACTIVE_BATCH_SIZE = int(os.environ.get("ACTIVE_BATCH_SIZE", 30))
SKU_MATCH_BONUS_HOURS = float(os.environ.get("SKU_MATCH_BONUS_HOURS", 3))
CATEGORY_MATCH_BONUS_HOURS = float(os.environ.get("CATEGORY_MATCH_BONUS_HOURS", 1))
MATCH_BONUS_CAP_HOURS = float(os.environ.get("MATCH_BONUS_CAP_HOURS", 20))
DEADLINE_URGENCY_WINDOW_HOURS = float(os.environ.get("DEADLINE_URGENCY_WINDOW_HOURS", 48))


def _parse_iso(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _hours_between(a, b):
    return (a - b).total_seconds() / 3600.0


def _skus(order):
    return {li["sku"] for li in order.get("line_items", []) if li.get("sku")}


def _categories(order):
    return {li["category"] for li in order.get("line_items", [])}


def compute_priority_score(fo_id, orders_by_id, now=None):
    """orders_by_id: {fo_id: order_dict} for every currently OPEN order
    (both active-in-a-bin and waiting) - overlap is checked against all
    of them, since being similar to something currently being picked is
    just as useful as being similar to something else waiting."""
    now = now or datetime.now(timezone.utc)
    order = orders_by_id[fo_id]

    created_at = _parse_iso(order.get("created_at"))
    age_hours = _hours_between(now, created_at) if created_at else 0.0

    deadline_boost = 0.0
    fulfill_by = _parse_iso(order.get("fulfill_by"))
    if fulfill_by:
        hours_until_deadline = _hours_between(fulfill_by, now)
        deadline_boost = max(0.0, DEADLINE_URGENCY_WINDOW_HOURS - hours_until_deadline)

    my_skus = _skus(order)
    my_categories = _categories(order)

    overlap_bonus = 0.0
    for other_id, other in orders_by_id.items():
        if other_id == fo_id:
            continue
        if my_skus & _skus(other):
            overlap_bonus += SKU_MATCH_BONUS_HOURS
        elif my_categories & _categories(other):
            overlap_bonus += CATEGORY_MATCH_BONUS_HOURS
    overlap_bonus = min(overlap_bonus, MATCH_BONUS_CAP_HOURS)

    return age_hours + deadline_boost + overlap_bonus


def score_all(orders_by_id, now=None):
    """{fo_id: score} for every order - used to show the score/ordering
    transparently in the dashboard's waiting-queue panel, and by
    choose_new_active_orders below."""
    now = now or datetime.now(timezone.utc)
    return {fo_id: compute_priority_score(fo_id, orders_by_id, now) for fo_id in orders_by_id}


def choose_new_active_orders(orders_by_id, currently_active_ids, available_slots, now=None):
    """Returns a list of fo_ids (length <= available_slots) - the
    highest-scoring orders NOT already active, to fill newly-freed bins.
    Never touches or reorders currently_active_ids themselves - see the
    module docstring for why bins already in use are never evicted."""
    if available_slots <= 0:
        return []

    waiting_ids = [fo_id for fo_id in orders_by_id if fo_id not in currently_active_ids]
    if not waiting_ids:
        return []

    now = now or datetime.now(timezone.utc)
    scored = [(compute_priority_score(fo_id, orders_by_id, now), fo_id) for fo_id in waiting_ids]
    scored.sort(key=lambda pair: -pair[0])
    return [fo_id for _score, fo_id in scored[:available_slots]]
