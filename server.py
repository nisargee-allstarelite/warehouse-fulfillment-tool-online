"""
Online (Shopify) Warehouse Fulfillment Dashboard

Polls both Shopify stores' Warehouse-location open orders on a timer and
shows two things:

  1. Buckets - a grid of physical warehouse storage locations ("bin name",
     e.g. "A11" = row A, box 11, read from a "custom.bin_name" metafield on
     each product VARIANT in Shopify - see shopify_api.py). Each bucket
     shows what's currently needed from that spot, aggregated across every
     open order. Items whose variant has no bin name set yet show up in an
     "Unassigned" section instead of the grid.

  2. Every open order, all at once (no more fixed-size batch/bin-per-order
     pool), sorted by priority score - order age plus a boost if Shopify
     gave it a real ship-by deadline (see batching.py). Each row lets you
     check off picked items, and its "Label" button just opens that
     order's real Shopify admin page - label purchase now happens there,
     not through this app.

Selected orders (checkboxes + select-all) can be turned into a printable
packing slip (order #, sku + bin, quantity) via /packing_slip.

This is the Shopify sibling of the existing TikTok warehouse tool
(~/Desktop/warehouse_shipment_management) - same overall architecture
(background poller, state persisted to disk so a crash doesn't lose
anything), adapted for Shopify's data model.

Run with: python3 server.py
Leave the terminal window open - closing it stops the poller.
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timezone

from flask import Flask, jsonify, request, render_template, send_from_directory, session, redirect, url_for
from dotenv import load_dotenv

import shopify_api
import batching
import shipping  # only for the read-only Shipping History page now - see /history below

load_dotenv()

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", 180))
PORT = int(os.environ.get("PORT", 5001))
STATE_FILE = "state.json"

LOGIN_USERNAME = os.environ.get("ONLINE_LOGIN_USERNAME", "online")
LOGIN_PASSWORD = os.environ.get("ONLINE_LOGIN_PASSWORD")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY")


class _PrefixMiddleware:
    """Lets this app be reverse-proxied behind a URL path (e.g. Nginx routing
    allstarelite.duckdns.org/online -> this app on its own port), so Flask's
    url_for()-generated links still point to the right place. No-op if
    URL_PREFIX isn't set (e.g. hitting the app directly on its port)."""

    def __init__(self, wsgi_app, prefix):
        self.wsgi_app = wsgi_app
        self.prefix = prefix

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if self.prefix and path.startswith(self.prefix):
            environ["PATH_INFO"] = path[len(self.prefix):] or "/"
            environ["SCRIPT_NAME"] = self.prefix
        return self.wsgi_app(environ, start_response)


URL_PREFIX = os.environ.get("URL_PREFIX", "").rstrip("/")
if URL_PREFIX:
    app.wsgi_app = _PrefixMiddleware(app.wsgi_app, URL_PREFIX)

if not LOGIN_PASSWORD:
    raise RuntimeError("ONLINE_LOGIN_PASSWORD is not set in .env - the site cannot start without it.")
if not app.secret_key:
    raise RuntimeError("FLASK_SECRET_KEY is not set in .env - the site cannot start without it.")


@app.context_processor
def inject_url_prefix():
    # So templates' JS can prefix its own fetch() calls (e.g. "/online/api/state")
    # instead of hardcoding "/api/state", which would break behind the Nginx
    # /online proxy - request.script_root reflects whatever _PrefixMiddleware
    # set above, and is just "" for local/unprefixed dev.
    return {"url_prefix": request.script_root}


@app.before_request
def require_login():
    if request.endpoint in ("login", "static"):
        return
    if not session.get("authenticated"):
        return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("username") == LOGIN_USERNAME and request.form.get("password") == LOGIN_PASSWORD:
            session["authenticated"] = True
            session.permanent = True
            return redirect(url_for("index"))
        error = "Incorrect username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


state_lock = threading.Lock()
state = {
    "fulfillment_orders": {},   # fo_id -> order dict (every open order lives here, no batching)
    "last_updated": None,
    "last_error": None,
    "is_polling": False,
}


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                saved = json.load(f)
            state["fulfillment_orders"] = saved.get("fulfillment_orders", {})
            state["last_updated"] = saved.get("last_updated")
        except Exception as e:
            print(f"Could not load saved state: {e}")


def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump({
            "fulfillment_orders": state["fulfillment_orders"],
            "last_updated": state["last_updated"],
        }, f, indent=2)


def _line_items_match(old, new):
    return old.get("sku") == new.get("sku") and old.get("variant_title") == new.get("variant_title") and old.get("qty") == new.get("qty")


def poll_once():
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] Polling both Shopify stores for open Warehouse orders...")
    with state_lock:
        state["is_polling"] = True
        state["last_error"] = None

    try:
        fetched = shopify_api.fetch_all_open_warehouse_fulfillment_orders()
        fetched_by_id = {f["fo_id"]: f for f in fetched}

        with state_lock:
            existing = state["fulfillment_orders"]

            # Drop anything no longer open (shipped/cancelled/fulfilled
            # elsewhere).
            for fo_id in list(existing.keys()):
                if fo_id not in fetched_by_id:
                    del existing[fo_id]

            # Add new orders / refresh existing ones with current data,
            # preserving "picked" checkmarks where the line items haven't
            # actually changed.
            for fo_id, fo in fetched_by_id.items():
                old_fo = existing.get(fo_id)
                new_line_items = []
                for li in fo["line_items"]:
                    picked = False
                    if old_fo:
                        for old_li in old_fo.get("line_items", []):
                            if _line_items_match(old_li, li):
                                picked = old_li.get("picked", False)
                                break
                    new_line_items.append({**li, "picked": picked})

                existing[fo_id] = {
                    "fo_id": fo_id,
                    "order_id": fo["order_id"],
                    "order_name": fo["order_name"],
                    "store_key": fo["store_key"],
                    "store_label": fo["store_label"],
                    "created_at": fo["created_at"],
                    "fulfill_by": fo.get("fulfill_by"),
                    "customer_name": fo["customer_name"],
                    "order_admin_url": fo["order_admin_url"],
                    "line_items": new_line_items,
                }

            scores = batching.score_all(existing)
            for fo_id, fo in existing.items():
                fo["priority_score"] = round(scores.get(fo_id, 0.0))

            state["last_updated"] = datetime.now(timezone.utc).isoformat()
            state["is_polling"] = False
            save_state()

        print(f"  Done - {len(fetched_by_id)} open order(s).")
    except Exception as e:
        print(f"  ERROR during poll: {e}")
        with state_lock:
            state["last_error"] = str(e)
            state["is_polling"] = False


def poll_loop():
    while True:
        poll_once()
        time.sleep(POLL_INTERVAL_SECONDS)


_BIN_NAME_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


def _parse_bin_name(bin_name):
    """'A11' -> ('A', 11). Anything that doesn't match that row-letter(s) +
    box-number shape (blank, or free-text someone typed wrong) is treated
    as unassigned rather than guessed at."""
    if not bin_name:
        return None
    m = _BIN_NAME_RE.match(bin_name)
    if not m:
        return None
    return m.group(1), int(m.group(2))


def build_buckets():
    """Groups every open order's line items by physical bin_name (warehouse
    location), aggregating quantity needed per exact SKU+variant at each
    location across ALL open orders - not just unpicked ones, same as the
    old pick sheet did, so a bucket's total reflects everything currently
    assigned there regardless of pick progress.

    Returns {
      "rows": [ { "row": "A", "cells": [ { "bin_name": "A1", "row": "A",
                   "box": 1, "items": [ {sku, name, size, qty}, ... ] }, ... ]
                }, ... ],  # rows sorted A-Z, cells sorted by box number
      "unassigned": [ {sku, name, size, qty}, ... ],  # no bin_name set yet
    }
    """
    # row -> box -> sku_key -> item
    rows = {}
    unassigned = {}

    for fo in state["fulfillment_orders"].values():
        for li in fo["line_items"]:
            sku_key = f"{li['sku']}|{li['variant_title']}"
            parsed = _parse_bin_name(li.get("bin_name"))
            if parsed is None:
                bucket = unassigned.setdefault(sku_key, {
                    "sku": li["sku"], "name": li["title"], "size": li["variant_title"], "qty": 0,
                })
                bucket["qty"] += li["qty"]
                continue

            row, box = parsed
            box_bucket = rows.setdefault(row, {}).setdefault(box, {})
            item = box_bucket.setdefault(sku_key, {
                "sku": li["sku"], "name": li["title"], "size": li["variant_title"], "qty": 0,
            })
            item["qty"] += li["qty"]

    return {
        "rows": [
            {
                "row": row,
                "cells": [
                    {
                        "bin_name": f"{row}{box}",
                        "row": row,
                        "box": box,
                        "items": sorted(items.values(), key=lambda it: it["sku"]),
                    }
                    for box, items in sorted(boxes.items())
                ],
            }
            for row, boxes in sorted(rows.items())
        ],
        "unassigned": sorted(unassigned.values(), key=lambda it: it["sku"]),
    }


def build_orders_list():
    """Every open order, highest priority_score first."""
    orders = list(state["fulfillment_orders"].values())
    orders.sort(key=lambda fo: -fo["priority_score"])

    result = []
    for fo in orders:
        item_count = len(fo["line_items"])
        picked_count = sum(1 for li in fo["line_items"] if li["picked"])
        result.append({
            "fo_id": fo["fo_id"],
            "order_name": fo["order_name"],
            "store_label": fo["store_label"],
            "order_admin_url": fo["order_admin_url"],
            "created_at": fo["created_at"],
            "fulfill_by": fo.get("fulfill_by"),
            "priority_score": fo["priority_score"],
            "line_items": fo["line_items"],
            "item_count": item_count,
            "picked_count": picked_count,
            "all_picked": item_count > 0 and picked_count == item_count,
        })
    return result


@app.route("/")
def index():
    return render_template("index.html", poll_interval=POLL_INTERVAL_SECONDS)


@app.route("/history")
def history_page():
    return render_template("history.html")


@app.route("/api/shipping_history")
def api_shipping_history():
    """Read-only - past label purchases made back when this app bought
    labels directly (kept for the record; new purchases now happen in
    Shopify itself via the order page, so nothing writes to this log
    anymore)."""
    search = request.args.get("search", "")
    entries = shipping.get_shipping_history(search=search, limit=300)
    return jsonify({"entries": entries, "count": len(entries)})


@app.route("/api/labels/<path:filename>")
def api_get_label(filename):
    return send_from_directory(shipping.LABELS_DIR, filename, as_attachment=False)


@app.route("/api/state")
def api_state():
    with state_lock:
        return jsonify({
            "buckets": build_buckets(),
            "orders": build_orders_list(),
            "total_orders": len(state["fulfillment_orders"]),
            "last_updated": state["last_updated"],
            "is_polling": state["is_polling"],
            "last_error": state["last_error"],
            "poll_interval_seconds": POLL_INTERVAL_SECONDS,
        })


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    threading.Thread(target=poll_once, daemon=True).start()
    return jsonify({"status": "refresh started"})


@app.route("/api/toggle_pick", methods=["POST"])
def api_toggle_pick():
    """Marks one line item (fo_id + line_item_index) picked/unpicked."""
    data = request.get_json()
    fo_id = data.get("fo_id")
    index = data.get("line_item_index")
    picked = bool(data.get("picked"))

    with state_lock:
        fo = state["fulfillment_orders"].get(fo_id)
        if not fo or index is None or index >= len(fo["line_items"]):
            return jsonify({"error": "Unknown fo_id or line_item_index"}), 400
        fo["line_items"][index]["picked"] = picked
        save_state()

    return jsonify({"status": "ok"})


@app.route("/api/toggle_pick_all", methods=["POST"])
def api_toggle_pick_all():
    """Check-all / uncheck-all for every line item in one order."""
    data = request.get_json()
    fo_id = data.get("fo_id")
    picked = bool(data.get("picked"))

    with state_lock:
        fo = state["fulfillment_orders"].get(fo_id)
        if not fo:
            return jsonify({"error": "Unknown fo_id"}), 400
        for li in fo["line_items"]:
            li["picked"] = picked
        save_state()

    return jsonify({"status": "ok"})


@app.route("/packing_slip")
def packing_slip():
    """Printable packing slip for the selected orders (?fo_ids=a,b,c - each
    URI-encoded, since fo_ids are Shopify GIDs like gid://shopify/...).
    For each order: order #, then each line item's sku + bin name + qty."""
    raw = request.args.get("fo_ids", "")
    fo_ids = [x for x in raw.split(",") if x]

    with state_lock:
        orders = [state["fulfillment_orders"][fo_id] for fo_id in fo_ids if fo_id in state["fulfillment_orders"]]

    return render_template("packing_slip.html", orders=orders)


if __name__ == "__main__":
    load_state()
    poller = threading.Thread(target=poll_loop, daemon=True)
    poller.start()
    print(f"Online warehouse dashboard running at http://localhost:{PORT}")
    print(f"Polling every {POLL_INTERVAL_SECONDS} seconds in the background.")
    app.run(host="0.0.0.0", port=PORT, debug=False)
