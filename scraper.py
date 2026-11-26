import glob
import gzip
import inspect
import json
import os
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

# 1. Download Stores & PriceFull XMLs using only parameters supported by the installed version
chains = list({normalize_chain(s["chain"]) for s in config["stores"]})
sig_params = inspect.signature(ScarpingTask.__init__).parameters
print("ScarpingTask parameters:", list(sig_params.keys()))

kwargs = {}
if "enabled_scrapers" in sig_params:
    kwargs["enabled_scrapers"] = chains
if "files_types" in sig_params:
    kwargs["files_types"] = [
        FileTypesFilters.STORE_FILE.name,
        FileTypesFilters.PRICE_FULL_FILE.name,
    ]
if "dump_folder_name" in sig_params:
    os.makedirs("dumps", exist_ok=True)
    kwargs["dump_folder_name"] = "dumps"

scraper = ScarpingTask(**kwargs)
scraper.start()
scraper.join()

def read_xml(path):
    try:
        if path.endswith(".gz"):
            with gzip.open(path, "rb") as f:
                return ET.fromstring(f.read())
        return ET.parse(path).getroot()
    except Exception:
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
    print(f"Chain {chain_norm} -> dir: {chain_dir}")
    if not chain_dir:
        continue
    store_files = glob.glob(os.path.join(chain_dir, "**", "*[Ss]tores*"), recursive=True)
    for sf in store_files:
        if os.path.isdir(sf):
            continue
        root = read_xml(sf)
        if root is None:
            continue
        for store_el in root.iter():
            if store_el.tag.endswith("Store"):
                sid = next((c.text.strip() for c in store_el if c.tag.endswith("StoreId") and c.text), "")
                city = next((c.text or "" for c in store_el if c.tag.endswith("City")), "")
                addr = next((c.text or "" for c in store_el if c.tag.endswith("Address")), "")
                if sid and store_cfg["city_match"] in city and store_cfg["address_match"] in addr:
                    print(f"Matched store: {store_cfg['label']} -> ID {sid} ({city}, {addr})")
                    target_stores[(chain_dir, sid.zfill(3))] = store_cfg["label"]
                    target_stores[(chain_dir, sid.lstrip("0") or "0")] = store_cfg["label"]

# 3. Parse PriceFull files for matched stores
results = {item["name"]: [] for item in config["items"]}
seen_files = set()

for (chain_dir, sid), label in target_stores.items():
    candidates = glob.glob(os.path.join(chain_dir, "**", f"*[Pp]rice[Ff]ull*-{sid}-*"), recursive=True)
    for pf in candidates[:1]:
        if pf in seen_files or os.path.isdir(pf):
            continue
        seen_files.add(pf)
        root = read_xml(pf)
        if root is None:
            continue
        for item_el in root.iter():
            if item_el.tag.endswith("Item"):
                name = next((c.text or "" for c in item_el if c.tag.endswith("ItemName")), "")
                try:
                    price = float(next((c.text or 0 for c in item_el if c.tag.endswith("ItemPrice")), 0))
                    unit_price = float(next((c.text or 0 for c in item_el if c.tag.endswith("UnitOfMeasurePrice")), 0))
                except ValueError:
                    continue
                unit = next((c.text or "" for c in item_el if c.tag.endswith("UnitOfMeasure")), "")
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
