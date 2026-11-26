import glob
import gzip
import inspect
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from il_supermarket_scarper import ScarpingTask, FileTypesFilters

with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

CHAIN_MAP = {
    "RamiLevy": "RAMI_LEVY",
    "OsherAd": "OSHER_AD",
    "DorAlon": "DOR_ALON",
    "Shufersal": "SHUFERSAL",
}

def normalize_chain(name):
    return CHAIN_MAP.get(name, name.upper())

def norm_text(s):
    if not s:
        return ""
    return re.sub(r"[\-\s\"׳״']+", " ", s).strip()

def local_tag(tag):
    return tag.split("}")[-1].lower()

def get_child_text(el, tag_name):
    target = tag_name.lower()
    for c in el:
        if local_tag(c.tag) == target and c.text:
            return c.text.strip()
    return ""

# 1. Download Stores & PriceFull XMLs
chains = list({normalize_chain(s["chain"]) for s in config["stores"]})
sig_params = inspect.signature(ScarpingTask.__init__).parameters

kwargs = {}
if "enabled_scrapers" in sig_params:
    kwargs["enabled_scrapers"] = chains
if "files_types" in sig_params:
    kwargs["files_types"] = [
        FileTypesFilters.STORE_FILE.name,
        FileTypesFilters.PRICE_FULL_FILE.name,
    ]

scraper = ScarpingTask(**kwargs)
scraper.start()
scraper.join()

def read_xml(path):
    try:
        if path.endswith(".gz"):
            with gzip.open(path, "rb") as f:
                raw = f.read()
        else:
            with open(path, "rb") as f:
                raw = f.read()
        try:
            return ET.fromstring(raw)
        except ET.ParseError:
            for enc in ("utf-8-sig", "utf-16", "windows-1255", "iso-8859-8"):
                try:
                    return ET.fromstring(raw.decode(enc))
                except Exception:
                    continue
        print(f"Failed to parse XML: {path}")
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

# 2. Resolve Store IDs from Stores XML files
target_stores = {}  # (chain_dir, store_id_normalized) -> label

for store_cfg in config["stores"]:
    chain_norm = normalize_chain(store_cfg["chain"])
    chain_dir = find_chain_dir(chain_norm)
    print(f"\n--- Looking for: {store_cfg['label']} in {chain_dir} ---")
    if not chain_dir:
        continue

    store_files = [
        p for p in glob.glob(os.path.join(chain_dir, "**", "*"), recursive=True)
        if os.path.isfile(p) and "store" in os.path.basename(p).lower()
    ]
    print(f"Found {len(store_files)} store file(s)")

    city_want = norm_text(store_cfg["city_match"])
    addr_want = norm_text(store_cfg["address_match"])
    matched = False
    city_candidates = []

    for sf in store_files:
        root = read_xml(sf)
        if root is None:
            continue
        for store_el in root.iter():
            if local_tag(store_el.tag) == "store":
                sid = get_child_text(store_el, "StoreId")
                city = get_child_text(store_el, "City")
                addr = get_child_text(store_el, "Address")
                name = get_child_text(store_el, "StoreName")
                if not sid:
                    continue
                city_n = norm_text(city)
                full_n = norm_text(f"{addr} {name}")
                if city_want in city_n:
                    city_candidates.append(f"ID={sid} | {name} | {addr} ({city})")
                    if addr_want in full_n:
                        print(f"MATCHED: {store_cfg['label']} -> ID {sid} ({name} | {addr}, {city})")
                        target_stores[(chain_dir, sid.zfill(3))] = store_cfg["label"]
                        target_stores[(chain_dir, sid.lstrip("0") or "0")] = store_cfg["label"]
                        matched = True

    if not matched:
        print(f"NO MATCH for {store_cfg['label']}. Branches in '{store_cfg['city_match']}':")
        for c in city_candidates[:25]:
            print("  ", c)

# 3. Parse PriceFull files for matched stores
results = {item["name"]: [] for item in config["items"]}
seen_files = set()

for (chain_dir, sid), label in target_stores.items():
    candidates = [
        p for p in glob.glob(os.path.join(chain_dir, "**", "*"), recursive=True)
        if os.path.isfile(p)
        and "pricefull" in os.path.basename(p).lower()
        and f"-{sid}-" in os.path.basename(p)
    ]
    for pf in candidates[:1]:
        if pf in seen_files:
            continue
        seen_files.add(pf)
        print(f"Parsing {os.path.basename(pf)} for {label}")
        root = read_xml(pf)
        if root is None:
            continue
        for item_el in root.iter():
            if local_tag(item_el.tag) == "item":
                name = get_child_text(item_el, "ItemName")
                try:
                    price = float(get_child_text(item_el, "ItemPrice") or 0)
                    unit_price = float(get_child_text(item_el, "UnitOfMeasurePrice") or 0)
                except ValueError:
                    continue
                unit = get_child_text(item_el, "UnitOfMeasure")
                if price <= 0:
                    continue
                for cfg_item in config["items"]:
                    if any(kw in name for kw in cfg_item["keywords"]) and not any(ex in name for ex in cfg_item["exclude"]):
                        results[cfg_item["name"]].append({
                            "store": label,
                            "product": name,
                            "price": price,
                            "unit_price": unit_price if unit_price > 0 else price,
                            "unit": unit,
                        })

# 4. Build Mobile HTML Page
now = datetime.now().strftime("%Y-%m-%d %H:%M")
store_labels = [s["label"] for s in config["stores"]]

html = f"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SabiPricez - השוואת מחירים</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 12px; background: #f7f7f8; color: #111; }}
  h1 {{ font-size: 1.3rem; margin-bottom: 4px; }}
  .meta {{ color: #666; font-size: 0.85rem; margin-bottom: 12px; }}
  .filters {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 16px; }}
  .chip {{ padding: 6px 10px; border-radius: 16px; border: 1px solid #ccc; background: #fff; font-size: 0.8rem; cursor: pointer; }}
  .chip.active {{ background: #111; color: #fff; border-color: #111; }}
  .card {{ background: #fff; border-radius: 10px; padding: 12px; margin-bottom: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  .card h2 {{ font-size: 1.05rem; margin: 0 0 8px 0; border-bottom: 1px solid #eee; padding-bottom: 6px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  th, td {{ text-align: right; padding: 6px 4px; border-bottom: 1px solid #f0f0f0; }}
  .best {{ background: #e8f8ee; font-weight: 600; }}
</style>
</head>
<body>
<h1>SabiPricez - השוואת מחירים</h1>
<div class="meta">עודכן לאחרונה: {now}</div>
<div class="filters" id="storeFilters">
  {''.join(f'<button class="chip active" onclick="toggleStore(this, `{s}`)">{s}</button>' for s in store_labels)}
</div>
"""

for item_name, matches in results.items():
    matches.sort(key=lambda x: x["unit_price"])
    top_matches = matches[:15]
    html += f'<div class="card"><h2>{item_name}</h2><table><tr><th>חנות</th><th>מוצר</th><th>מחיר</th><th>ליחידה</th></tr>'
    if not top_matches:
        html += '<tr><td colspan="4" style="color:#888;">לא נמצאו תוצאות</td></tr>'
    for idx, m in enumerate(top_matches):
        cls = "best" if idx == 0 else ""
        html += f'<tr class="{cls}" data-store="{m["store"]}"><td>{m["store"]}</td><td>{m["product"]}</td><td>₪{m["price"]:.2f}</td><td>₪{m["unit_price"]:.2f} ({m["unit"]})</td></tr>'
    html += "</table></div>"

html += """
<script>
const hiddenStores = new Set();
function toggleStore(btn, store) {
  btn.classList.toggle('active');
  if (hiddenStores.has(store)) hiddenStores.delete(store);
  else hiddenStores.add(store);
  document.querySelectorAll('tr[data-store]').forEach(row => {
    row.style.display = hiddenStores.has(row.dataset.store) ? 'none' : '';
  });
}
</script>
</body></html>"""

with open("index.html", "w", encoding="utf-8") as f:
    f.write(html)
