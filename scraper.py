import glob
import gzip
import json
import os
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

# 1. Fast targeted download using exact Store IDs
chains = list({s["chain"] for s in config["stores"]})
known_ids = [s["store_id"].zfill(3) for s in config["stores"] if s.get("store_id") and s["store_id"] != "TBD"]
has_tbd = any(s.get("store_id") == "TBD" for s in config["stores"])

id_pattern = "|".join(known_ids)
file_regex = rf"(?i).*(Stores|PriceFull.*-({id_pattern})-)" if has_tbd else rf"(?i).*PriceFull.*-({id_pattern})-"
file_types = [FileTypesFilters.PRICE_FULL_FILE.name]
if has_tbd:
    file_types.append(FileTypesFilters.STORE_FILE.name)

print("Downloading with regex:", file_regex)
scraper = ScarpingTask(
    enabled_scrapers=chains,
    files_types=file_types,
    file_name_regex=file_regex,
)
scraper.start()
scraper.join()

# Optional: Print Rami Levy Ramat Gan / Bnei Brak branch IDs if still TBD
if has_tbd:
    rl_dir = find_chain_dir("RAMI_LEVY")
    if rl_dir:
        store_files = sorted(
            [p for p in glob.glob(os.path.join(rl_dir, "**", "*"), recursive=True)
             if os.path.isfile(p) and "store" in os.path.basename(p).lower()],
            reverse=True,
        )
        if store_files:
            root = read_xml(store_files[0])
            if root is not None:
                print("\n--- Rami Levy Ramat Gan / Bnei Brak / Ayalon candidates ---")
                for el in root.iter():
                    if local_tag(el.tag) == "store":
                        sid = get_child_text(el, "StoreID")
                        name = get_child_text(el, "StoreName")
                        addr = get_child_text(el, "Address")
                        city = get_child_text(el, "City")
                        blob = f"{name} {addr} {city}"
                        if any(k in blob for k in ("8600", "6100", "רמת גן", "בני ברק", "איילון", "מבצע קדש", "ביאליק", "ז'בוטינסקי", "גבעתיים")):
                            print(f"  ID={sid} | {name} | {addr} (City={city})")

# 2. Parse newest PriceFull XML for each configured store
results = {item["name"]: [] for item in config["items"]}

for store_cfg in config["stores"]:
    sid = store_cfg.get("store_id", "")
    if not sid or sid == "TBD":
        continue
    sid_pad = sid.zfill(3)
    chain_dir = find_chain_dir(store_cfg["chain"])
    if not chain_dir:
        print(f"Missing folder for {store_cfg['chain']}")
        continue

    candidates = sorted(
        [
            p for p in glob.glob(os.path.join(chain_dir, "**", "*"), recursive=True)
            if os.path.isfile(p)
            and "pricefull" in os.path.basename(p).lower()
            and f"-{sid_pad}-" in os.path.basename(p)
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
            except ValueError:
                continue
            unit = get_child_text(item_el, "UnitOfMeasure")
            if price <= 0:
                continue
            for cfg_item in config["items"]:
                if any(kw in name for kw in cfg_item["keywords"]) and not any(ex in name for ex in cfg_item["exclude"]):
                    results[cfg_item["name"]].append({
                        "store": store_cfg["label"],
                        "short_store": store_cfg.get("short_name", store_cfg["label"]),
                        "product": name,
                        "price": round(price, 2),
                        "unit_price": round(unit_price if unit_price > 0 else price, 2),
                        "unit": unit,
                    })
                    matched_count += 1
    print(f"  -> {matched_count} product matches")

# 3. Save data.json (for our upcoming custom UI)
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

# 4. Build current index.html so you can see live prices right away
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

for item_data in payload["items"]:
    html += f'<div class="card"><h2>{item_data["name"]}</h2><table><tr><th>חנות</th><th>מוצר</th><th>מחיר</th><th>ליחידה</th></tr>'
    if not item_data["matches"]:
        html += '<tr><td colspan="4" style="color:#888;">לא נמצאו תוצאות</td></tr>'
    for idx, m in enumerate(item_data["matches"][:15]):
        cls = "best" if idx == 0 else ""
        html += f'<tr class="{cls}" data-store="{m["store"]}"><td>{m["short_store"]}</td><td>{m["product"]}</td><td>₪{m["price"]:.2f}</td><td>₪{m["unit_price"]:.2f} ({m["unit"]})</td></tr>'
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
