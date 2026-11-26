# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import html
import os
import re
import requests
from typing import List, Dict, Optional
from thefuzz import fuzz  # type: ignore
import datetime

from dotenv import load_dotenv  # type: ignore

cur_dir = os.path.dirname(os.path.realpath(__file__))
load_dotenv(os.path.join(cur_dir, "../.env"))
S2_API_KEY = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
ADS_API_TOKEN = os.environ.get("ADS_API_TOKEN")
ADS_API_URL = os.environ.get(
    "ADS_API_URL", "https://api.adsabs.harvard.edu/v1"
).rstrip("/")

# Which bibliographic service verifies candidate papers: "ads" or "s2".
# Defaults to ADS when an ADS token is configured.
LIT_SEARCH_BACKEND = os.environ.get(
    "LIT_SEARCH_BACKEND", "ads" if ADS_API_TOKEN else "s2"
).lower()
if LIT_SEARCH_BACKEND not in ("ads", "s2"):
    raise ValueError(f"LIT_SEARCH_BACKEND must be 'ads' or 's2', got {LIT_SEARCH_BACKEND!r}")
if LIT_SEARCH_BACKEND == "ads" and not ADS_API_TOKEN:
    raise ValueError("LIT_SEARCH_BACKEND=ads requires ADS_API_TOKEN to be set.")

# Seconds to wait between lookups. Unauthenticated S2 needs ~1 req/s;
# ADS allows 5000 queries/day with no tight per-second limit.
SEARCH_DELAY_SEC = 0.2 if LIT_SEARCH_BACKEND == "ads" else 1.0

print(
    f"Literature lookup backend: {LIT_SEARCH_BACKEND} "
    f"(S2 key set: {bool(S2_API_KEY)}, ADS token set: {bool(ADS_API_TOKEN)})"
)


# --- SHARED MATCHING ---


def _is_date_valid(p_date, p_year, cutoff_str):
    if not cutoff_str:
        return True
    try:
        c_year, c_month = map(int, cutoff_str.split("-"))
        cutoff_dt = datetime.datetime(c_year, c_month, 1)
        if p_date:
            try:
                p_dt = datetime.datetime.strptime(p_date, "%Y-%m-%d")
                return p_dt < cutoff_dt
            except ValueError:
                pass
        if p_year:
            if p_year < c_year:
                return True
            if p_year == c_year and c_month > 1:
                return True
            return False
        return True
    except Exception:
        return True


def _pick_best_match(
    results: List[Dict], title_query: str, year_hint: int, cutoff_date: str
) -> Optional[Dict]:
    """Return the result whose title best fuzzy-matches the query (score > 70).

    Series papers ("Molecfit: ... I. Method and ...") are often cited by their
    leading title only, so the leading part of each title is also compared.
    Ties are broken by citation count.
    """
    best_match = None
    best_key = (0, 0)
    q = title_query.lower().strip()

    for r in results:
        if not r.get("title"):
            continue
        if not _is_date_valid(r.get("publicationDate"), r.get("year"), cutoff_date):
            continue

        t = r["title"].lower()
        ratio = fuzz.ratio(q, t)
        if len(q) >= 20 and len(t) > len(q):
            ratio = max(ratio, fuzz.ratio(q, t[: len(q)]))
        if year_hint and r.get("year") == year_hint:
            ratio += 10

        key = (ratio, r.get("citationCount") or 0)
        if key > best_key:
            best_key = key
            best_match = r

    if best_key[0] > 70:
        return best_match
    return None


# --- SEMANTIC SCHOLAR UTILS ---


def s2_title_search(
    title_query: str, year_hint: int = None, cutoff_date: str = None
) -> Optional[Dict]:
    headers = {"X-API-KEY": S2_API_KEY} if S2_API_KEY else {}

    try:
        rsp = requests.get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            headers=headers,
            params={
                "query": title_query,
                "limit": 3,
                "fields": "title,authors,venue,year,abstract,citationCount,journal,publicationDate",
            },
            timeout=5,
        )
        if rsp.status_code != 200:
            return None
        results = rsp.json().get("data", [])
        if not results:
            return None
        return _pick_best_match(results, title_query, year_hint, cutoff_date)
    except Exception as e:
        print(f"      ⚠️ S2 Error for '{title_query}': {e}")
        return None


# --- NASA ADS / SciX UTILS ---

_ADS_FIELDS = "bibcode,title,author,year,pubdate,pub,volume,page,abstract,citation_count,doi"


def _ads_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {ADS_API_TOKEN}"}


def _clean_ads_text(text: str) -> str:
    """Strip HTML tags/entities that ADS embeds in titles and abstracts."""
    text = re.sub(r"<[^>]+>", "", text or "")
    return html.unescape(text).strip()


def _ads_author_to_name(author: str) -> str:
    """'Vaswani, Ashish' -> 'Ashish Vaswani' (so the last token is the surname)."""
    parts = [p.strip() for p in author.split(",")]
    if len(parts) < 2:
        return author.strip()
    surname, rest = parts[0], [p for p in parts[1:] if p]
    suffixes = [p for p in rest if p.rstrip(".").lower() in ("jr", "sr", "ii", "iii", "iv")]
    given = [p for p in rest if p not in suffixes]
    return " ".join(given + [surname] + suffixes).strip()


def _ads_pubdate(pubdate: Optional[str]) -> Optional[str]:
    """ADS uses 'YYYY-MM-00'; drop zero month/day components."""
    if not pubdate:
        return None
    parts = [p for p in pubdate.split("-") if p and int(p) != 0]
    return "-".join(parts) or None


def _ads_doc_to_paper(doc: Dict) -> Dict:
    """Convert an ADS doc into the Semantic Scholar-shaped dict the agents use."""
    title = _clean_ads_text((doc.get("title") or [""])[0])
    pub = doc.get("pub")
    pages = (doc.get("page") or [None])[0]
    year = doc.get("year")
    return {
        "title": title,
        "authors": [{"name": _ads_author_to_name(a)} for a in doc.get("author") or []],
        "venue": pub,
        "year": int(year) if year and str(year).isdigit() else None,
        "abstract": _clean_ads_text(doc.get("abstract") or ""),
        "citationCount": doc.get("citation_count") or 0,
        "journal": {"name": pub, "volume": doc.get("volume"), "pages": pages}
        if pub
        else None,
        "publicationDate": _ads_pubdate(doc.get("pubdate")),
        "bibcode": doc.get("bibcode"),
        "doi": (doc.get("doi") or [None])[0],
    }


def _ads_query(q: str, rows: int = 5) -> List[Dict]:
    rsp = requests.get(
        f"{ADS_API_URL}/search/query",
        headers=_ads_headers(),
        params={"q": q, "fl": _ADS_FIELDS, "rows": rows},
        timeout=10,
    )
    if rsp.status_code != 200:
        print(f"      ⚠️ ADS HTTP {rsp.status_code} for q={q!r}: {rsp.text[:200]}")
        return []
    return rsp.json().get("response", {}).get("docs", [])


def ads_title_search(
    title_query: str, year_hint: int = None, cutoff_date: str = None
) -> Optional[Dict]:
    """Look up a paper by title on NASA ADS / SciX.

    Tries an exact title phrase first, then falls back to all title words.
    """
    clean = re.sub(r'["\\]', " ", title_query).strip()
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]*", clean)
    queries = [f'title:"{clean}"']
    if words:
        queries.append("title:(" + " ".join(words) + ")")

    try:
        for q in queries:
            docs = _ads_query(q)
            if not docs:
                continue
            best = _pick_best_match(
                [_ads_doc_to_paper(d) for d in docs], title_query, year_hint, cutoff_date
            )
            if best:
                return best
        return None
    except Exception as e:
        print(f"      ⚠️ ADS Error for '{title_query}': {e}")
        return None


def ads_bibcode_lookup(bibcodes: List[str]) -> Dict[str, Dict]:
    """Fetch papers by ADS bibcode. Returns {bibcode: paper dict} for those found."""
    out = {}
    for bib in dict.fromkeys(b.strip() for b in bibcodes if b.strip()):
        docs = _ads_query(f'identifier:"{bib}"', rows=1)
        if docs:
            out[bib] = _ads_doc_to_paper(docs[0])
    return out


def ads_export_bibtex(bibcodes: List[str]) -> Dict[str, str]:
    """Fetch ADS BibTeX entries. Returns {bibcode: bibtex_entry}."""
    bibcodes = [b for b in dict.fromkeys(bibcodes) if b]
    if not bibcodes:
        return {}
    out = {}
    for i in range(0, len(bibcodes), 1000):
        rsp = requests.post(
            f"{ADS_API_URL}/export/bibtex",
            headers=_ads_headers(),
            json={"bibcode": bibcodes[i : i + 1000]},
            timeout=60,
        )
        rsp.raise_for_status()
        export = rsp.json().get("export", "")
        for entry in re.split(r"\n(?=@)", export.strip()):
            m = re.match(r"@\w+\{([^,]+),", entry)
            if m:
                out[m.group(1).strip()] = entry.strip()
    return out


# --- BACKEND DISPATCH ---


def paper_title_search(
    title_query: str, year_hint: int = None, cutoff_date: str = None
) -> Optional[Dict]:
    """Verify a candidate paper using the configured backend (ADS or S2)."""
    if LIT_SEARCH_BACKEND == "ads":
        return ads_title_search(title_query, year_hint, cutoff_date)
    return s2_title_search(title_query, year_hint, cutoff_date)
