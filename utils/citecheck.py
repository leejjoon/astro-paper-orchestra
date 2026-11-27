"""Check every citation in a LaTeX draft against the full text of the cited paper.

The full texts live in a papertrail corpus (kb.sqlite: one node per paragraph, table,
figure caption or equation). Two ways of reading a cited paper:

  full    one model call reads the whole paper (every non-reference node, with its
          node_id) and every citation of it in the draft. The first full read of a paper
          also writes a detailed summary of it, every point tied to node_ids, which is
          saved in <corpus>/summaries/<doc_id>.json.
  routed  for a paper that already has a summary: one call reads the summary and picks,
          per citation, the passages that could support AND the passages that could
          qualify or contradict the claim; a second call judges from those passages
          plus every passage the summary lists under scope limits, things not done,
          limitations and status. Any verdict other than supported/names_only, and any
          "not enough in the excerpts", is escalated to a full read.

Routed reading is for re-checks after edits; the final pass before submission should be
a full read (--mode full).

After the model, deterministically:
  - every (citation occurrence, key) pair must get a row (asserted);
  - every quote must occur in the node it names (after normalizing markup); a verdict
    whose quotes cannot be found becomes "quote_not_found".

Verdicts: supported, partially_supported, not_supported, names_only, quote_not_found,
no_fulltext. Results are cached per (citing sentence + the sentence before it, key,
paper, model, prompt version), so editing one sentence re-checks only that sentence.
"""

import hashlib
import html
import json
import os
import re
import sqlite3
import subprocess
import time
import unicodedata
from typing import Callable, Dict, List, Optional, Tuple

VERDICTS = ("supported", "partially_supported", "not_supported", "names_only")
GOOD = ("supported", "names_only")
MIN_QUOTE_WORDS = 6
PROMPT_VERSION = "citecheck-v2"

RULES = """Rules:
- Judge only the part of the sentence attributed to THIS paper. In a grouped citation \
(\\citep{A,B,C}) a paper need only support its share; say what that share is.
- "names_only": the citation only names or points to this paper, tool or dataset \
(e.g. "LBLRTM \\citep{X}", "the IAG atlas \\citep{Y}") and the sentence makes no claim \
about its content beyond what it is. Still quote the passage that shows the paper is that \
tool, dataset or work.
- "supported": the paper states or directly shows the attributed claim, and nothing in \
the paper qualifies it (e.g. a different code than the draft implies, a capability the \
paper says it lacks, something only proposed or planned, a number that differs).
- "partially_supported": part of the attributed claim is supported, or the paper says \
something weaker, narrower or different in a way an author should fix. Say exactly what.
- "not_supported": the paper does not say it (or contradicts it).
- Evidence: 1 to 4 quotes. Each quote must be copied character for character from the \
node text (you may drop markup like <sub>, * or _), at least 6 words long, and give the \
node_id it comes from. Never quote the reference list. For not_supported, quote the \
closest relevant passage if there is one, otherwise give no evidence. When a qualifying \
passage is the reason for partially_supported, quote it.
- Do not use knowledge from outside this paper's text."""

RESULT_SCHEMA = """{"claim_id": "...", "attributed_claim": "...", "verdict": "...", \
"explanation": "one or two sentences", "evidence": [{"node_id": "...", "quote": "..."}]}"""

SUMMARY_SCHEMA = """"summary": {
  "scope": [POINT, ...],            what the paper is about and what it sets out to do
  "data_and_codes": [POINT, ...],   instruments, data sets, codes, line lists, versions it uses
  "methods": [POINT, ...],
  "results": [POINT, ...],          main results, with the numbers the paper gives
  "comparisons": [POINT, ...],      what it compares with what, and the outcome
  "not_done": [POINT, ...],         what it explicitly does NOT do, fit, compare or claim
  "limitations": [POINT, ...],      limitations and caveats the authors state
  "status": [POINT, ...],           anything only proposed, planned or future (e.g. "will be implemented")
  "section_map": [{"section": "<heading as shown>", "about": "..."}]
}
where POINT = {"text": "a specific statement, with numbers where the paper gives them", "node_ids": ["...", ...]}"""


# --- the draft ---------------------------------------------------------------------------

_CITE = re.compile(r"\\(cite[tp]?|citealt|citealp|citeauthor|citeyear)\*?((?:\[(?:[^\[\]]|\{[^{}]*\})*\])*)\{([^}]*)\}")
_ABBREV = re.compile(r"(?:\bet al|\be\.g|\bi\.e|\bcf|\bvs|\bFigs?|\bEqs?|\bSect|\bRef|\bapprox|\bResp|\bNo)\.$")


def _sentences(text: str) -> List[Tuple[int, int]]:
    """Sentence spans; avoids splitting after 'et al.', 'e.g.', 'Fig.' and the like."""
    spans, start = [], 0
    for m in re.finditer(r"[.!?:](?=\s+(?:[A-Za-z$(`]|\\item\b|\\begin\b|\\end\b|\\[A-Za-z]))|(?=\\item\b)", text):
        if m.end() <= start or _ABBREV.search(text[max(0, m.start() - 8): m.end()]):
            continue
        spans.append((start, m.end()))
        start = m.end()
    spans.append((start, len(text)))
    return spans


def cite_occurrences(tex: str) -> List[Dict]:
    """One record per (citation command, key) in the document body."""
    start = tex.find("\\begin{document}")
    body_start = start + len("\\begin{document}") if start >= 0 else 0
    end = tex.find("\\bibliography", body_start)
    end = end if end >= 0 else len(tex)
    body = tex[body_start:end]
    line0 = tex[:body_start].count("\n")
    out, pos = [], 0
    for para in re.split(r"\n\s*\n", body):
        p_off = body.find(para, pos)
        pos = p_off + len(para)
        clean = "\n".join(re.sub(r"(?<!\\)%.*", "", l) for l in para.splitlines())
        flat = re.sub(r"\s+", " ", clean)
        spans = _sentences(flat)
        section = _section_before(body[:p_off])
        for m in _CITE.finditer(clean):
            keys = [k.strip() for k in m.group(3).split(",") if k.strip()]
            line = line0 + body[:p_off].count("\n") + clean[: m.start()].count("\n") + 1
            marked = clean[: m.start()] + "⟦" + m.group(0) + "⟧" + clean[m.end():]
            # the citing sentence (and the one before) in the whitespace-flattened paragraph
            fpos = len(re.sub(r"\s+", " ", clean[: m.start()]))
            si = next(i for i, (a, b) in enumerate(spans) if a <= fpos < b or i == len(spans) - 1)
            a, b = spans[si]
            sentence = (flat[a:fpos] + "⟦" + m.group(0) + "⟧" + flat[fpos + len(m.group(0)):b]).strip()
            prev = flat[spans[si - 1][0]: spans[si - 1][1]].strip() if si > 0 else ""
            for k in keys:
                out.append({"key": k, "line": line, "cite": m.group(0), "section": section,
                            "paragraph": re.sub(r"[ \t]+", " ", marked).strip(),
                            "sentence": sentence, "prev_sentence": prev,
                            "claim_id": hashlib.sha1(f"{line}|{m.group(0)}|{k}".encode()).hexdigest()[:10]})
    return out


def _section_before(text: str) -> str:
    heads = re.findall(r"\\(?:sub)*section\*?\{([^}]*)\}", text)
    return heads[-1] if heads else ""


def doi_id(doi: str) -> str:
    """Corpus id for a paper with no ADS bibcode: 'doi-' + the DOI with '/' as '_'."""
    return "doi-" + doi.strip().replace("/", "_")


def bib_bibcodes(bib_text: str) -> Dict[str, Optional[str]]:
    out = {}
    for e in re.split(r"\n(?=@)", bib_text):
        m = re.match(r"\s*@\w+\{([^,]+),", e)
        if m:
            ads = re.search(r"adsurl\s*=\s*[{\"][^}\"]*/abs/([^}\"/]+)", e, re.I)
            doi = re.search(r"\bdoi\s*=\s*[{\"]\s*(10\.[^}\",\s]+)", e, re.I)
            out[m.group(1).strip()] = (ads.group(1).replace("%26", "&") if ads
                                       else doi_id(doi.group(1)) if doi else None)
    return out


# --- the corpus ----------------------------------------------------------------------------

def _norm(s: str, tag: str = "") -> str:
    """tag="" joins inline markup (H<sub>2</sub>O -> h2o); tag=" " keeps table cells apart;
    tag="fn" also drops footnote markers (unsupervised<sup>1</sup> deep -> unsupervised deep)."""
    if tag == "fn":
        s, tag = re.sub(r"<sup>[\s\d,*\u2020\u2021\u00a7]+</sup>", "", s or ""), ""
    s = html.unescape(re.sub(r"<[^>]+>", tag, s or ""))
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    s = re.sub(r"[*_`]", "", s)
    s = s.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    s = re.sub(r"\$|\\[a-zA-Z]+|[{}]", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


class Corpus:
    def __init__(self, corpus_root: str):
        self.root = corpus_root
        self.db = sqlite3.connect(f"file:{os.path.join(corpus_root, 'data', 'kb.sqlite')}?mode=ro", uri=True)
        self.summary_dir = os.path.join(corpus_root, "summaries")

    def has(self, doc_id: str) -> bool:
        r = self.db.execute("SELECT has_fulltext FROM docs WHERE doc_id=?", (doc_id,)).fetchone()
        return bool(r and r[0])

    def nodes(self, doc_id: str) -> List[Tuple[str, str, str, str]]:
        return self.db.execute(
            "SELECT node_id, block_type, coalesce(parent_title,''), text FROM nodes "
            "WHERE doc_id=? AND kind='leaf' AND block_type != 'reference' ORDER BY id",
            (doc_id,)).fetchall()

    def node_meta(self, doc_id: str) -> Dict[str, Dict]:
        """node_id -> {section, block_type, page (1-based, None for EPUB)} for every node of a paper."""
        rows = self.db.execute("SELECT node_id, block_type, coalesce(parent_title,''), page FROM nodes "
                               "WHERE doc_id=?", (doc_id,)).fetchall()
        return {n: {"section": s.strip("*").strip(), "block_type": b,
                    "page": (pg + 1) if isinstance(pg, int) else None} for n, b, s, pg in rows}

    def node_ids(self, doc_id: str) -> set:
        return {n[0] for n in self.nodes(doc_id)}

    def paper_text(self, doc_id: str, only: Optional[set] = None) -> str:
        lines, last, skipped = [], None, False
        for nid, bt, parent, text in self.nodes(doc_id):
            if only is not None and nid not in only:
                skipped = True
                continue
            if parent != last or skipped:
                lines.append(f"\n## {parent}" + (" (excerpt)" if only is not None else ""))
                last, skipped = parent, False
            lines.append(f"[{nid}] ({bt}) {text}")
        return "\n".join(lines)

    def expand(self, doc_id: str, picks: set, sections: set, around: int = 2) -> set:
        """Picked nodes plus `around` neighbours in the same section, and whole picked sections."""
        nodes = self.nodes(doc_id)
        keep = set()
        for i, (nid, _, parent, _) in enumerate(nodes):
            if parent in sections:
                keep.add(nid)
            if nid in picks:
                for j in range(max(0, i - around), min(len(nodes), i + around + 1)):
                    if nodes[j][2] == parent:
                        keep.add(nodes[j][0])
        return keep

    def fingerprint(self, doc_id: str) -> str:
        h = hashlib.sha1()
        for nid, _, _, text in self.nodes(doc_id):
            h.update(f"{nid}\x00{text}\x00".encode())
        return h.hexdigest()[:16]

    # summaries
    def summary(self, doc_id: str) -> Optional[Dict]:
        """The saved summary, or None if there is none or the paper's text has changed since
        (e.g. it was re-converted), because its node_ids would then point at the wrong text."""
        path = os.path.join(self.summary_dir, f"{doc_id}.json")
        if not os.path.exists(path):
            return None
        s = json.load(open(path))
        return s if s.get("index_fingerprint") == self.fingerprint(doc_id) else None

    def save_summary(self, doc_id: str, summary: Dict, model: str) -> None:
        os.makedirs(self.summary_dir, exist_ok=True)
        valid = self.node_ids(doc_id)
        for field, points in summary.items():
            if isinstance(points, list):
                for p in points:
                    if isinstance(p, dict) and "node_ids" in p:
                        p["node_ids"] = [n for n in p.get("node_ids") or [] if n in valid]
        with open(os.path.join(self.summary_dir, f"{doc_id}.json"), "w") as f:
            json.dump({"doc_id": doc_id, "model": model, "prompt_version": PROMPT_VERSION,
                       "index_fingerprint": self.fingerprint(doc_id), "summary": summary},
                      f, indent=1, ensure_ascii=False)


def check_quote(corpus: Corpus, doc_id: str, node_id: str, quote: str) -> Tuple[bool, str]:
    row = corpus.db.execute("SELECT text FROM nodes WHERE doc_id=? AND node_id=?",
                            (doc_id, node_id)).fetchone()
    if row is None:
        return False, "node does not exist"
    variants = [(_norm(quote, tag), tag) for tag in ("", " ", "fn")]
    if all(len(q.split()) < MIN_QUOTE_WORDS for q, _ in variants):
        return False, f"quote shorter than {MIN_QUOTE_WORDS} words"
    variants = [(q, tag) for q, tag in variants if len(q.split()) >= MIN_QUOTE_WORDS]
    if any(q in _norm(row[0], tag) for q, tag in variants):
        return True, ""
    for (nid, text) in corpus.db.execute("SELECT node_id, text FROM nodes WHERE doc_id=?", (doc_id,)):
        if any(q in _norm(text, tag) for q, tag in variants):
            return False, f"quote found in node {nid}, not {node_id}"
    return False, "quote not found in the paper"


# --- the model calls -----------------------------------------------------------------------

def _claims_block(items: List[Dict]) -> str:
    return "\n\n".join(
        f"claim_id: {o['claim_id']}\ncitation key: {o['key']} (the cited paper)\n"
        f"section of the draft: {o['section']}\n"
        f"paragraph (the citation being checked is inside ⟦ ⟧):\n{o['paragraph']}" for o in items)


def _full_prompt(doc_id: str, paper: str, items: List[Dict], want_summary: bool) -> str:
    task = ("You are checking the citations in a draft astronomy paper against the full text "
            "of one cited paper. You read the whole paper. For each citation listed, decide what "
            "the draft attributes to this paper at that citation, and whether the paper supports it.")
    out = '{"results": [' + RESULT_SCHEMA + ', ...]' + (", " + SUMMARY_SCHEMA if want_summary else "") + "}"
    extra = ("\n\nAlso write a detailed summary of the paper for later checks of other "
             "citations: specific, with numbers, every point tied to the node_ids that state "
             "it. Be thorough in not_done, limitations and status: a later reader will rely "
             "on them to catch claims the paper does not make." if want_summary else "")
    return (f"{task}\n\n{RULES}{extra}\n\nReturn JSON only: {out}\nwith one result for every "
            f"claim_id given.\n\n=== THE CITED PAPER ({doc_id}) ===\n{paper}\n=== END OF PAPER ===\n\n"
            f"=== CITATIONS TO CHECK ===\n{_claims_block(items)}")


def _route_prompt(doc_id: str, summary: Dict, sections: List[str], items: List[Dict]) -> str:
    return (
        "You are routing a citation check. Below is a detailed summary of one cited paper (every "
        "point lists the node_ids that state it) and the list of its section headings. For each "
        "citation, pick the passages a checker must read: those that could SUPPORT the claim the "
        "draft attributes to this paper, and those that could QUALIFY OR CONTRADICT it (different "
        "code or data, something the paper does not do, a limitation, something only planned, a "
        "different number). Prefer whole sections when unsure.\n\n"
        'Return JSON only: {"routes": [{"claim_id": "...", "support_nodes": [...], '
        '"contrary_nodes": [...], "sections": ["<heading exactly as listed>", ...]}]}\n\n'
        f"=== SUMMARY OF {doc_id} ===\n{json.dumps(summary, ensure_ascii=False, indent=0)}\n\n"
        f"=== SECTION HEADINGS ===\n" + "\n".join(sections) + "\n\n"
        f"=== CITATIONS ===\n{_claims_block(items)}")


def _routed_prompt(doc_id: str, excerpt: str, items: List[Dict]) -> str:
    task = ("You are checking the citations in a draft astronomy paper against EXCERPTS of one "
            "cited paper: the passages most likely to support or qualify each claim, plus every "
            "passage on its scope limits, things not done, limitations and status. If the "
            "excerpts are not enough to decide, give the verdict \"needs_full_read\" (the whole "
            "paper will then be read).")
    return (f"{task}\n\n{RULES}\n\nReturn JSON only: {{\"results\": [{RESULT_SCHEMA}, ...]}} with one "
            f"result for every claim_id given.\n\n=== EXCERPTS OF THE CITED PAPER ({doc_id}) ===\n"
            f"{excerpt}\n=== END OF EXCERPTS ===\n\n=== CITATIONS TO CHECK ===\n{_claims_block(items)}")


_GUARD_FIELDS = ("not_done", "limitations", "status", "data_and_codes")


def _guard_nodes(summary: Dict) -> set:
    out = set()
    for f in _GUARD_FIELDS:
        for p in summary.get(f) or []:
            if isinstance(p, dict):
                out.update(p.get("node_ids") or [])
    return out


# --- the check -----------------------------------------------------------------------------

def _cache_key(o: Dict, doc_id: str, model: str) -> str:
    return hashlib.sha1(json.dumps([PROMPT_VERSION, o["prev_sentence"], o["sentence"], o["key"],
                                    doc_id, model]).encode()).hexdigest()


class Stats:
    def __init__(self):
        self.calls = {"full": 0, "route": 0, "routed": 0}
        self.prompt_chars = {"full": 0, "route": 0, "routed": 0}
        self.seconds = 0.0


def run(tex: str, bib_text: str, corpus_root: str, model: str, cache_path: str,
        llm_call: Callable[[str], Optional[Dict]], mode: str = "auto", keys: Optional[List[str]] = None,
        extra_claims: Optional[List[Dict]] = None, sentence_filter: Optional[str] = None,
        use_cache: bool = True, log=print, stats: Optional[Stats] = None,
        author_checked: Optional[Dict[str, str]] = None) -> List[Dict]:
    """One row per (citation occurrence, key). mode: full, routed (needs summaries), auto."""
    stats = stats or Stats()
    t0 = time.time()
    occs = cite_occurrences(tex) + list(extra_claims or [])
    if keys:
        occs = [o for o in occs if o["key"] in keys]
    if sentence_filter:
        probe = _probe_text(sentence_filter)
        occs = [o for o in occs if probe in _probe_text(o["sentence"])]
    bibcodes = bib_bibcodes(bib_text)
    corpus = Corpus(corpus_root)
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    lookup = cache if use_cache else {}

    by_doc: Dict[str, List[Dict]] = {}
    rows = []
    for o in occs:
        bib = o.get("bibcode") or bibcodes.get(o["key"])
        doc_id = bib.replace("&", "+") if bib else None
        o = {**o, "bibcode": bib, "doc_id": doc_id}
        if (not doc_id or not corpus.has(doc_id)) and o["key"] in (author_checked or {}):
            rows.append({**o, "verdict": "author_checked", "read": "", "attributed_claim": "",
                         "explanation": "No full text; the authors take responsibility: "
                         + author_checked[o["key"]], "evidence": []})
            continue
        if not doc_id or not corpus.has(doc_id):
            rows.append({**o, "verdict": "no_fulltext", "read": "", "attributed_claim": "",
                         "explanation": "No full text in the reference corpus (not an ADS paper, "
                         "or not downloaded).", "evidence": []})
            continue
        ck = _cache_key(o, doc_id, model)
        if ck in lookup:
            res = _reverify(corpus, doc_id, lookup[ck])
            if res is not None:
                cache[ck] = res
                rows.append({**o, **res, "cached": True})
                continue
        by_doc.setdefault(doc_id, []).append(o)

    def full_read(doc_id, items, read_label):
        want_summary = corpus.summary(doc_id) is None
        prompt = _full_prompt(doc_id, corpus.paper_text(doc_id), items, want_summary)
        stats.calls["full"] += 1
        stats.prompt_chars["full"] += len(prompt)
        parsed = llm_call(prompt) or {}
        if want_summary and isinstance(parsed.get("summary"), dict):
            corpus.save_summary(doc_id, parsed["summary"], model)
        return {r.get("claim_id"): r for r in (parsed.get("results") or []) if isinstance(r, dict)}

    for doc_id, items in by_doc.items():
        summary = (corpus.summary(doc_id) or {}).get("summary")
        results: Dict[str, Tuple[Dict, str]] = {}
        if mode in ("routed", "auto") and summary:
            log(f"   routed read of {doc_id} for {len(items)} citation(s)")
            sections = list(dict.fromkeys(n[2] for n in corpus.nodes(doc_id)))
            rp = _route_prompt(doc_id, summary, sections, items)
            stats.calls["route"] += 1
            stats.prompt_chars["route"] += len(rp)
            routes = {r.get("claim_id"): r for r in ((llm_call(rp) or {}).get("routes") or [])
                      if isinstance(r, dict)}
            picks, secs = set(_guard_nodes(summary)), set()
            for r in routes.values():
                picks.update(r.get("support_nodes") or [])
                picks.update(r.get("contrary_nodes") or [])
                secs.update(r.get("sections") or [])
            keep = corpus.expand(doc_id, picks, secs)
            jp = _routed_prompt(doc_id, corpus.paper_text(doc_id, only=keep), items)
            stats.calls["routed"] += 1
            stats.prompt_chars["routed"] += len(jp)
            got = {r.get("claim_id"): r for r in ((llm_call(jp) or {}).get("results") or [])
                   if isinstance(r, dict)}
            for o in items:
                r = got.get(o["claim_id"])
                if r is not None:
                    res = _verify(corpus, doc_id, r)
                    if res["verdict"] in GOOD:
                        results[o["claim_id"]] = (res, "routed")
            pending = [o for o in items if o["claim_id"] not in results]
            if pending:
                log(f"   escalating {len(pending)} citation(s) of {doc_id} to a full read")
                got = full_read(doc_id, pending, "routed→full")
                for o in pending:
                    if o["claim_id"] in got:
                        results[o["claim_id"]] = (_verify(corpus, doc_id, got[o["claim_id"]]), "routed→full")
        else:
            log(f"   full read of {doc_id} for {len(items)} citation(s)")
            got = full_read(doc_id, items, "full")
            for o in items:
                if o["claim_id"] in got:
                    results[o["claim_id"]] = (_verify(corpus, doc_id, got[o["claim_id"]]), "full")
        for o in items:
            if o["claim_id"] in results:
                res, how = results[o["claim_id"]]
                res = {**res, "read": how}
                cache[_cache_key(o, doc_id, model)] = res
            else:
                res = {"verdict": "model_gave_no_verdict", "read": "", "attributed_claim": "",
                       "explanation": "The model returned no verdict for this citation; rerun "
                       "or check by hand.", "evidence": []}
            rows.append({**o, **res})
        with open(cache_path, "w") as f:
            json.dump(cache, f, indent=1, ensure_ascii=False)

    stats.seconds += time.time() - t0
    assert len(rows) == len(occs), "every citation must get a row"
    return rows


def _reverify(corpus: Corpus, doc_id: str, res: Dict) -> Optional[Dict]:
    """Re-check a cached verdict's quotes against the current index. A quote that moved to
    another node (the paper was re-converted) gets the new node_id; if a verified quote can
    no longer be found, return None so the citation is read again."""
    evidence = []
    for e in res.get("evidence") or []:
        ok, why = check_quote(corpus, doc_id, e["node_id"], e.get("quote", ""))
        m = re.match(r"quote found in node (\S+), not", why)
        if not ok and m:
            e = {**e, "node_id": m.group(1)}
            ok, why = True, ""
        if e.get("quote_ok") and not ok:
            return None
        evidence.append({**e, "quote_ok": ok, "problem": why})
    out = {**res, "evidence": evidence}
    mv = res.get("model_verdict", res.get("verdict"))
    if mv in ("supported", "partially_supported", "names_only"):  # same rule as _verify
        out["verdict"] = mv if evidence and all(e["quote_ok"] for e in evidence) else "quote_not_found"
    return out


def _verify(corpus: Corpus, doc_id: str, r: Dict) -> Dict:
    verdict = r.get("verdict") if r.get("verdict") in VERDICTS else "needs_full_read"
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


# --- fix proposals -------------------------------------------------------------------------

FIX_PROMPT = """A citation in a draft astronomy paper is not fully supported by the cited paper. \
Propose the smallest edit to the draft that makes the citation accurate. You may only:
  "narrow": weaken or narrow the sentence so it claims no more than the evidence shows;
  "move_citation": move the citation to the part of the sentence the paper does support;
  "drop_citation": remove this citation from the sentence (if other citations or the \
draft's own results carry the claim).
Never add a statement about the cited paper that is not in the verified quotes below. Keep \
the author's style and LaTeX. "old" must be copied exactly from the paragraph and must be \
long enough to occur only once (usually the whole sentence or clause); "new" replaces it.

Return JSON only: {"kind": "narrow|move_citation|drop_citation", "old": "...", "new": "...", \
"rationale": "one sentence"}"""



def _probe_text(s: str) -> str:
    """Text for locating an edited sentence: citations become a \\x00 separator, whitespace collapsed."""
    s = s.replace("⟦", "").replace("⟧", "")
    s = re.sub(r"\\cite\w*(?:\[(?:[^\[\]{}]|\{[^{}]*\})*\])*\{[^}]*\}", "\x00", s)
    return re.sub(r"\s+", " ", s).replace(" \x00", "\x00").replace("\x00 ", "\x00").strip()


def propose_fixes(rows: List[Dict], tex: str, llm_call, recheck: Callable[[str, str], List[Dict]],
                  log=print) -> List[Dict]:
    """For each flagged row: a proposed edit, re-checked with a full read of the new sentence."""
    fixes = []
    for r in rows:
        if r["verdict"] not in ("not_supported", "partially_supported", "quote_not_found"):
            continue
        quotes = "\n".join(f'- [{e["node_id"]}] "{e["quote"]}"' for e in r["evidence"] if e["quote_ok"]) or "(none)"
        prompt = (f"{FIX_PROMPT}\n\nCitation key: {r['key']}\nVerdict: {r['verdict']}\n"
                  f"What the draft attributes: {r['attributed_claim']}\nWhy: {r['explanation']}\n"
                  f"Verified quotes from the cited paper:\n{quotes}\n\n"
                  f"Paragraph (the citation is inside ⟦ ⟧):\n{r['paragraph']}")
        p = llm_call(prompt) or {}
        fid = hashlib.sha1(f"{r['claim_id']}|{p.get('old')}".encode()).hexdigest()[:8]
        fix = {"id": fid, "line": r["line"], "key": r["key"], "verdict": r["verdict"],
               "kind": p.get("kind"), "old": p.get("old") or "", "new": p.get("new") or "",
               "rationale": p.get("rationale", ""), "status": "", "recheck": []}
        if fix["kind"] not in ("narrow", "move_citation", "drop_citation") or not fix["old"]:
            fix["status"] = "no usable proposal"
        elif tex.count(fix["old"]) != 1:
            fix["status"] = f"'old' occurs {tex.count(fix['old'])} times in the draft"
        else:
            new_tex = tex.replace(fix["old"], fix["new"])
            probe = max(_probe_text(fix["new"]).split("\x00"), key=len)[:60]
            log(f"   re-checking proposed fix {fid} ({r['key']}, line {r['line']})")
            checked = recheck(new_tex, probe)
            fix["recheck"] = [{"key": c["key"], "verdict": c["verdict"], "explanation": c["explanation"]}
                              for c in checked]
            fix["status"] = ("passes" if checked and all(c["verdict"] in GOOD or c["verdict"] == "no_fulltext"
                                                          for c in checked)
                             else "does not pass the re-check")
        fixes.append(fix)
    return fixes


def apply_fixes(tex: str, fixes: List[Dict], ids: List[str]) -> Tuple[str, List[str]]:
    done = []
    for f in fixes:
        if f["id"] in ids:
            if tex.count(f["old"]) != 1:
                raise ValueError(f"fix {f['id']}: 'old' no longer occurs exactly once")
            tex = tex.replace(f["old"], f["new"])
            done.append(f["id"])
    missing = set(ids) - set(done)
    if missing:
        raise ValueError(f"unknown fix ids: {sorted(missing)}")
    return tex, done


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
        answer = (f"**{r['verdict'].replace('_', ' ')}** (model said: {r.get('model_verdict', r['verdict'])}; "
                  f"read: {r.get('read') or '-'})\n\n"
                  f"**Draft, line {r['line']}:** {r['attributed_claim']}\n\n{r['explanation']}\n\n"
                  + "\n\n".join(marks))
        bad = [e for e in r["evidence"] if not e["quote_ok"]]
        if bad:
            answer += "\n\nQuotes that could not be verified: " + "; ".join(
                f"node {e['node_id']}: {e['problem']}" for e in bad)
        res = {"question": f"{r['key']} at line {r['line']}", "answer": answer, "citations": cites}
        out.setdefault(r["doc_id"], {"results": []})["results"].append(res)
    return out


def summary_payload(corpus: Corpus, doc_id: str) -> Optional[Dict]:
    s = corpus.summary(doc_id)
    if not s:
        return None
    cites, parts = [], []
    for field, points in s["summary"].items():
        if not isinstance(points, list) or not points or field == "section_map":
            continue
        parts.append(f"**{field.replace('_', ' ')}**")
        for p in points:
            if not isinstance(p, dict):
                continue
            marks = []
            for nid in p.get("node_ids") or []:
                cites.append({"id": len(cites) + 1, "doc_id": doc_id, "node_id": nid})
                marks.append(f"[{len(cites)}]")
            parts.append(f"- {p.get('text', '')} {' '.join(marks)}")
    return {"results": [{"question": f"Summary of {doc_id} (for citation checks)",
                         "answer": "\n".join(parts), "citations": cites}]}


def publish(payloads: Dict[str, Dict], corpus_root: str, papertrail_python: str, helper: str,
            tmp_path: str, title_prefix: str = "Citation check") -> Dict[str, Dict]:
    with open(tmp_path, "w") as f:
        json.dump(payloads, f, ensure_ascii=False)
    r = subprocess.run([papertrail_python, "-I", helper, corpus_root, tmp_path, title_prefix],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"papertrail publish failed: {r.stderr[-2000:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


ORDER = ["not_supported", "quote_not_found", "model_gave_no_verdict", "needs_full_read",
         "partially_supported", "no_fulltext", "author_checked", "supported", "names_only"]


def draft_sections(tex: str) -> List[Tuple[int, str]]:
    """[(line, 'Section > Subsection')] for each sectioning command in the draft, in order."""
    out, path = [], []
    levels = {"section": 0, "subsection": 1, "subsubsection": 2}
    for i, line in enumerate(tex.splitlines(), 1):
        for m in re.finditer(r"\\(section|subsection|subsubsection)\*?\{((?:[^{}]|\{[^{}]*\})*)\}", line):
            lvl = levels[m.group(1)]
            path = path[:lvl] + [m.group(2)]
            out.append((i, " > ".join(path)))
    return out


def annotate_evidence(rows: List[Dict], corpus: Corpus, tex: str) -> None:
    """Add the draft section to each row and the cited paper's section/page to each evidence quote."""
    secs = draft_sections(tex)
    meta_cache: Dict[str, Dict] = {}
    for r in rows:
        line = int(r["line"]) if str(r.get("line", "")).isdigit() else 0
        r["draft_section"] = next((s for l, s in reversed(secs) if l <= line), "")
        doc = r.get("doc_id")
        if not doc:
            continue
        if doc not in meta_cache:
            meta_cache[doc] = corpus.node_meta(doc)
        for e in r.get("evidence", []):
            m = meta_cache[doc].get(e.get("node_id"), {})
            e["section"], e["block_type"], e["page"] = m.get("section", ""), m.get("block_type", ""), m.get("page")


def _evidence_md(rows: List[Dict], published: Dict[str, Dict], base_url: str) -> List[str]:
    L = ["", "## Supporting evidence", "",
         "For each citation: the draft sentence, then each quote from the cited paper with the section, "
         "paragraph id (papertrail node) and page it comes from. Quotes marked ✗ were not found in the "
         "paper's text. Grouped by draft section, in draft order.", ""]
    last_sec = None
    for r in sorted(rows, key=lambda r: int(r["line"]) if str(r["line"]).isdigit() else 0):  # stable: draft order
        if r["verdict"] in ("author_checked", "no_fulltext"):
            continue
        sec = r.get("draft_section") or "(before first section)"
        if sec != last_sec:
            L += [f"### {sec}", ""]
            last_sec = sec
        pub = published.get(r.get("doc_id") or "", {})
        link = f" · [papertrail]({base_url}/#answer={pub['id']})" if pub.get("id") else ""
        sent = re.sub(r"\\(?:sub)*section\*?\{(?:[^{}]|\{[^{}]*\})*\}|\\label\{[^}]*\}", "", r.get("sentence", ""))
        sent = re.sub(r"\s+", " ", sent).strip().replace("⟦", "**").replace("⟧", "**")
        L += [f"**{r['key']}** — line {r['line']} — *{r['verdict']}*{link}", "",
              f"> {sent}", ""]
        ev = r.get("evidence") or []
        if not ev:
            L += ["- (no quote given)", ""]
        for e in ev:
            where = ", ".join(x for x in (f"§ {e['section']}" if e.get("section") else "",
                                          f"node `{e.get('node_id')}`",
                                          e.get("block_type") if e.get("block_type") not in (None, "", "paragraph") else "",
                                          f"p. {e['page']}" if e.get("page") else "") if x)
            mark = "" if e.get("quote_ok") else f" ✗ ({e.get('problem') or 'not found'})"
            L.append(f"- {where}{mark}: “{e.get('quote', '')}”")
        L.append("")
    return L


def write_report(rows: List[Dict], published: Dict[str, Dict], tex_path: str, out_md: str,
                 out_json: str, model: str, base_url: str, mode: str, stats: Stats,
                 fixes: Optional[List[Dict]] = None):
    with open(out_json, "w") as f:
        json.dump({"tex": tex_path, "model": model, "prompt_version": PROMPT_VERSION, "mode": mode,
                   "stats": {"calls": stats.calls, "prompt_chars": stats.prompt_chars,
                             "seconds": round(stats.seconds)},
                   "published": published, "rows": rows, "fixes": fixes or []},
                  f, indent=1, ensure_ascii=False)
    counts = {v: sum(r["verdict"] == v for r in rows) for v in ORDER}
    reads = {k: sum(r.get("read") == k for r in rows) for k in ("full", "routed", "routed→full")}
    cell = lambda s: (s or "").replace("|", "\\|").replace("\n", " ")
    L = [f"# Citation check: {os.path.basename(tex_path)}", "",
         f"Model: {model}. Mode: {mode}. Reads this run: "
         + ", ".join(f"{k} {v}" for k, v in reads.items() if v)
         + f" (cached: {sum(bool(r.get('cached')) for r in rows)}). Quotes were checked against "
         "the paper's text by script. Open a paper's link to see each verdict with clickable "
         "quotes in the papertrail reader (`papertrail serve --corpus <corpus>`). "
         "Routed reads are for re-checks; run a full read (`--mode full --fresh`) before submission.",
         "", "| verdict | count |", "|---|---|"] + [f"| {v} | {c} |" for v, c in counts.items() if c]
    if fixes:
        L += ["", "## Proposed fixes", "",
              "Apply with `run_steps.py citefix --workdir <dir> --apply <id> ...`. Each proposal was "
              "re-checked with a full read of the cited paper.", "",
              "| id | line | key | kind | status | old → new |", "|---|---|---|---|---|---|"]
        for f in fixes:
            L.append(f"| `{f['id']}` | {f['line']} | {f['key']} | {f['kind']} | {f['status']} | "
                     f"{cell(f['old'])} → **{cell(f['new'])}** |")
    L += ["", "## All citations", "",
          "| line | key | verdict | read | what the draft attributes | explanation | open |",
          "|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: (ORDER.index(r["verdict"]) if r["verdict"] in ORDER else 0, r["line"])):
        pub = published.get(r.get("doc_id") or "", {})
        link = f"[answer]({base_url}/#answer={pub['id']})" if pub.get("id") else ""
        L.append(f"| {r['line']} | {r['key']} | **{r['verdict']}** | {r.get('read', '')} | "
                 f"{cell(r['attributed_claim'])} | {cell(r['explanation'])} | {link} |")
    L += _evidence_md(rows, published, base_url)
    with open(out_md, "w") as f:
        f.write("\n".join(L) + "\n")
    return counts
