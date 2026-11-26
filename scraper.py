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
