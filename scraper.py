import glob
import gzip
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo
from il_supermarket_scarper import ScarpingTask, FileTypesFilters

with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

def local_tag(tag):
    return tag.split("}")[-1].lower()

def get_child_text(el, tag_name):
    target = tag_name.lower()
    for c in el:
        if local_tag(c.tag) == target and c.text:
            return c.text.strip()
    return ""

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

def calc_normalized_unit_price(price, unit_price, unit, qty, unit_qty, is_weighted):
    # 1. Weighted items (produce / butcher) are always priced per kg
    if is_weighted:
        return round(price, 2), 'ק"ג'

    # 2. Best source: calculate directly from Quantity + UnitQty
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

    # 3. Fallback to UnitOfMeasurePrice + UnitOfMeasure (with Osher Ad 1ml/1g bug guard)
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

    return round(up, 2), unit or "יח'"

# 1. Fast targeted download using exact Store IDs (anchored before YYYYMMDD date)
chains = list({s["chain"] for s in config["stores"]})
known_ids = [s["store_id"].zfill(3) for s in config["stores"] if s.get("store_id")]
id_pattern = "|".join(known_ids)
file_regex = rf"(?i).*PriceFull.*-({id_pattern})-\d{{8}}"

print("Downloading with regex:", file_regex)
scraper = ScarpingTask(
    enabled_scrapers=chains,
    files_types=[FileTypesFilters.PRICE_FULL_FILE.name],
    file_name_regex=file_regex,
)
scraper.start()
scraper.join()

# 2. Parse newest PriceFull XML for each configured store
results = {item["name"]: [] for item in config["items"]}
seen_entries = {item["name"]: set() for item in config["items"]}

for store_cfg in config["stores"]:
    sid_pad = store_cfg["store_id"].zfill(3)
    chain_dir = find_chain_dir(store_cfg["chain"])
    if not chain_dir:
        print(f"Missing folder for {store_cfg['chain']}")
        continue

    candidates = sorted(
        [
            p for p in glob.glob(os.path.join(chain_dir, "**", "*"), recursive=True)
            if os.path.isfile(p)
            and "pricefull" in os.path.basename(p).lower()
            and re.search(rf"-{sid_pad}-\d{{8}}", os.path.basename(p))
        ],
        reverse=True,
    )
    if not candidates:
        print(f"No PriceFull file found for {store_cfg['label']} (ID {sid_pad})")
        continue

    pf = candidates[0]
    print(f"Parsing {os.path.basename(pf)} for {store_cfg['label']}")
    root = read_xml(pf)
    if root is None:
        continue

    matched_count = 0
    for item_el in root.iter():
        if local_tag(item_el.tag) == "item":
            name = get_child_text(item_el, "ItemName")
            try:
                price = float(get_child_text(item_el, "ItemPrice") or 0)
                unit_price = float(get_child_text(item_el, "UnitOfMeasurePrice") or 0)
                qty = float(get_child_text(item_el, "Quantity") or 0)
            except ValueError:
                continue
            unit = get_child_text(item_el, "UnitOfMeasure")
            unit_qty = get_child_text(item_el, "UnitQty")
            is_weighted = get_child_text(item_el, "bIsWeighted") == "1"
            if price <= 0:
                continue

            for cfg_item in config["items"]:
                if (
                    any(matches_keyword(name, kw) for kw in cfg_item["keywords"])
                    and not any(ex in name for ex in cfg_item["exclude"])
                ):
                    norm_up, norm_unit = calc_normalized_unit_price(
                        price, unit_price, unit, qty, unit_qty, is_weighted
                    )
                    dedup_key = (store_cfg["label"], name, round(price, 2))
                    if dedup_key in seen_entries[cfg_item["name"]]:
                        continue
                    seen_entries[cfg_item["name"]].add(dedup_key)

                    results[cfg_item["name"]].append({
                        "store": store_cfg["label"],
                        "short_store": store_cfg.get("short_name", store_cfg["label"]),
                        "product": name,
                        "price": round(price, 2),
                        "unit_price": norm_up,
                        "unit": norm_unit,
                    })
                    matched_count += 1
    print(f"  -> {matched_count} product matches")

# 3. Save data.json
now = datetime.now(ZoneInfo("Asia/Jerusalem")).strftime("%Y-%m-%d %H:%M")
payload = {
    "updated_at": now,
    "stores": config["stores"],
    "items": [
        {
            "name": item["name"],
            "category": item.get("category", "כללי"),
            "matches": sorted(results[item["name"]], key=lambda x: x["unit_price"])[:20],
        }
        for item in config["items"]
    ],
}
with open("data.json", "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
