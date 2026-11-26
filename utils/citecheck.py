"""Check every citation in a LaTeX draft against the full text of the cited paper.

The full texts live in a papertrail corpus (section-tree nodes in kb.sqlite, one node per
paragraph, table, figure caption or equation). For each cited paper, one model call reads
the whole paper (every non-reference node, prefixed by its node_id) together with every
place the draft cites it, and returns, per citation: what the draft attributes to the
paper, a verdict, and verbatim quotes with node_ids as evidence.

Then, deterministically:
  - every (citation occurrence, key) pair must have a verdict (coverage is asserted);
  - every quote must occur in the node it names (after normalizing markup). A verdict
    whose quotes cannot be found becomes "quote_not_found": a human must look.

Verdicts: supported, partially_supported, not_supported, names_only (the citation only
names a tool, dataset or paper and makes no claim about its content), quote_not_found,
no_fulltext. The model reads; the authors decide: each verdict links to the exact
paragraphs in the papertrail web app.
"""

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import unicodedata
from typing import Dict, List, Optional, Tuple

VERDICTS = ("supported", "partially_supported", "not_supported", "names_only")
MIN_QUOTE_WORDS = 6
PROMPT_VERSION = "citecheck-v1"

SYSTEM = """You are checking the citations in a draft astronomy paper against the full text \
of one cited paper. You read the whole paper. For each citation listed, decide what the \
draft attributes to this paper at that citation, and whether the paper supports it.

Rules:
- Judge only the part of the sentence attributed to THIS paper. In a grouped citation \
(\\citep{A,B,C}) a paper need only support its share; say what that share is.
- "names_only": the citation only names or points to this paper, tool or dataset \
(e.g. "LBLRTM \\citep{X}", "the IAG atlas \\citep{Y}") and the sentence makes no claim \
about its content beyond what it is. Still quote the passage that shows the paper is that \
tool, dataset or work.
- "supported": the paper states or directly shows the attributed claim.
- "partially_supported": part of the attributed claim is supported, or the paper says \
something weaker, narrower or different in a way an author should fix. Say exactly what.
- "not_supported": the paper does not say it (or contradicts it).
- Evidence: 1 to 4 quotes. Each quote must be copied character for character from the \
node text (you may drop markup like <sub>, * or _), at least 6 words long, and give the \
node_id it comes from. Never quote the reference list. For not_supported, quote the \
closest relevant passage if there is one, otherwise give no evidence.
- Do not use knowledge from outside this paper's text.

Return JSON only: {"results": [{"claim_id": "...", "attributed_claim": "...", \
"verdict": "...", "explanation": "one or two sentences", "evidence": [{"node_id": "...", \
"quote": "..."}]}]} with one entry for every claim_id given."""


# --- the draft ---------------------------------------------------------------------------

_CITE = re.compile(r"\\(cite[tp]?|citealt|citealp|citeauthor|citeyear)\*?((?:\[[^\]]*\])*)\{([^}]*)\}")


def cite_occurrences(tex: str) -> List[Dict]:
    """One record per (citation command, key) in the document body, with its paragraph."""
    start = tex.find("\\begin{document}")
    body_start = start + len("\\begin{document}") if start >= 0 else 0
    end = tex.find("\\bibliography", body_start)
    end = end if end >= 0 else len(tex)
    body = tex[body_start:end]
    line0 = tex[:body_start].count("\n")
    out = []
    pos = 0
    for para in re.split(r"\n\s*\n", body):
        p_off = body.find(para, pos)
        pos = p_off + len(para)
        clean = "\n".join(re.sub(r"(?<!\\)%.*", "", l) for l in para.splitlines())
        for m in _CITE.finditer(clean):
            keys = [k.strip() for k in m.group(3).split(",") if k.strip()]
            line = line0 + body[:p_off].count("\n") + clean[: m.start()].count("\n") + 1
            marked = clean[: m.start()] + "⟦" + m.group(0) + "⟧" + clean[m.end():]
            section = _section_before(body[:p_off])
            for k in keys:
                occ = f"L{line}:{m.group(0)}"
                out.append({"key": k, "line": line, "cite": m.group(0), "section": section,
                            "paragraph": re.sub(r"[ \t]+", " ", marked).strip(),
                            "claim_id": hashlib.sha1(f"{occ}|{k}".encode()).hexdigest()[:10]})
    return out


def _section_before(text: str) -> str:
    heads = re.findall(r"\\(?:sub)*section\*?\{([^}]*)\}", text)
    return heads[-1] if heads else ""


def bib_bibcodes(bib_text: str) -> Dict[str, Optional[str]]:
    out = {}
    for e in re.split(r"\n(?=@)", bib_text):
        m = re.match(r"\s*@\w+\{([^,]+),", e)
        if m:
            ads = re.search(r"adsurl\s*=\s*[{\"][^}\"]*/abs/([^}\"/]+)", e, re.I)
            out[m.group(1).strip()] = ads.group(1).replace("%26", "&") if ads else None
    return out


# --- the corpus ----------------------------------------------------------------------------

def _norm(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    s = re.sub(r"[*_`]", "", s)
    s = s.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    s = re.sub(r"\$|\\[a-zA-Z]+|[{}]", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


class Corpus:
    def __init__(self, sqlite_path: str):
        self.db = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)

    def has(self, doc_id: str) -> bool:
        r = self.db.execute("SELECT has_fulltext FROM docs WHERE doc_id=?", (doc_id,)).fetchone()
        return bool(r and r[0])

    def nodes(self, doc_id: str) -> List[Tuple[str, str, str, str]]:
        return self.db.execute(
            "SELECT node_id, block_type, coalesce(parent_title,''), text FROM nodes "
            "WHERE doc_id=? AND kind='leaf' AND block_type != 'reference' ORDER BY id",
            (doc_id,)).fetchall()

    def paper_text(self, doc_id: str) -> str:
        lines, last = [], None
        for nid, bt, parent, text in self.nodes(doc_id):
            if parent != last:
                lines.append(f"\n## {parent}")
                last = parent
            lines.append(f"[{nid}] ({bt}) {text}")
        return "\n".join(lines)


def check_quote(corpus: Corpus, doc_id: str, node_id: str, quote: str) -> Tuple[bool, str]:
    row = corpus.db.execute("SELECT text FROM nodes WHERE doc_id=? AND node_id=?",
                            (doc_id, node_id)).fetchone()
    if row is None:
        return False, "node does not exist"
    q = _norm(quote)
    if len(q.split()) < MIN_QUOTE_WORDS:
        return False, f"quote shorter than {MIN_QUOTE_WORDS} words"
    if q in _norm(row[0]):
        return True, ""
    # Quote correct but attributed to a neighbouring node?
    for (nid, text) in corpus.db.execute("SELECT node_id, text FROM nodes WHERE doc_id=?", (doc_id,)):
        if q in _norm(text):
            return False, f"quote found in node {nid}, not {node_id}"
    return False, "quote not found in the paper"


# --- the check -----------------------------------------------------------------------------

def _cache_key(occ: Dict, doc_id: str, model: str) -> str:
    return hashlib.sha1(json.dumps([PROMPT_VERSION, occ["paragraph"], occ["key"], doc_id, model]).encode()).hexdigest()


def run(tex: str, bib_text: str, sqlite_path: str, model: str, cache_path: str,
        llm_call, keys: Optional[List[str]] = None, extra_claims: Optional[List[Dict]] = None,
        log=print) -> List[Dict]:
    """Returns one row per (citation occurrence, key)."""
    occs = cite_occurrences(tex) + list(extra_claims or [])
    if keys:
        occs = [o for o in occs if o["key"] in keys]
    bibcodes = bib_bibcodes(bib_text)
    corpus = Corpus(sqlite_path)
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}

    by_doc: Dict[str, List[Dict]] = {}
    rows = []
    for o in occs:
        bib = o.get("bibcode") or bibcodes.get(o["key"])
        doc_id = bib.replace("&", "+") if bib else None
        o = {**o, "bibcode": bib, "doc_id": doc_id}
        if not doc_id or not corpus.has(doc_id):
            rows.append({**o, "verdict": "no_fulltext", "attributed_claim": "", "explanation":
                         "No full text in the reference corpus (not an ADS paper, or not downloaded).",
                         "evidence": []})
            continue
        ck = _cache_key(o, doc_id, model)
        if ck in cache:
            rows.append({**o, **cache[ck], "cached": True})
            continue
        by_doc.setdefault(doc_id, []).append(o)

    for doc_id, items in by_doc.items():
        log(f"   reading {doc_id} for {len(items)} citation(s)")
        claims = "\n\n".join(
            f"claim_id: {o['claim_id']}\ncitation key: {o['key']} (the cited paper)\n"
            f"section of the draft: {o['section']}\n"
            f"paragraph (the citation being checked is inside ⟦ ⟧):\n{o['paragraph']}" for o in items)
        prompt = (f"{SYSTEM}\n\n=== THE CITED PAPER ({doc_id}) ===\n{corpus.paper_text(doc_id)}\n"
                  f"=== END OF PAPER ===\n\n=== CITATIONS TO CHECK ===\n{claims}")
        parsed = llm_call(prompt) or {}
        got = {r.get("claim_id"): r for r in (parsed.get("results") or []) if isinstance(r, dict)}
        for o in items:
            r = got.get(o["claim_id"])
            if r is None:
                res = {"verdict": "model_gave_no_verdict", "attributed_claim": "", "explanation":
                       "The model returned no verdict for this citation; rerun or check by hand.",
                       "evidence": []}
            else:
                res = _verify(corpus, doc_id, r)
                cache[_cache_key(o, doc_id, model)] = res
            rows.append({**o, **res})
        with open(cache_path, "w") as f:
            json.dump(cache, f, indent=1, ensure_ascii=False)

    assert len(rows) == len(occs), "every citation must get a row"
    return rows


def _verify(corpus: Corpus, doc_id: str, r: Dict) -> Dict:
    verdict = r.get("verdict") if r.get("verdict") in VERDICTS else "model_gave_no_verdict"
    evidence = []
    for e in r.get("evidence") or []:
        ok, why = check_quote(corpus, doc_id, str(e.get("node_id", "")), str(e.get("quote", "")))
        evidence.append({"node_id": str(e.get("node_id", "")), "quote": e.get("quote", ""),
                         "quote_ok": ok, "problem": why})
    model_verdict = verdict
    if verdict in ("supported", "partially_supported", "names_only"):
        if not evidence or not all(e["quote_ok"] for e in evidence):
            verdict = "quote_not_found"
    return {"verdict": verdict, "model_verdict": model_verdict,
            "attributed_claim": r.get("attributed_claim", ""),
            "explanation": r.get("explanation", ""), "evidence": evidence}


# --- publishing and reporting --------------------------------------------------------------

def answer_payloads(rows: List[Dict]) -> Dict[str, Dict]:
    """One papertrail answer payload per cited paper (results = its citations)."""
    out: Dict[str, Dict] = {}
    for r in rows:
        if not r.get("doc_id") or r["verdict"] == "no_fulltext":
            continue
        cites, marks = [], []
        for e in r["evidence"]:
            if e["quote_ok"]:
                n = len(cites) + 1
                cites.append({"id": n, "doc_id": r["doc_id"], "node_id": e["node_id"]})
                marks.append(f"> \"{e['quote']}\" [{n}]")
        answer = (f"**{r['verdict'].replace('_', ' ')}** (model said: {r.get('model_verdict', r['verdict'])})\n\n"
                  f"**Draft, line {r['line']}:** {r['attributed_claim']}\n\n{r['explanation']}\n\n"
                  + "\n\n".join(marks))
        bad = [e for e in r["evidence"] if not e["quote_ok"]]
        if bad:
            answer += "\n\nQuotes that could not be verified: " + "; ".join(
                f"node {e['node_id']}: {e['problem']}" for e in bad)
        res = {"question": f"{r['key']} at line {r['line']}", "answer": answer, "citations": cites}
        out.setdefault(r["doc_id"], {"results": []})["results"].append(res)
    return out


def publish(payloads: Dict[str, Dict], corpus_root: str, papertrail_python: str, helper: str,
            tmp_path: str) -> Dict[str, Dict]:
    with open(tmp_path, "w") as f:
        json.dump(payloads, f, ensure_ascii=False)
    r = subprocess.run([papertrail_python, "-I", helper, corpus_root, tmp_path],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"papertrail publish failed: {r.stderr[-2000:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


ORDER = ["not_supported", "quote_not_found", "model_gave_no_verdict", "partially_supported",
         "no_fulltext", "supported", "names_only"]


def write_report(rows: List[Dict], published: Dict[str, Dict], tex_path: str, out_md: str,
                 out_json: str, model: str, base_url: str):
    with open(out_json, "w") as f:
        json.dump({"tex": tex_path, "model": model, "prompt_version": PROMPT_VERSION,
                   "published": published, "rows": rows}, f, indent=1, ensure_ascii=False)
    counts = {v: sum(r["verdict"] == v for r in rows) for v in ORDER}
    L = [f"# Citation check: {os.path.basename(tex_path)}", "",
         f"Model: {model}. Each cited paper was read in full from the reference corpus. "
         "Quotes were checked against the paper's text by script. Open a paper's link to see "
         "each verdict with clickable quotes in the papertrail reader "
         "(`papertrail serve --corpus refs_corpus`).", "",
         "| verdict | count |", "|---|---|"] + [f"| {v} | {c} |" for v, c in counts.items() if c]
    L += ["", "| line | key | verdict | what the draft attributes | explanation | open |",
          "|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: (ORDER.index(r["verdict"]) if r["verdict"] in ORDER else 0, r["line"])):
        pub = published.get(r.get("doc_id") or "", {})
        link = f"[answer]({base_url}/#answer={pub['id']})" if pub.get("id") else ""
        cell = lambda s: (s or "").replace("|", "\\|").replace("\n", " ")
        L.append(f"| {r['line']} | {r['key']} | **{r['verdict']}** | {cell(r['attributed_claim'])} | "
                 f"{cell(r['explanation'])} | {link} |")
    with open(out_md, "w") as f:
        f.write("\n".join(L) + "\n")
    return counts
