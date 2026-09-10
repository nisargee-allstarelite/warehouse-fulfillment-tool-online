"""
Online (Shopify) Warehouse Fulfillment Dashboard

Polls both Shopify stores' Warehouse-location open orders on a timer,
runs the wave-picking batching logic (batching.py) to decide which 30
orders are "active" right now, assigns each active order a numbered bin,
groups everything into a pick sheet (Category -> SKU -> which bins need
it), and lets you buy real shipping labels for a bin, a range of bins, or
specific bins at once.

This is the Shopify sibling of the existing TikTok warehouse tool
(~/Desktop/warehouse_shipment_management) - same overall architecture
(background poller, state persisted to disk so a crash doesn't lose
anything, async job pattern for the slow/real-money operation so a
request can never time out and get double-submitted), adapted for
Shopify's data model and its own label-purchase API.

Run with: python3 server.py
Leave the terminal window open - closing it stops the poller.
"""

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, request, render_template, send_from_directory, session, redirect, url_for
from dotenv import load_dotenv

import shopify_api
import categorize
import shipping
import batching
from bins import BinPool

load_dotenv()

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", 180))
PORT = int(os.environ.get("PORT", 5001))
STATE_FILE = "state.json"
NOTIFY_CUSTOMER = os.environ.get("NOTIFY_CUSTOMER_ON_LABEL", "false").lower() == "true"
BATCH_SIZE = batching.ACTIVE_BATCH_SIZE  # fixed number of bins/active orders at once (default 30)

LOGIN_USERNAME = os.environ.get("ONLINE_LOGIN_USERNAME", "online")
LOGIN_PASSWORD = os.environ.get("ONLINE_LOGIN_PASSWORD")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY")

if not LOGIN_PASSWORD:
    raise RuntimeError("ONLINE_LOGIN_PASSWORD is not set in .env - the site cannot start without it.")
if not app.secret_key:
    raise RuntimeError("FLASK_SECRET_KEY is not set in .env - the site cannot start without it.")


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
    "fulfillment_orders": {},   # fo_id -> order/bin/line-item dict (both active AND waiting orders live here)
    "last_updated": None,
    "last_error": None,
    "is_polling": False,
}
bin_pool = BinPool(max_bins=BATCH_SIZE)

jobs_lock = threading.Lock()
jobs = {}                    # job_id -> {"status": "running"|"done", "response": {...}}
purchasing_fo_ids = set()    # fo_ids currently mid-purchase - blocks double-submit (NOT the same as "active batch")
reconcile_state = {"in_progress": False}


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                saved = json.load(f)
            state["fulfillment_orders"] = saved.get("fulfillment_orders", {})
            state["last_updated"] = saved.get("last_updated")
            saved_pool = saved.get("bin_pool", {})
            bin_pool.assigned = saved_pool.get("assigned", {})
            bin_pool.free = saved_pool.get("free", [])
            bin_pool.next_new = saved_pool.get("next_new", 1)
        except Exception as e:
            print(f"Could not load saved state: {e}")


def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump({
            "fulfillment_orders": state["fulfillment_orders"],
            "bin_pool": bin_pool.to_dict(),
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
            # elsewhere, or by us) and free its bin, if it had one, for
            # batching.py to hand to the next order below.
            for fo_id in list(existing.keys()):
                if fo_id not in fetched_by_id:
                    bin_pool.release(fo_id)
                    del existing[fo_id]

            # Add new orders / refresh existing ones with current data,
            # preserving "picked" checkmarks where the line items haven't
            # actually changed. Deliberately NOT touching bins here -
            # batching.py decides that next, and never evicts an order
            # that already has one (see its module docstring).
            for fo_id, fo in fetched_by_id.items():
                old_fo = existing.get(fo_id)
                new_line_items = []
                for li in fo["line_items"]:
                    category = categorize.categorize(li["sku"], li["title"])
                    picked = False
                    if old_fo:
                        for old_li in old_fo.get("line_items", []):
                            if _line_items_match(old_li, li):
                                picked = old_li.get("picked", False)
                                break
                    new_line_items.append({**li, "category": category, "picked": picked})

                existing[fo_id] = {
                    "fo_id": fo_id,
                    "order_id": fo["order_id"],
                    "order_name": fo["order_name"],
                    "store_key": fo["store_key"],
                    "store_label": fo["store_label"],
                    "created_at": fo["created_at"],
                    "fulfill_by": fo.get("fulfill_by"),
                    "customer_name": fo["customer_name"],
                    "bin": bin_pool.bin_for(fo_id),  # None until/unless batching.py below gives it one
                    "line_items": new_line_items,
                }

            # --- Wave-picking batching (see batching.py) ---
            # Bins already in use stay exactly as they are. Only newly
            # freed bins (from the release() calls above, or bins that
            # were simply never filled yet) get handed out, to whichever
            # WAITING order currently scores highest.
            currently_active_ids = set(bin_pool.assigned.keys())
            available_slots = BATCH_SIZE - len(currently_active_ids)
            if available_slots > 0:
                newly_active = batching.choose_new_active_orders(existing, currently_active_ids, available_slots)
                for fo_id in newly_active:
                    bin_number = bin_pool.assign(fo_id)
                    existing[fo_id]["bin"] = bin_number

            # Score everyone (for the waiting-queue panel) and record
            # whether each order is in the active batch right now.
            scores = batching.score_all(existing)
            for fo_id, fo in existing.items():
                fo["priority_score"] = round(scores.get(fo_id, 0.0))
                fo["in_batch"] = fo["bin"] is not None

            state["last_updated"] = datetime.now(timezone.utc).isoformat()
            state["is_polling"] = False
            save_state()

        active_count = sum(1 for fo in existing.values() if fo["in_batch"])
        print(f"  Done - {len(fetched_by_id)} open order(s) total, {active_count} active in bins, "
              f"{len(fetched_by_id) - active_count} waiting.")
    except Exception as e:
        print(f"  ERROR during poll: {e}")
        with state_lock:
            state["last_error"] = str(e)
            state["is_polling"] = False


def poll_loop():
    while True:
        poll_once()
        time.sleep(POLL_INTERVAL_SECONDS)


def build_pick_sheet():
    """Category -> SKU -> {title, variant_title, total_qty, bins: [...]}.
    Only ACTIVE (binned) orders appear here - a waiting order's items
    aren't picked yet, so they don't belong on the pick sheet."""
    categories = {}
    for fo in state["fulfillment_orders"].values():
        if not fo["in_batch"]:
            continue
        all_picked = all(li["picked"] for li in fo["line_items"]) if fo["line_items"] else False
        for idx, li in enumerate(fo["line_items"]):
            cat = li["category"]
            sku_key = f"{li['sku']}|{li['variant_title']}"
            cat_bucket = categories.setdefault(cat, {})
            entry = cat_bucket.setdefault(sku_key, {
                "sku": li["sku"], "title": li["title"], "variant_title": li["variant_title"],
                "total_qty": 0, "bins": [],
            })
            entry["total_qty"] += li["qty"]
            entry["bins"].append({
                "bin": fo["bin"], "qty": li["qty"], "fo_id": fo["fo_id"],
                "line_item_index": idx, "order_name": fo["order_name"],
                "store_label": fo["store_label"], "picked": li["picked"],
                "order_all_picked": all_picked,
            })

    return [
        {
            "category": cat,
            "sku_count": len(skus),
            "items": sorted(skus.values(), key=lambda s: -s["total_qty"]),
        }
        for cat, skus in sorted(categories.items())
    ]


def build_bins_summary():
    """Only ACTIVE (binned) orders - one row per bin currently in use."""
    bins = []
    for fo in state["fulfillment_orders"].values():
        if not fo["in_batch"]:
            continue
        item_count = len(fo["line_items"])
        picked_count = sum(1 for li in fo["line_items"] if li["picked"])
        bins.append({
            "bin": fo["bin"], "fo_id": fo["fo_id"], "order_name": fo["order_name"],
            "store_label": fo["store_label"], "customer_name": fo["customer_name"],
            "item_count": item_count, "picked_count": picked_count,
            "all_picked": item_count > 0 and picked_count == item_count,
            "priority_score": fo["priority_score"],
        })
    bins.sort(key=lambda b: b["bin"])
    return bins


def build_waiting_summary(limit=20):
    """Orders NOT yet in the active batch, highest priority score first -
    the "next up" queue, shown read-only on the dashboard for visibility
    into what's coming and why (age vs. deadline vs. product match)."""
    waiting = [fo for fo in state["fulfillment_orders"].values() if not fo["in_batch"]]
    waiting.sort(key=lambda fo: -fo["priority_score"])
    return {
        "count": len(waiting),
        "top": [
            {
                "fo_id": fo["fo_id"], "order_name": fo["order_name"], "store_label": fo["store_label"],
                "created_at": fo["created_at"], "fulfill_by": fo.get("fulfill_by"),
                "priority_score": fo["priority_score"],
            }
            for fo in waiting[:limit]
        ],
    }


@app.route("/")
def index():
    return render_template("index.html", poll_interval=POLL_INTERVAL_SECONDS, batch_size=BATCH_SIZE)


@app.route("/history")
def history_page():
    return render_template("history.html")


@app.route("/api/state")
def api_state():
    with state_lock:
        return jsonify({
            "pick_sheet": build_pick_sheet(),
            "bins": build_bins_summary(),
            "waiting": build_waiting_summary(),
            "total_orders": len(state["fulfillment_orders"]),
            "batch_size": BATCH_SIZE,
            "bins_in_use": bin_pool.in_use_count(),
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
    """Check-all / uncheck-all for every line item in one bin/order."""
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


@app.route("/api/reassign_bin", methods=["POST"])
def api_reassign_bin():
    """Manually move an order to a different bin number. Only works for
    orders already in the active batch - a waiting order has no bin yet
    to move (it'll get one automatically when batching.py picks it)."""
    data = request.get_json()
    fo_id = data.get("fo_id")
    new_bin = data.get("new_bin")

    with state_lock:
        fo = state["fulfillment_orders"].get(fo_id)
        if not fo:
            return jsonify({"error": "Unknown fo_id"}), 400
        try:
            bin_pool.reassign(fo_id, int(new_bin))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        fo["bin"] = bin_pool.bin_for(fo_id)
        save_state()

    return jsonify({"status": "ok", "bin": fo["bin"]})


@app.route("/api/buy_labels", methods=["POST"])
def api_buy_labels():
    """
    Starts buying REAL shipping labels for every order currently sitting
    in the given bin numbers. Spends real money the instant it runs - the
    frontend is responsible for confirming with the person first.

    Body: { "bins": [1, 2, 3, ...] }  (frontend expands a range into a list
    before sending, so this endpoint only ever deals with explicit numbers)

    Returns a job_id immediately; poll /api/job_status/<id> for the
    result, same pattern as /api/reconcile - buying N labels makes N real
    network calls (purchase + poll each), which can take a while. Once a
    purchase succeeds, its bin frees up and the next poll (triggered right
    away, see run_job below) hands that bin to whoever's next in the
    waiting queue.
    """
    data = request.get_json()
    requested_bins = set(data.get("bins") or [])
    if not requested_bins:
        return jsonify({"error": "No bins specified"}), 400

    with state_lock:
        items = [fo for fo in state["fulfillment_orders"].values()
                 if fo["in_batch"] and fo["bin"] in requested_bins]

    if not items:
        return jsonify({"error": "No active orders found in those bins"}), 400

    # Hard block: every order in this request must be fully picked first.
    # Buying a label ships that exact order right now, so a half-picked
    # bin means a wrong/incomplete package goes out - block the whole
    # request rather than silently skip some, so it's never a surprise
    # which ones actually went out.
    not_ready = [it for it in items if not all(li["picked"] for li in it["line_items"])]
    if not_ready:
        names = ", ".join(f"bin #{it['bin']} ({it['order_name']})" for it in not_ready)
        return jsonify({
            "error": f"Not fully picked yet, so nothing was bought: {names}. "
                     f"Check off every item in these bins first, then try again."
        }), 400

    with jobs_lock:
        already_active = [it["order_name"] for it in items if it["fo_id"] in purchasing_fo_ids]
        if already_active:
            return jsonify({
                "error": f"Already buying label(s) for: {', '.join(already_active)}. "
                         f"Please wait for that to finish first."
            }), 409
        for it in items:
            purchasing_fo_ids.add(it["fo_id"])

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {"status": "running", "response": None}

    def run_job():
        try:
            results = shipping.buy_labels_for_bins(items, notify_customer=NOTIFY_CUSTOMER)
            combined_filename, included, skipped = shipping.build_combined_label_pdf(
                results, label_prefix=f"bins_{min(requested_bins)}-{max(requested_bins)}"
            )
            shipping.log_shipping_results(results, combined_filename)

            with state_lock:
                for r in results:
                    if r.get("success"):
                        bin_pool.release(r["fo_id"])
                        state["fulfillment_orders"].pop(r["fo_id"], None)
                save_state()

            response = {
                "results": results,
                "combined_pdf_url": f"/api/labels/{combined_filename}" if combined_filename else None,
                "combined_pdf_page_count": len(included),
                "combined_pdf_skipped": skipped,
            }
            with jobs_lock:
                jobs[job_id] = {"status": "done", "response": response}
        except Exception as e:
            with jobs_lock:
                jobs[job_id] = {"status": "done", "response": {"error": str(e)}}
        finally:
            with jobs_lock:
                for it in items:
                    purchasing_fo_ids.discard(it["fo_id"])
            # Refresh right away so freed bins get refilled from the
            # waiting queue immediately, instead of waiting for the next
            # scheduled poll.
            threading.Thread(target=poll_once, daemon=True).start()

    threading.Thread(target=run_job, daemon=True).start()
    return jsonify({"job_id": job_id, "status": "started"})


@app.route("/api/labels/<path:filename>")
def api_get_label(filename):
    return send_from_directory(shipping.LABELS_DIR, filename, as_attachment=False)


@app.route("/api/shipping_history")
def api_shipping_history():
    search = request.args.get("search", "")
    entries = shipping.get_shipping_history(search=search, limit=300)
    return jsonify({"entries": entries, "count": len(entries)})


@app.route("/api/reconcile", methods=["POST"])
def api_reconcile():
    """Re-checks every fulfillment order whose last logged attempt timed
    out (see shipping.reconcile_pending_purchases for why that's the only
    ambiguous case on Shopify's side). Same async job pattern as buy_labels."""
    with jobs_lock:
        if reconcile_state["in_progress"]:
            return jsonify({"error": "A reconciliation check is already in progress. Please wait for it to finish."}), 409
        reconcile_state["in_progress"] = True

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {"status": "running", "response": None}

    def run_job():
        try:
            result = shipping.reconcile_pending_purchases()
            response = {
                "checked": result["checked"],
                "fixed_count": len(result["fixed"]),
                "fixed": result["fixed"],
                "still_failed": result["still_failed"],
                "combined_pdf_url": f"/api/labels/{result['combined_pdf_filename']}" if result["combined_pdf_filename"] else None,
            }
            with jobs_lock:
                jobs[job_id] = {"status": "done", "response": response}
        except Exception as e:
            with jobs_lock:
                jobs[job_id] = {"status": "done", "response": {"error": str(e)}}
        finally:
            with jobs_lock:
                reconcile_state["in_progress"] = False

    threading.Thread(target=run_job, daemon=True).start()
    return jsonify({"job_id": job_id, "status": "started"})


@app.route("/api/job_status/<job_id>")
def api_job_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job_id"}), 404
    return jsonify(job)


if __name__ == "__main__":
    load_state()
    poller = threading.Thread(target=poll_loop, daemon=True)
    poller.start()
    print(f"Online warehouse dashboard running at http://localhost:{PORT}")
    print(f"Polling every {POLL_INTERVAL_SECONDS} seconds in the background. Active batch size: {BATCH_SIZE}.")
    app.run(host="0.0.0.0", port=PORT, debug=False)
