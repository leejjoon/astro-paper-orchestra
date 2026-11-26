"""Fetch the published full text (EPUB first, then PDF) of cited papers, publisher-direct.

No preprint servers and no bot-protection workarounds: a paper whose publisher answers
with a captcha or a login page is reported as failed, for the authors to download by hand
and drop into the sources directory as <safe_bibcode>.epub or <safe_bibcode>.pdf.

Order per paper:
  1. a file already in the sources directory (manual downloads are first-class);
  2. EPUB: IOP/AAS journals serve /article/<doi>/epub; otherwise any EPUB link on the
     DOI landing page;
  3. PDF: the ADS link gateway (PUB_PDF), then the landing page's citation_pdf_url.

State lives in <sources>/manifest.json, one record per bibcode.
"""

import json
import os
import re
import time
import zipfile
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import urljoin

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Accept": "text/html,application/xhtml+xml,application/pdf,application/epub+zip;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}
GATEWAY = "https://ui.adsabs.harvard.edu/link_gateway/{bibcode}/PUB_PDF"
MIN_BYTES = 20_000
SLEEP = 1.5


def safe_bibcode(bibcode: str) -> str:
    return bibcode.replace("&", "+")


def _is_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-" and len(data) >= MIN_BYTES


def _is_epub(path: str) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            return z.read("mimetype").strip() == b"application/epub+zip"
    except (zipfile.BadZipFile, KeyError, OSError):
        return False


def _get(session: requests.Session, url: str) -> Optional[requests.Response]:
    try:
        return session.get(url, headers=HEADERS, timeout=60, allow_redirects=True)
    except requests.RequestException:
        return None


def _save(data: bytes, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _try_epub(session, doi: str, landing_html: str, landing_url: str, out: str) -> Optional[str]:
    candidates = []
    if doi.startswith(("10.3847/", "10.1088/")):
        candidates.append(f"https://iopscience.iop.org/article/{doi}/epub")
    for href in re.findall(r'href="([^"]+)"', landing_html or ""):
        if re.search(r"\bepub\b|\.epub\b|/epub/", href, re.I) and "page/ePub" not in href:
            candidates.append(urljoin(landing_url, href))
    for url in dict.fromkeys(candidates):
        r = _get(session, url)
        if r is not None and r.status_code == 200 and r.content[:2] == b"PK":
            _save(r.content, out)
            if _is_epub(out):
                return url
            os.remove(out)
    return None


def _try_pdf(session, bibcode: str, landing_html: str, landing_url: str, out: str) -> Optional[str]:
    candidates = [] if bibcode.startswith("doi-") else [GATEWAY.format(bibcode=bibcode)]
    m = re.search(r'<meta[^>]+name="citation_pdf_url"[^>]+content="([^"]+)"', landing_html or "")
    if m:
        candidates.append(urljoin(landing_url, m.group(1)))
    for url in candidates:
        r = _get(session, url)
        if r is not None and r.status_code == 200 and _is_pdf(r.content):
            _save(r.content, out)
            return url
    return None


def _failure_reason(landing: Optional[requests.Response]) -> str:
    if landing is None:
        return "landing page unreachable"
    text = (landing.text or "")[:20000].lower()
    if "captcha" in text or "just a moment" in text or "cloudflare" in text:
        return f"bot protection on landing page (HTTP {landing.status_code})"
    if landing.status_code in (401, 403):
        return f"access denied (HTTP {landing.status_code})"
    return f"no EPUB/PDF link served (landing HTTP {landing.status_code})"


def fetch_all(papers: List[Dict], sources_dir: str, retry_failed: bool = False) -> Dict[str, Dict]:
    """papers: [{bibcode, doi, key}] -> manifest {bibcode: record}."""
    os.makedirs(sources_dir, exist_ok=True)
    manifest_path = os.path.join(sources_dir, "manifest.json")
    manifest = json.load(open(manifest_path)) if os.path.exists(manifest_path) else {}
    session = requests.Session()
    for p in papers:
        bib, doi = p.get("bibcode"), p.get("doi")
        if not bib:
            continue
        stem = os.path.join(sources_dir, safe_bibcode(bib))
        rec = manifest.get(bib, {})
        existing = [f"{stem}.{ext}" for ext in ("epub", "pdf") if os.path.exists(f"{stem}.{ext}")]
        if existing:
            if rec.get("status") != "ok" or rec.get("file") != os.path.basename(existing[0]):
                rec = {"status": "ok", "file": os.path.basename(existing[0]), "source": "manual"}
            manifest[bib] = {**rec, "key": p.get("key"), "doi": doi}
            continue
        if rec.get("status") == "failed" and not retry_failed:
            continue
        rec = {"key": p.get("key"), "doi": doi, "tried": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if not doi:
            rec.update(status="failed", reason="no DOI (book, atlas or not a journal article?)")
            manifest[bib] = rec
            continue
        if doi.startswith("10.48550/"):
            rec.update(status="failed", reason="arXiv-only record (publisher versions only)")
            manifest[bib] = rec
            continue
        landing = _get(session, f"https://doi.org/{doi}")
        html = landing.text if landing is not None and landing.status_code == 200 else ""
        url_base = landing.url if landing is not None else f"https://doi.org/{doi}"
        got = _try_epub(session, doi, html, url_base, f"{stem}.epub")
        if got:
            rec.update(status="ok", file=os.path.basename(stem) + ".epub", source=got)
        else:
            got = _try_pdf(session, bib, html, url_base, f"{stem}.pdf")
            if got:
                rec.update(status="ok", file=os.path.basename(stem) + ".pdf", source=got)
            else:
                rec.update(status="failed", reason=_failure_reason(landing),
                           landing=url_base)
        manifest[bib] = rec
        print(f"   {bib:22s} {rec['status']:6s} {rec.get('file') or rec.get('reason')}")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)
        time.sleep(SLEEP)
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    return manifest


def cited_papers(tex: str, bib_text: str) -> List[Dict]:
    """[{key, bibcode, doi}] for every key cited in the .tex, from ADS-exported BibTeX."""
    keys = set()
    for group in re.findall(r"\\cite\w*(?:\[[^\]]*\])*\{([^}]*)\}", tex):
        keys.update(k.strip() for k in group.split(",") if k.strip())
    entries = {}
    for e in re.split(r"\n(?=@)", bib_text):
        m = re.match(r"@\w+\{([^,]+),", e.strip())
        if m:
            entries[m.group(1).strip()] = e
    out = []
    for k in sorted(keys):
        e = entries.get(k, "")
        doi_field = re.search(r"\bdoi\s*=\s*[{\"]([^}\"]+)", e, re.I)
        dois = re.findall(r"10\.\d{4,9}/.+?(?=\s*,|\s|10\.\d{4,9}/|$)", doi_field.group(1)) if doi_field else []
        ads = re.search(r"adsurl\s*=\s*[{\"][^}\"]*/abs/([^}\"/]+)", e, re.I)
        bibcode = ads.group(1).replace("%26", "&") if ads else (
            "doi-" + dois[0].replace("/", "_") if dois else None)  # no ADS record: DOI-based id
        out.append({"key": k, "bibcode": bibcode,
                    "doi": dois[0] if dois else None})
    return out
