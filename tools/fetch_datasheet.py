#!/usr/bin/env python3
"""
fetch_datasheet.py  —  download a REAL, text-extractable component datasheet PDF

Usage:
    python tools/fetch_datasheet.py <PARTNUM> [--out DIR] [--report]

Source cascade (stops at first PDF that passes content validation):
    0. Known verified URL table (parts we've already located by hand)
    1. Manufacturer direct / product-page   (no key; authoritative datasheet)
    2. DigiKey API   (env DIGIKEY_CLIENT_ID/SECRET; broad catch-all, ~9/9)
    3. give up -> ask user to drop the PDF in manually

(Mouser was evaluated and dropped: 0/9 usable — it returns no datasheet for
most parts, and the URLs it does return are mouser.com-hosted behind a
bot-challenge wall that scripts can't download. DigiKey covers everything
Mouser did and more, without a bot wall.)

Every candidate PDF is validated (see validate_pdf):
    - %PDF magic bytes
    - has a real text layer (>= MIN_TEXT_CHARS extractable chars)
    - is NOT an LCSC/aggregator "quality certificate"

--report: try ALL sources for the part and print which ones yield a valid PDF
          (does not stop at first hit). Use this to measure per-source coverage
          on a machine where the paid APIs actually authenticate.

Why this file exists: the old version scraped the LCSC search page and grabbed
the first .pdf link — which was a "quality certificate", a valid but useless
image-only PDF. That silently poisoned STAGE 1.5. Never trust magic bytes alone.

Requires: requests, PyMuPDF (both in the ESP-IDF venv).
"""

import sys
import os
import re
import argparse

try:
    import requests
except ImportError:
    print("ERROR: requests not installed (run inside the ESP-IDF venv)")
    sys.exit(1)

TIMEOUT = 20
MIN_TEXT_CHARS = 400            # a real datasheet has plenty of text on p1-3
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
HEADERS = {"User-Agent": UA}

# Tier 0: parts whose datasheet URL we've already verified by hand.
# Manufacturer sites with unpredictable date/rev-stamped filenames (Winbond)
# or JS-rendered search pages can't be discovered by scraping, so pin them here.
# key = part number as passed on the CLI (case-insensitive match).
KNOWN_URLS = {
    "ESP32-PICO-D4": "https://documentation.espressif.com/esp32-pico_series_datasheet_en.pdf",
    "W25N01GV": "https://www.winbond.com/resource-files/"
                "w25n01gv%20revl%20050918%20unsecured.pdf",
}


# ────────────────────────── content validation ──────────────────────────

def _extract_text(pdf_bytes: bytes, max_pages: int = 3) -> str:
    """Return extractable text from the first max_pages pages, or ''."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        # No extractor available — fall back to a crude text-operator sniff so
        # we at least reject pure-image PDFs. Better than nothing.
        return "" if pdf_bytes.count(b"Tj") + pdf_bytes.count(b"TJ") < 50 else "sniff-ok"
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        n = min(max_pages, doc.page_count)
        text = "".join(doc[i].get_text() for i in range(n))
        doc.close()
        return text
    except Exception:
        return ""


def validate_pdf(data: bytes) -> tuple[bool, str]:
    """(ok, reason). Rejects non-PDFs, image-only scans, and certificates."""
    if not data or data[:4] != b"%PDF":
        return False, "not a PDF (bad magic)"
    text = _extract_text(data)
    if len(text) < MIN_TEXT_CHARS:
        return False, f"no/too-little text layer ({len(text)} chars) — likely image-only scan"
    low = text.lower()
    for bad in ("quality certificate", "certificate of", "合格证", "质量合格"):
        if bad in low:
            return False, f"looks like a certificate, not a datasheet ('{bad}')"
    return True, f"ok ({len(text)} text chars)"


def try_url(url: str) -> bytes | None:
    """GET a URL, return bytes only if it validates as a real datasheet PDF."""
    try:
        r = requests.get(url, timeout=TIMEOUT, allow_redirects=True, headers=HEADERS)
    except Exception as e:
        print(f"    [x] {url}\n        request failed: {e}")
        return None
    if r.status_code != 200:
        print(f"    [x] {url}  http={r.status_code}")
        return None
    ok, reason = validate_pdf(r.content)
    mark = "ok" if ok else "x"
    print(f"    [{mark}] {url}  ({len(r.content)//1024} kB) - {reason}")
    return r.content if ok else None


def _links_from_page(url: str) -> list[str]:
    """Fetch an HTML page and return absolute .pdf links found in it."""
    from urllib.parse import urljoin
    try:
        r = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
        if r.status_code != 200:
            return []
    except Exception:
        return []
    links = re.findall(r'https?://[^"\'\s<>]+\.pdf', r.text, re.I)
    rel = re.findall(r'href=["\']([^"\']+\.pdf)["\']', r.text, re.I)
    links += [urljoin(url, h) for h in rel]
    # de-dup, keep order
    seen, out = set(), []
    for l in links:
        if l not in seen:
            seen.add(l); out.append(l)
    return out


# ────────────────────────── ① manufacturer direct ──────────────────────────

def ti_candidates(part: str) -> list[str]:
    # TI base part = drop package/reel suffix after the last digit run.
    # BQ25180YBGR -> bq25180 ; TPS62840DRYR -> tps62840
    m = re.match(r"^([A-Za-z]+\d+)", part)
    base = m.group(1).lower() if m else part.lower()
    return [f"https://www.ti.com/lit/gpn/{base}",
            f"https://www.ti.com/lit/ds/symlink/{base}.pdf"]


def invensense_product_page(part: str) -> str:
    return f"https://invensense.tdk.com/products/{part.lower()}/"


def known_url(part: str) -> bytes | None:
    u = KNOWN_URLS.get(part.upper())
    if not u:
        print("  [0] known-URL table - no entry")
        return None
    print("  [0] known-URL table")
    return try_url(u)


def manufacturer_direct(part: str) -> bytes | None:
    p = part.upper()
    # --- Texas Instruments ---
    if re.match(r"^(BQ|TPS|TLV|LM|LP|INA|ADS|DAC|TMP|TCA|TXB|UCC|SN74|CD|CSD|OPA)", p):
        print("  [1] TI direct")
        for u in ti_candidates(part):
            d = try_url(u)
            if d: return d
    # --- TDK / InvenSense (motion + MEMS mic) ---
    if re.match(r"^(ICS|ICM|MPU|IIM|IAM|ICP|IMP)", p):
        print("  [1] TDK/InvenSense product page")
        page = invensense_product_page(part)
        for u in _links_from_page(page):
            if part.lower() in u.lower() or "ds-" in u.lower() or "datasheet" in u.lower():
                d = try_url(u)
                if d: return d
    # --- Winbond: URLs are date/rev-stamped & unpredictable; try product search page ---
    if re.match(r"^W25", p):
        print("  [1] Winbond product page")
        for page in (f"https://www.winbond.com/hq/search/?keyword={part}",
                     "https://www.winbond.com/hq/product/code-storage-flash-memory/"
                     "serial-nand-flash/"):
            for u in _links_from_page(page):
                if part.lower()[:6] in u.lower():
                    d = try_url(u)
                    if d: return d
    return None


# ────────────────────────── ② DigiKey API ──────────────────────────

_DK_TOKEN = None

def _digikey_token(cid: str, csec: str) -> str | None:
    global _DK_TOKEN
    if _DK_TOKEN:
        return _DK_TOKEN
    try:
        r = requests.post("https://api.digikey.com/v1/oauth2/token",
                          data={"client_id": cid, "client_secret": csec,
                                "grant_type": "client_credentials"}, timeout=TIMEOUT)
        _DK_TOKEN = r.json().get("access_token")
        if not _DK_TOKEN:
            print(f"    token failed: {r.json().get('ErrorMessage')}")
    except Exception as e:
        print(f"    token request failed: {e}")
    return _DK_TOKEN


def digikey_lookup(part: str) -> bytes | None:
    cid = os.environ.get("DIGIKEY_CLIENT_ID")
    csec = os.environ.get("DIGIKEY_CLIENT_SECRET")
    if not (cid and csec):
        print("  [2] DigiKey - skipped (no DIGIKEY_CLIENT_ID/SECRET)")
        return None
    print("  [2] DigiKey API")
    token = _digikey_token(cid, csec)
    if not token:
        return None
    try:
        r = requests.post("https://api.digikey.com/products/v4/search/keyword",
                          headers={"Authorization": f"Bearer {token}",
                                   "X-DIGIKEY-Client-Id": cid,
                                   "X-DIGIKEY-Locale-Site": "US",
                                   "X-DIGIKEY-Locale-Language": "en",
                                   "X-DIGIKEY-Locale-Currency": "USD",
                                   "Content-Type": "application/json"},
                          json={"Keywords": part, "Limit": 10}, timeout=TIMEOUT)
        prods = r.json().get("Products") or []
    except Exception as e:
        print(f"    request failed: {e}")
        return None
    urls = []
    for p in prods:
        u = p.get("DatasheetUrl")
        if u:
            if u.startswith("//"):
                u = "https:" + u
            urls.append(u)
    if not urls:
        print("    no DatasheetUrl in results")
    for u in dict.fromkeys(urls):
        d = try_url(u)
        if d: return d
    return None


# ────────────────────────── driver ──────────────────────────

SOURCES = [("known", known_url),
           ("manufacturer", manufacturer_direct),
           ("digikey", digikey_lookup)]


def fetch(part: str, out_dir: str, report: bool) -> str | None:
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{part}.pdf")

    if report:
        print(f"=== coverage report: {part} ===")
        hits = []
        for name, fn in SOURCES:
            data = fn(part)
            if data:
                hits.append(name)
        print(f"\nSOURCES yielding a valid text PDF for {part}: "
              f"{hits or 'NONE'}")
        return None

    if os.path.exists(out_path):
        print(f"[already exists] {out_path}")
        return out_path

    print(f"=== fetching datasheet: {part} ===")
    for name, fn in SOURCES:
        data = fn(part)
        if data:
            with open(out_path, "wb") as f:
                f.write(data)
            print(f"\n[OK] {part} via {name} -> {out_path} ({len(data)//1024} kB)")
            return out_path

    print(f"\n[FAIL] no valid datasheet for {part}.")
    print(f"       Drop the real PDF into {out_dir}/{part}.pdf manually.")
    return None


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("partnum")
    ap.add_argument("--out", default=".")
    ap.add_argument("--report", action="store_true",
                    help="try every source and report coverage (don't stop at first)")
    args = ap.parse_args()
    res = fetch(args.partnum, args.out, args.report)
    if not args.report and not res:
        sys.exit(1)
