import glob
import gzip
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from il_supermarket_scarper import ScarpingTask, FileTypesFilters

with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

TZ = ZoneInfo("Asia/Jerusalem")
TODAY = datetime.now(TZ).date()
TODAY_STR = TODAY.strftime("%Y-%m-%d")
STALE_CUTOFF = (TODAY - timedelta(days=365)).strftime("%Y-%m-%d")
HISTORY_CUTOFF = (TODAY - timedelta(days=30)).strftime("%Y-%m-%d")
HISTORY_FILE = "history.json"

IGNORE_PROMO_TERMS = (
    "קופון", "פיצוי", "הנחת עובד", "תו זהב", "מצטרפים", "במתנה", 'ח"ע', "הטבת",
)
CLUB_TERMS = ("מועדון", "אשראי", "פלטינה", "חברי", "שופרסל כארד")
CONDITIONAL_TERMS = (
    "בקנייה מעל", "בקניה מעל", "מעל 50", "מעל 75", "מעל 100", "מעל 150", "מעל 200",
)


def local_tag(tag):
    return tag.split("}")[-1].lower()


def get_child_text(el, tag_name):
    target = tag_name.lower()
    for c in el:
        if local_tag(c.tag) == target and c.text:
            return c.text.strip()
    return ""


def get_child_float(el, *tag_names):
    for name in tag_names:
        txt = get_child_text(el, name)
        if txt:
            val = safe_float(txt, 0.0)
            if val > 0:
                return val
    return 0.0


def safe_float(val, default=0.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def read_xml(path):
    try:
        raw = gzip.open(path, "rb").read() if path.endswith(".gz") else open(path, "rb").read()
        try:
            return ET.fromstring(raw)
        except ET.ParseError:
            for enc in ("utf-8-sig", "utf-16", "windows-1255", "iso-8859-8"):
                try:
                    return ET.fromstring(raw.decode(enc))
                except Exception:
                    continue
        return None
    except Exception as e:
        print(f"Error reading {path}: {e}")
        return None


def find_chain_dir(chain_norm):
    target = chain_norm.replace("_", "").lower()
    for root_dir, dirs, _ in os.walk("."):
        dirs[:] = [d for d in dirs if d not in (".git", ".github", "__pycache__")]
        for d in dirs:
            if d.replace("_", "").lower() == target:
                return os.path.join(root_dir, d)
    return None


def matches_keyword(name, kw):
    return re.search(rf"(?<![א-ת]){re.escape(kw)}(?![א-ת])", name) is not None


def parse_weight_from_name(name, price):
    """Fallback when XML mislabels a weighted package (e.g. 800g lentils) as 'יחידות'."""
    m_kg = re.search(r"(\d+(?:\.\d+)?)\s*(?:ק\"ג|קג|קילו)", name)
    if m_kg:
        val = safe_float(m_kg.group(1), 0.0)
        if 0.1 <= val <= 25:
            return round(price / val, 2), 'ק"ג'
    m_g = re.search(r"(\d+(?:\.\d+)?)\s*(?:גרם|גר|ג')", name)
    if m_g:
        val = safe_float(m_g.group(1), 0.0)
        if 50 <= val <= 5000:
            return round(price / (val / 1000.0), 2), 'ק"ג'
    return None, None


def calc_normalized_unit_price(name, price, unit_price, unit, qty, unit_qty, is_weighted):
    if is_weighted:
        return round(price, 2), 'ק"ג'

    uq = (unit_qty or "").replace(" ", "").lower()
    if qty > 0 and uq:
        if "קילו" in uq or 'ק"ג' in uq or "קג" in uq:
            return round(price / qty, 2), 'ק"ג'
        if "גר" in uq or "ג'" in uq:
            return round(price / (qty / 1000.0), 2), 'ק"ג'
        if "מילי" in uq or 'מ"ל' in uq or "מל" in uq:
            return round(price / (qty / 1000.0), 2), "ליטר"
        if "ליטר" in uq:
            return round(price / qty, 2), "ליטר"

    up = unit_price if unit_price > 0 else price
    u = (unit or "").replace(" ", "").lower()
    if "100" in u and ("גר" in u or "ג'" in u):
        mult = 1000 if (up < 0.5 and price >= 5) else 10
        return round(up * mult, 2), 'ק"ג'
    if "100" in u and ("מל" in u or 'מ"ל' in u or "מילי" in u):
        mult = 1000 if (up < 0.5 and price >= 5) else 10
        return round(up * mult, 2), "ליטר"
    if u in ("גרם", "גרמים", "ג'"):
        return round(up * 1000, 2), 'ק"ג'
    if "קילו" in u or 'ק"ג' in u or "קג" in u:
        return round(up, 2), 'ק"ג'
    if "ליטר" in u:
        return round(up, 2), "ליטר"

    # Guard against POS mislabeling weighted bags as "יחידות" (e.g. 800g lentils -> ₪0.80)
    if "יח" in u or not u:
        parsed_up, parsed_u = parse_weight_from_name(name, price)
        if parsed_up is not None:
            return parsed_up, parsed_u

    return round(up, 2), unit or "יח'"


def parse_promos(xml_root):
    promos_by_code = {}
    if xml_root is None:
        return promos_by_code

    debug_printed = 0
    for el in xml_root.iter():
        if local_tag(el.tag) != "promotion":
            continue
        try:
            end_date = get_child_text(el, "PromotionEndDate")[:10]
            start_date = get_child_text(el, "PromotionStartDate")[:10]
            if end_date and end_date < TODAY_STR:
                continue
            if start_date and start_date > TODAY_STR:
                continue

            desc = get_child_text(el, "PromotionDescription") or get_child_text(el, "Description")
            if not desc or any(t in desc for t in IGNORE_PROMO_TERMS):
                continue

            min_qty = get_child_float(el, "MinQty", "minqty") or 1.0
            disc_price = get_child_float(el, "DiscountedPrice", "DiscountPrice", "PromoPrice")
            disc_per_mida = get_child_float(el, "DiscountedPricePerMida", "DiscountPricePerMida")
            disc_rate = get_child_float(el, "DiscountRate", "DiscountPercent", "PriceReduction")
            reward_type = get_child_text(el, "RewardType") or get_child_text(el, "DiscountType")
            min_purchase = get_child_float(el, "MinPurchaseAmnt", "MinPurchaseAmount")

            is_club = any(t in desc for t in CLUB_TERMS)
            is_conditional = (min_purchase > 0) or any(t in desc for t in CONDITIONAL_TERMS)

            if debug_printed < 3 and not is_club and not is_conditional:
                print("[PROMO DEBUG]", {
                    local_tag(c.tag): c.text.strip()
                    for c in el if c.text and c.text.strip()
                })
                debug_printed += 1

            promo_info = {
                "desc": desc,
                "min_qty": round(min_qty, 2),
                "disc_price": disc_price,
                "disc_per_mida": disc_per_mida,
                "disc_rate": disc_rate,
                "reward_type": reward_type,
                "is_club": is_club,
                "is_conditional": is_conditional,
            }

            for code_el in el.iter():
                if local_tag(code_el.tag) in ("itemcode", "barcode", "item_code") and code_el.text:
                    code = code_el.text.strip()
                    if code:
                        promos_by_code.setdefault(code, []).append(promo_info)
        except Exception:
            continue

    return promos_by_code


def resolve_best_promo(promos, price, norm_up, is_weighted):
    if not promos:
        return norm_up, None

    best_open_up = norm_up
    best_open_promo = None
    fallback_info_promo = None

    for p in promos:
        if p["is_club"] or p["is_conditional"]:
            if fallback_info_promo is None:
                fallback_info_promo = {
                    "desc": p["desc"],
                    "min_qty": p["min_qty"],
                    "is_club": p["is_club"],
                    "is_conditional": p["is_conditional"],
                    "promo_unit_price": None,
                }
            continue

        candidate_up = None
        if is_weighted and p["disc_per_mida"] > 0:
            candidate_up = p["disc_per_mida"]
        elif p["disc_price"] > 0 and price > 0:
            mq = p["min_qty"]
            # If total bundle price is >= single-item shelf price and min_qty > 1, divide by min_qty
            promo_shelf = (p["disc_price"] / mq) if (mq > 1 and p["disc_price"] >= price * 0.99) else p["disc_price"]
            candidate_up = norm_up * (promo_shelf / price)
        elif p["disc_rate"] > 0:
            rate = (p["disc_rate"] / 100.0) if p["disc_rate"] > 100 else p["disc_rate"]
            if 1.0 <= rate <= 75.0:
                candidate_up = norm_up * (1.0 - rate / 100.0)

        # Sanity guard: allow down to 20% of normal price, require at least 1% drop
        if candidate_up is not None and (0.20 * norm_up <= candidate_up < norm_up * 0.99):
            candidate_up = round(candidate_up, 2)
            if candidate_up < best_open_up:
                best_open_up = candidate_up
                best_open_promo = {
                    "desc": p["desc"],
                    "min_qty": p["min_qty"],
                    "is_club": False,
                    "is_conditional": False,
                    "promo_unit_price": candidate_up,
                }
        elif fallback_info_promo is None:
            fallback_info_promo = {
                "desc": p["desc"],
                "min_qty": p["min_qty"],
                "is_club": False,
                "is_conditional": False,
                "promo_unit_price": None,
            }

    if best_open_promo:
        return best_open_up, best_open_promo
    return norm_up, fallback_info_promo


def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            print(f"Warning: could not read {HISTORY_FILE}: {e}")
    return {}


def update_history_and_trend(history, item_name, sorted_matches):
    item_hist = history.get(item_name, {})
    if not isinstance(item_hist, dict):
        item_hist = {}

    item_hist = {d: v for d, v in item_hist.items() if d >= HISTORY_CUTOFF and isinstance(v, (int, float)) and v > 0}

    if sorted_matches:
        current_min = round(sorted_matches[0]["effective_unit_price"], 2)
        item_hist[TODAY_STR] = current_min
    else:
        current_min = None

    history[item_name] = dict(sorted(item_hist.items()))

    values = list(history[item_name].values())
    if current_min is None or len(values) < 3:
        return {"days": len(values)}

    min_30d = round(min(values), 2)
    avg_30d = round(sum(values) / len(values), 2)
    pct_below_avg = round(((avg_30d - current_min) / avg_30d) * 100) if avg_30d > 0 else 0
    is_low = (current_min <= min_30d + 0.01) and (pct_below_avg >= 2)

    return {
        "days": len(values),
        "min_30d": min_30d,
        "avg_30d": avg_30d,
        "pct_below_avg": pct_below_avg,
        "is_low": is_low,
    }


# 1. Fast targeted download of PriceFull + PromoFull for our 5 Store IDs
chains = list({s["chain"] for s in config["stores"]})
known_ids = [s["store_id"].zfill(3) for s in config["stores"] if s.get("store_id")]
id_pattern = "|".join(known_ids)
file_regex = rf"(?i).*(PriceFull|PromoFull).*-({id_pattern})-\d{{8}}"

promo_file_types = [f.name for f in FileTypesFilters if "PROMO" in f.name and "FULL" in f.name]
target_types = [FileTypesFilters.PRICE_FULL_FILE.name] + promo_file_types

print("Downloading types:", target_types, "with regex:", file_regex)
scraper = ScarpingTask(
    enabled_scrapers=chains,
    files_types=target_types,
    file_name_regex=file_regex,
)
scraper.start()
scraper.join()

# 2. Parse newest PriceFull + PromoFull XML for each configured store
results = {item["name"]: [] for item in config["items"]}
seen_entries = {item["name"]: set() for item in config["items"]}

for store_cfg in config["stores"]:
    sid_pad = store_cfg["store_id"].zfill(3)
    chain_dir = find_chain_dir(store_cfg["chain"])
    if not chain_dir:
        print(f"Missing folder for {store_cfg['chain']}")
        continue

    all_files = [
        p for p in glob.glob(os.path.join(chain_dir, "**", "*"), recursive=True)
        if os.path.isfile(p) and re.search(rf"-{sid_pad}-\d{{8}}", os.path.basename(p))
    ]
    price_files = sorted([p for p in all_files if "pricefull" in os.path.basename(p).lower()], reverse=True)
    promo_files = sorted([p for p in all_files if "promofull" in os.path.basename(p).lower()], reverse=True)

    if not price_files:
        print(f"No PriceFull file found for {store_cfg['label']} (ID {sid_pad}) — files in dir: {os.listdir(chain_dir)[:5]}")
        continue

    promos_by_code = {}
    if promo_files:
        print(f"Parsing promos {os.path.basename(promo_files[0])} for {store_cfg['label']}")
        promos_by_code = parse_promos(read_xml(promo_files[0]))
        print(f"  -> {len(promos_by_code)} active promo barcodes indexed")

    pf = price_files[0]
    print(f"Parsing prices {os.path.basename(pf)} for {store_cfg['label']}")
    root = read_xml(pf)
    if root is None:
        continue

    matched_count = 0
    promo_hits = 0
    discounted_hits = 0
    for item_el in root.iter():
        if local_tag(item_el.tag) == "item":
            name = get_child_text(item_el, "ItemName")
            item_code = get_child_text(item_el, "ItemCode")
            price_updated = get_child_text(item_el, "PriceUpdateDate")[:10]

            if price_updated and price_updated < STALE_CUTOFF:
                continue

            price = safe_float(get_child_text(item_el, "ItemPrice"), 0.0)
            unit_price = safe_float(get_child_text(item_el, "UnitOfMeasurePrice"), 0.0)
            qty = safe_float(get_child_text(item_el, "Quantity"), 0.0)
            unit = get_child_text(item_el, "UnitOfMeasure")
            unit_qty = get_child_text(item_el, "UnitQty")
            is_weighted = get_child_text(item_el, "bIsWeighted") == "1"
            if price <= 0:
                continue

            item_promos = promos_by_code.get(item_code, [])
            promo_text_blob = " ".join(p["desc"] for p in item_promos)

            for cfg_item in config["items"]:
                if (
                    any(matches_keyword(name, kw) for kw in cfg_item["keywords"])
                    and not any(ex in name for ex in cfg_item["exclude"])
                    and not ("קפוא" in cfg_item["exclude"] and "קפוא" in promo_text_blob)
                ):
                    norm_up, norm_unit = calc_normalized_unit_price(
                        name, price, unit_price, unit, qty, unit_qty, is_weighted
                    )
                    dedup_key = (store_cfg["label"], name, round(price, 2))
                    if dedup_key in seen_entries[cfg_item["name"]]:
                        continue
                    seen_entries[cfg_item["name"]].add(dedup_key)

                    eff_up, promo_obj = resolve_best_promo(
                        item_promos, price, norm_up, is_weighted
                    )
                    if promo_obj:
                        promo_hits += 1
                    if eff_up < norm_up - 0.01:
                        discounted_hits += 1

                    results[cfg_item["name"]].append({
                        "store": store_cfg["label"],
                        "short_store": store_cfg.get("short_name", store_cfg["label"]),
                        "item_code": item_code,
                        "product": name,
                        "price": round(price, 2),
                        "unit_price": norm_up,
                        "effective_unit_price": eff_up,
                        "unit": norm_unit,
                        "price_updated": price_updated,
                        "promo": promo_obj,
                    })
                    matched_count += 1
    print(f"  -> {matched_count} matches ({promo_hits} promos, {discounted_hits} lowered price)")

# 3. Update 30-day history and save history.json + data.json
history = load_history()
items_payload = []

for item in config["items"]:
    sorted_matches = sorted(results[item["name"]], key=lambda x: x["effective_unit_price"])[:15]
    trend = update_history_and_trend(history, item["name"], sorted_matches)
    items_payload.append({
        "name": item["name"],
        "category": item.get("category", "כללי"),
        "trend": trend,
        "matches": sorted_matches,
    })

with open(HISTORY_FILE, "w", encoding="utf-8") as f:
    json.dump(history, f, ensure_ascii=False, indent=2)

now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
payload = {
    "updated_at": now,
    "stores": config["stores"],
    "items": items_payload,
}
with open("data.json", "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
