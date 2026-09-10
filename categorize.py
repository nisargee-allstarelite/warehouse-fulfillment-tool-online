"""
Categorization logic for Shopify line items.

This is the Shopify counterpart to the TikTok tool's bucketing.py - and it
turns out to be much simpler. TikTok gives us a free-text seller note that
has to be parsed apart (SKU-style line? descriptive-name line? both?
size/qty embedded how?). Shopify gives us clean structured data straight
from the line item: a real `sku` field and a real `productTitle` field,
already split out, with quantity and variant (size) as their own fields.
So there's no note-parsing needed here at all.

What IS reused, unchanged, from the TikTok side: the actual category
lookup tables (CATEGORY_MAP for SKU-code segments, and the tiered alias
dictionaries for matching a plain-language product name). These represent
the real physical categories used in the warehouse, and - important find -
Shopify SKUs use the exact same category-code scheme as the SKU-style
TikTok notes do (e.g. "WTSN026-CRDNMSHT-01-KHAKI-38" uses the same
"CRDNMSHT" style code as TikTok's "WTSN026-DNMSHT-07-LT WASH-M"). So the
same CATEGORY_MAP directly applies.

If you add a new product line / SKU code, add it to CATEGORY_MAP (or the
right alias tier below) - both tools should be kept in sync, since they're
describing the same physical warehouse categories.
"""

import re

OTHERS = "Others"
NEEDS_REVIEW = "⚠️ NEEDS REVIEW"

# --- SKU-code format: 2nd dash-segment (or 2nd+3rd combined) is the code ---
# Copied from the TikTok tool's bucketing.py CATEGORY_MAP - keep these two
# in sync if a new product line/code gets added on either side.
CATEGORY_MAP = {
    "DNMSHT": "Denim Shorts",
    "BBJ": "Basketball Jersey",
    "BAJ": "Baseball Jersey",
    "POLO": "Rugby Polo",
    "TSHT": "Tshirt",
    "MINK": "Mink Jacket",
    "SFLNL": "Short Sleeved Flannel Shirt",
    "MOTO": "Moto Jacket",
    "LESHT": "Leather Shorts",
    "CSHT": "Cotton Shorts",
    "CUTSHT": "Cutoff Tshirt",
    "CRODNMSHT": "Crochet Denim Shorts",
    "CRDNMSHT": "Crochet Denim Shorts",  # Shopify's shortened variant of the code above
    "CARPSHT": "Carpenter Shorts",
    "TFLNL": "Tshirt Flannel",
    "LTSHT": "Long Sleeved Tshirts",
    "HOOD": "Hoodie",
    "DNMWSHT": "Denim Work Shirt",
    "PUFF": "Puffer Jacket",
    "TAPE": "Tapestry Shorts",
    "CSWT": "Sweater",
    "SSHT": "Short Sleeved Button Down Shirts",
    "PWSHT": "Button Shirt",
    "PPNTS": "Pants",
    "SWEAT-JKT": "Sweatsuit Jacket",  # two-segment code - matched before plain SWEAT
    "SHT": "Shorts",
    "SWEAT": "Sweats",
}
# Note: a SKU like "DENIM-CHAIN-C-6-OS" doesn't follow the
# STYLEPREFIX-CODE-... convention (there's no style prefix segment to
# skip), so match_category_code below can't find its code from position
# 1. That's fine - categorize() falls back to matching the product TITLE
# against ACCESSORY_ALIASES ("PANT CHAIN"/"DENIM CHAIN" -> "Denim Chain")
# for exactly this shape of SKU, same as it would for any other
# not-quite-coded SKU.


def match_category_code(parts):
    """Try a combined 2-segment code first (e.g. SWEAT-JKT), then a plain
    single-segment code (e.g. BBJ). `parts` is a SKU already split on '-'.
    Returns the friendly category name, or OTHERS if nothing matches."""
    if len(parts) >= 3:
        two_seg = f"{parts[1]}-{parts[2]}".strip().upper()
        if two_seg in CATEGORY_MAP:
            return CATEGORY_MAP[two_seg]
    if len(parts) >= 2:
        one_seg = parts[1].strip().upper()
        return CATEGORY_MAP.get(one_seg, OTHERS)
    return OTHERS


# ─── Descriptive-name category matching - identical tables to the TikTok
# tool's bucketing.py (same 88-category master list). Used as a fallback
# for line items whose SKU doesn't carry a recognized category code, by
# matching the Shopify product title instead. ────────────────────────────

SHOE_LINE_ALIASES = {
    "COURT CLASSIC 2":  "Court Classic 2",
    "COURT CLASSIC":    "Court Classic",
    "CHUNKY BONES":     "Chunky Bones",
    "CRYPT CRAWLER":    "Crypt Crawler",
    "VULTURE STRAP":    "Vulture Strap",
    "VULTURE V2":       "Vulture V2",
    "VULTURE":          "Vulture",
    "TRIPLE 7 RACER":   "Triple 7 Racer",
    "LIFESTYLE 01":     "Lifestyle 01",
    "SOUL SPRINTER":    "Soul Sprinter",
    "LYTE RUNNER":      "Lyte Runner",
    "NORTH STAR":       "North Star",
    "ARTIC FOX":        "Artic Fox",
    "ARCTIC FOX":       "Artic Fox",
    "K FLIP":           "K Flip",
    "K-FLIP":           "K Flip",
    "KFLIP":            "K Flip",
    "BONESTA":          "Bonesta",
    "BONEBA":           "Boneba",
    "BONES":            "Bones",
    "VULCAN/VULCANS":   "Vulcan/Vulcans",
    "VULCANS":          "Vulcan/Vulcans",
    "VULCAN":           "Vulcan/Vulcans",
    "INVADER":          "Invader",
    "ROCKSTAR":         "Rockstar",
    "SKELETOR 2":       "Skeletor 2",
    "SKELETOR":         "Skeletor",
    "TRAILMAX":         "Trailmax",
    "COSMOS":           "Cosmos",
    "RAIDER":           "Raider",
    "LOPRO/LOPROS":     "LoPro/LoPros",
    "LOPROS":           "LoPro/LoPros",
    "LOPRO":            "LoPro/LoPros",
    "CONCORD":          "Concord",
    "ANURAS":           "Anuras",
    "ANURA":            "Anuras",
    "7SVN7":            "7SVN7",
    "CYBER":            "Cyber",
}

APPAREL_ALIASES = {
    "BASKETBALL JERSEYS": "Basketball Jersey",
    "BASKETBALL JERSEY":  "Basketball Jersey",
    "BASEBALL JERSEYS":   "Baseball Jersey",
    "BASEBALL JERSEY":    "Baseball Jersey",
    "FOOTBALL JERSEYS":   "Football Jersey",
    "FOOTBALL JERSEY":    "Football Jersey",
    "HOCKEY JERSEYS":     "Hockey Jersey",
    "HOCKEY JERSEY":      "Hockey Jersey",
    "SOCCER JERSEYS":     "Soccer Jersey",
    "SOCCER JERSEY":      "Soccer Jersey",
    "MOVIE JERSEYS":      "Movie Jersey",
    "MOVIE JERSEY":       "Movie Jersey",
    "JERSEY DRESSES":     "Jersey Dress",
    "JERSEY DRESS":       "Jersey Dress",
    "BASKETBALL SHORTS":  "Basketball Shorts",
    "BASKETBALL SHORT":   "Basketball Shorts",
    "SATIN JACKETS":      "Satin Jacket",
    "SATIN JACKET":       "Satin Jacket",
    "VARSITY JACKETS":    "Varsity Jacket",
    "VARSITY JACKET":     "Varsity Jacket",
    "MOTO JACKETS":       "Moto Jacket",
    "MOTO JACKET":        "Moto Jacket",
    "RACING JACKETS":     "Racing Jackets",
    "RACING JACKET":      "Racing Jackets",
    "MINK JACKETS":       "Mink Jacket",
    "MINK JACKET":        "Mink Jacket",
    "DICKIES JACKETS":    "Dickies Jacket",
    "DICKIES JACKET":     "Dickies Jacket",
    "LEATHER JACKETS":    "Leather Jacket",
    "LEATHER JACKET":     "Leather Jacket",
    "SHERPA JACKETS":     "Sherpa Jacket",
    "SHERPA JACKET":      "Sherpa Jacket",
    "PUFFER JACKETS":     "Puffer Jacket/Vest",
    "PUFFER JACKET":      "Puffer Jacket/Vest",
    "PUFFER VESTS":       "Puffer Jacket/Vest",
    "PUFFER VEST":        "Puffer Jacket/Vest",
    "DENIM JACKETS":      "Denim Jacket",
    "DENIM JACKET":       "Denim Jacket",
    "WORK JACKETS":       "Work Jacket",
    "WORK JACKET":        "Work Jacket",
    "FLANNEL SHIRTS":     "Flannel Shirt",
    "FLANNEL SHIRT":      "Flannel Shirt",
    "WORK SHIRTS":        "Work Shirt",
    "WORK SHIRT":         "Work Shirt",
    "SATIN PANTS":        "Satin Pants",
    "SATIN PANT":         "Satin Pants",
    "LONG SLEEVE TEES":   "Long Sleeve Tee",
    "LONG SLEEVE TEE":    "Long Sleeve Tee",
    "LONG SLEEVE TSHIRT": "Long Sleeve Tee",
    "SWEATSUITS":         "Sweatsuit",
    "SWEATSUIT":          "Sweatsuit",
}

ACCESSORY_ALIASES = {
    "PANT CHAINS":  "Denim Chain",
    "PANT CHAIN":   "Denim Chain",
    "DENIM CHAINS": "Denim Chain",
    "DENIM CHAIN":  "Denim Chain",
    "SNAPBACKS":    "Snapback",
    "SNAPBACK":     "Snapback",
    "STRAPBACKS":   "Snapback",
    "STRAPBACK":    "Snapback",
    "BACKPACKS":    "Backpack",
    "BACKPACK":     "Backpack",
    "BOOKBAGS":     "Backpack",
    "BOOKBAG":      "Backpack",
    "DUFFLE BAGS":  "Duffle Bag",
    "DUFFLE BAG":   "Duffle Bag",
    "DUFFLE":       "Duffle Bag",
    "LANYARDS":     "Lanyard",
    "LANYARD":      "Lanyard",
    "BEANIES":      "Beanie",
    "BEANIE":       "Beanie",
    "SOCKS":        "Socks",
    "SOCK":         "Socks",
    "PINS":         "Pin",
    "PIN":          "Pin",
    "RUGS":         "Rug",
    "RUG":          "Rug",
    "BELTS":        "Belts",
    "BELT":         "Belts",
    "FLAGS":        "Flag",
    "FLAG":         "Flag",
    "TRUCKER HAT":  "Hats",
    "DAD HAT":      "Hats",
    "DADHAT":       "Hats",
    "HATS":         "Hats",
    "HAT":          "Hats",
}

GENERIC_ALIASES = {
    "T-SHIRTS":   "Tshirts",
    "T-SHIRT":    "Tshirts",
    "TSHIRTS":    "Tshirts",
    "TSHIRT":     "Tshirts",
    "HOODIES":    "Hoodie",
    "HOODIE":     "Hoodie",
    "SWEATPANTS": "Sweatpants",
    "SWEATSHIRTS": "Sweatshirt",
    "SWEATSHIRT": "Sweatshirt",
    "SWEATERS":   "Sweater",
    "SWEATER":    "Sweater",
    "CROP TOPS":  "Crop Top",
    "CROP TOP":   "Crop Top",
    "POLOS":      "Polo",
    "POLO":       "Polo",
    "VESTS":      "Vest",
    "VEST":       "Vest",
    "SKIRTS":     "Skirt",
    "SKIRT":      "Skirt",
    "UNDERWEAR":  "Underwear",
    "BOXERS":     "Boxers",
    "BOXER":      "Boxers",
    "DENIM":      "Denim",
    "SHORTS":     "Shorts",
    "SHORT":      "Shorts",
    "PANTS":      "Pants",
    "PANT":       "Pants",
    "JERSEYS":    "Jerseys",
    "JERSEY":     "Jerseys",
    "SNEAKERS":   "Sneakers",
    "SNEAKER":    "Sneakers",
    "OUTERWEAR":  "Outerwear",
    "JACKETS":    "Jackets",
    "JACKET":     "Jackets",
    "SHIRTS":     "Shirt",
    "SHIRT":      "Shirt",
}

ROCK_BANDS = {
    "ACDC", "AC/DC", "AEROSMITH", "DEF LEPPARD", "KISS",
    "MOTLEY CRUE", "MÖTLEY CRÜE", "MOTLEY CRÜE", "MÖTLEY CRUE",
}


def match_known_category_name(name):
    """Same tiered longest-match-wins logic as the TikTok tool: shoe lines
    and apparel checked first (as whole tiers) so a short shoe-line name
    like "Bones" beats the longer generic word "Sneakers" that usually
    follows it, then Rock Tshirt, then accessories/generic. Returns None
    if nothing matches at all (caller decides what that means)."""
    name_upper = name.upper()

    for tier in (SHOE_LINE_ALIASES, APPAREL_ALIASES):
        best_match = None
        for alias in tier:
            if alias in name_upper:
                if best_match is None or len(alias) > len(best_match):
                    best_match = alias
        if best_match:
            return tier[best_match]

    if "TSHIRT" in name_upper or "T-SHIRT" in name_upper:
        for band in ROCK_BANDS:
            if band in name_upper:
                return "Rock Tshirt"

    for tier in (ACCESSORY_ALIASES, GENERIC_ALIASES):
        best_match = None
        for alias in tier:
            if alias in name_upper:
                if best_match is None or len(alias) > len(best_match):
                    best_match = alias
        if best_match:
            return tier[best_match]

    if "DONATION" in name_upper or "CHARITY" in name_upper:
        return "Donation"

    return None


def categorize(sku, product_title):
    """
    The main entry point - takes a Shopify line item's sku and productTitle
    (both already clean, structured fields - no parsing needed) and returns
    a category name.

    Tries the SKU code first (e.g. "WTSN026-CRDNMSHT-01-KHAKI-38" -> looks
    up "CRDNMSHT" in CATEGORY_MAP). Falls back to matching the product
    title against the known category names if the SKU doesn't carry a
    recognized code (e.g. a SKU that doesn't follow the coded format at
    all, or a code not yet added to CATEGORY_MAP). Returns OTHERS if
    neither approach finds a match - never NEEDS_REVIEW, since Shopify
    line items always have SOME usable name/SKU (unlike a TikTok seller
    note, which can be garbled or empty).
    """
    sku = (sku or "").strip()
    if sku:
        parts = sku.split('-')
        if len(parts) >= 2:
            cat = match_category_code(parts)
            if cat != OTHERS:
                return cat

    title = (product_title or "").strip()
    if title:
        cat = match_known_category_name(title)
        if cat:
            return cat

    return OTHERS
