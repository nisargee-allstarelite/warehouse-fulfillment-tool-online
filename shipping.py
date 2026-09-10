"""
Bulk label-buying for the Shopify warehouse tool - mirrors the TikTok
tool's shipping.py in spirit (async-safe, permanent history log, combined
PDF, reconcile for anything left ambiguous) but the underlying mechanics
differ because Shopify's API is fundamentally different from TikTok's:

  - TikTok: 3 manual steps (Create Package / Batch Ship / Get Document),
    each a real network call, and Batch Ship has a hard 50-item limit that
    can silently succeed on Shopify's/TikTok's side while the response
    still looks like a failure to us - that's why TikTok needs a
    "reconcile against real status" button.
  - Shopify: ONE mutation per fulfillment order (shippingLabelPurchase),
    which is itself already async - we start it, then poll the SAME
    result id until it reaches PURCHASED or PURCHASE_FAILED. No batch
    limit, so no silent-partial-success class of bug. The only genuinely
    ambiguous case here is a poll that times out before Shopify finishes -
    reconcile handles exactly that, by re-polling the SAME result id
    (never re-purchasing, which would risk a second real label).

IMPORTANT: this spends real money the moment a purchase starts. There is
no dry-run mode. See shopify_api.py's module docstring for what's flagged
as unverified against a live store.
"""

import json
import os
import time
import requests
from datetime import datetime, timezone
from io import BytesIO
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

import shopify_api

LABELS_DIR = "labels"
SHIPPING_LOG_FILE = "shipping_history.jsonl"


def _stamp_bin_number(page, text):
    """Overlays a small text stamp (e.g. 'BIN #3 - #1023 (All Star Elite)')
    in the corner of one label page, sized to match that page's own
    dimensions (label PDFs are usually 4x6in, not standard letter size) -
    so it prints right on the label itself, no separate sheet to lose."""
    width = float(page.mediabox.width)
    height = float(page.mediabox.height)
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))
    c.setFont("Helvetica-Bold", 10)
    c.setFillColorRGB(0, 0, 0)
    c.drawString(6, height - 14, text)
    c.save()
    buf.seek(0)
    overlay_page = PdfReader(buf).pages[0]
    page.merge_page(overlay_page)
    return page


def buy_labels_for_bins(items, notify_customer=False, progress_callback=None):
    """
    Runs the full purchase pipeline for a list of fulfillment-order items
    (bucket-item dicts from server.py's state - each needs at least fo_id,
    store_key, order_name, bin). Every item gets exactly one result dict,
    success or failure, and nothing is ever retried automatically within
    one call (retrying create-a-label automatically would risk a second
    real purchase for the same order).

    Returns a list of:
    {
        "fo_id": "...", "order_name": "#1023", "store_label": "...", "bin": 7,
        "success": True/False,
        "error": "...",                (only if success is False)
        "purchase_result_id": "...",   (present once the purchase started -
                                         kept even on failure/timeout, so
                                         reconcile can re-poll instead of
                                         re-purchasing)
        "tracking_number": "...",      (present once purchased)
        "label_url": "...",            (present once purchased)
    }
    """
    def report(msg):
        if progress_callback:
            progress_callback(msg)

    results = []
    shipping_datetime = datetime.now(timezone.utc).isoformat()

    for item in items:
        fo_id = item["fo_id"]
        report(f"Purchasing label for {item.get('order_name', fo_id)}...")
        result = {
            "fo_id": fo_id,
            "order_name": item.get("order_name", ""),
            "store_key": item.get("store_key", ""),
            "store_label": item.get("store_label", ""),
            "bin": item.get("bin"),
            "success": False,
        }
        try:
            result_id, _status = shopify_api.purchase_shipping_label(
                item["store_key"], fo_id, shipping_datetime, notify_customer=notify_customer
            )
            result["purchase_result_id"] = result_id
        except Exception as e:
            result["error"] = str(e)
            results.append(result)
            continue

        time.sleep(0.2)  # small pacing buffer between purchase calls
        success, data = shopify_api.poll_shipping_label_result(item["store_key"], result_id)
        if success:
            result["success"] = True
            result["tracking_number"] = data.get("tracking_number")
            result["label_url"] = data.get("label_url")
        else:
            result["error"] = data.get("error", "Unknown error")

        results.append(result)

    return results


def build_combined_label_pdf(results, label_prefix="bins"):
    """
    Same idea as the TikTok tool's version: download every successfully
    purchased label and merge into ONE multi-page PDF saved permanently to
    LABELS_DIR, so packing can print everything for this run in one go.
    Returns (filename, included_order_names, skipped) - filename is None
    if there was nothing successful to combine.
    """
    os.makedirs(LABELS_DIR, exist_ok=True)

    writer = PdfWriter()
    included = []
    skipped = []

    for r in results:
        if not r.get("success") or not r.get("label_url"):
            continue
        try:
            resp = requests.get(r["label_url"], timeout=20)
            resp.raise_for_status()
            reader = PdfReader(BytesIO(resp.content))
            # No text stamp on the label itself - it overlapped the carrier's
            # own printed text. Match bin-to-order using the order number
            # shown in the dashboard instead.
            for page in reader.pages:
                writer.add_page(page)
            included.append(r["order_name"])
        except Exception as e:
            skipped.append({"order_name": r.get("order_name"), "error": str(e)})

    if not included:
        return None, included, skipped

    safe_prefix = "".join(c if c.isalnum() else "_" for c in label_prefix)[:40]
    filename = f"labels_{safe_prefix}_{int(time.time())}.pdf"
    filepath = os.path.join(LABELS_DIR, filename)
    with open(filepath, "wb") as f:
        writer.write(f)

    return filename, included, skipped


def log_shipping_results(results, combined_pdf_filename=None):
    """Permanently appends every result to a JSON-Lines log (never
    overwritten) - powers the Shipping History page."""
    with open(SHIPPING_LOG_FILE, "a") as f:
        for r in results:
            entry = dict(r)
            entry["combined_pdf_filename"] = combined_pdf_filename
            entry["logged_at"] = datetime.now(timezone.utc).isoformat()
            f.write(json.dumps(entry) + "\n")


def get_shipping_history(search="", limit=300):
    """Returns logged results, most recent first. Search matches order
    name, fo_id, tracking number, or store label."""
    if not os.path.exists(SHIPPING_LOG_FILE):
        return []

    entries = []
    with open(SHIPPING_LOG_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if search:
        q = search.lower()

        def matches(e):
            haystack = " ".join(str(e.get(k, "")) for k in
                                 ("order_name", "fo_id", "tracking_number", "store_label"))
            return q in haystack.lower()

        entries = [e for e in entries if matches(e)]

    entries.reverse()
    return entries[:limit]


def _find_results_needing_reconciliation():
    """Walk the log and return the most-recent entry for every fo_id whose
    latest record shows failure AND has a purchase_result_id (meaning a
    purchase genuinely started - most likely a poll timeout, not a
    synchronous rejection, since a rejection never gets a result_id at
    all). An order that failed once but later succeeded is correctly
    excluded (last occurrence wins)."""
    if not os.path.exists(SHIPPING_LOG_FILE):
        return []

    last_entry = {}
    with open(SHIPPING_LOG_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            fo_id = entry.get("fo_id")
            if fo_id:
                last_entry[fo_id] = entry

    return [e for e in last_entry.values() if not e.get("success") and e.get("purchase_result_id")]


def reconcile_pending_purchases():
    """
    Re-polls (never re-purchases) every fulfillment order whose most recent
    Shipping History entry shows a failure that still has a
    purchase_result_id - almost always a poll timeout rather than a real
    rejection. Safe to re-run any time.

    Returns:
    {
        "checked": int,
        "fixed": [order_name, ...],
        "still_failed": [order_name, ...],
        "combined_pdf_filename": str/None,
    }
    """
    pending = _find_results_needing_reconciliation()

    result = {"checked": len(pending), "fixed": [], "still_failed": [], "combined_pdf_filename": None}
    if not pending:
        return result

    fixed_results = []
    for entry in pending:
        success, data = shopify_api.poll_shipping_label_result(
            entry["store_key"], entry["purchase_result_id"], timeout_seconds=20
        )
        if success:
            fixed_entry = dict(entry)
            fixed_entry["success"] = True
            fixed_entry["tracking_number"] = data.get("tracking_number")
            fixed_entry["label_url"] = data.get("label_url")
            fixed_entry.pop("error", None)
            fixed_results.append(fixed_entry)
            result["fixed"].append(entry.get("order_name"))
        else:
            result["still_failed"].append(entry.get("order_name"))

    if fixed_results:
        combined_filename, _included, _skipped = build_combined_label_pdf(fixed_results, "RECONCILED")
        log_shipping_results(fixed_results, combined_filename)
        result["combined_pdf_filename"] = combined_filename

    return result
