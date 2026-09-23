"""
Priority score for every open order, shown in the orders table so picking
happens oldest/most-urgent first.

Priority score for one order =
    hours it's been waiting
  + a deadline boost, if Shopify gave this order a real ship-by date
    (see DEADLINE_URGENCY_WINDOW_HOURS)

No more "shares a product with other open orders" bonus, and no more
fixed-size active batch / bin pool - every open order is shown, all the
time, just sorted by this score. (Physical "bin" now means a warehouse
storage location per product - see categorize.py / server.py's bucket
building - not a per-order slot.)
"""

import os
from datetime import datetime, timezone

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


def compute_priority_score(order, now=None):
    now = now or datetime.now(timezone.utc)

    created_at = _parse_iso(order.get("created_at"))
    age_hours = _hours_between(now, created_at) if created_at else 0.0

    deadline_boost = 0.0
    fulfill_by = _parse_iso(order.get("fulfill_by"))
    if fulfill_by:
        hours_until_deadline = _hours_between(fulfill_by, now)
        deadline_boost = max(0.0, DEADLINE_URGENCY_WINDOW_HOURS - hours_until_deadline)

    return age_hours + deadline_boost


def score_all(orders_by_id, now=None):
    """{fo_id: score} for every order."""
    now = now or datetime.now(timezone.utc)
    return {fo_id: compute_priority_score(order, now) for fo_id, order in orders_by_id.items()}
