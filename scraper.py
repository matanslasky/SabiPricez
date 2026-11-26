import glob
import gzip
import os
import xml.etree.ElementTree as ET
from il_supermarket_scarper import ScarpingTask, FileTypesFilters

CHAINS = ["RAMI_LEVY", "OSHER_AD", "DOR_ALON", "SHUFERSAL"]
KEYWORDS = ["יהודה", "אבא הלל", "קרמניצקי", "קרמינצקי", "שלום עליכם"]

scraper = ScarpingTask(
    enabled_scrapers=CHAINS,
    files_types=[FileTypesFilters.STORE_FILE.name],
)
scraper.start()
scraper.join()

def decode_file(path):
    raw = gzip.open(path, "rb").read() if path.endswith(".gz") else open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-16", "windows-1255", "iso-8859-8"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")

for chain in CHAINS:
    target = chain.replace("_", "").lower()
    chain_dir = next(
        (os.path.join(r, d) for r, dirs, _ in os.walk(".") for d in dirs if d.replace("_", "").lower() == target),
        None,
    )
    print(f"\n========== {chain} ({chain_dir}) ==========")
    if not chain_dir:
        continue
    files = [p for p in glob.glob(os.path.join(chain_dir, "**", "*"), recursive=True) if os.path.isfile(p)]
    print("Files:", [os.path.basename(f) for f in files])
    if not files:
        continue

    text = decode_file(files[0])
    print("Preview:", text[:500].replace("\n", " "))

    try:
        root = ET.fromstring(text.encode("utf-8"))
        for el in root.iter():
            if len(el) >= 3:
                fields = {c.tag.split("}")[-1]: (c.text or "").strip() for c in el if c.text and c.text.strip()}
                joined = " ".join(fields.values())
                if any(kw in joined for kw in KEYWORDS):
                    print("MATCH:", fields)
    except Exception as e:
        print("XML parse error:", e)
