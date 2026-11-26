"""List the numbers in a LaTeX draft that do not appear in the source materials.

This is a deterministic check, not an LLM review: every number in the .tex body is
compared, by value, with every number in the sources (idea, experimental log, figure
captions, and any extra files such as JSON reports). Numbers found nowhere are
reported with their line and context, for a human to check. Small integers (0-10),
and numbers inside \\ref, \\cite, \\label, \\includegraphics options and similar
commands are skipped.
"""

import json
import os
import re
from typing import Dict, Iterable, List, Set, Tuple

_SUPERSCRIPTS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺", "0123456789-+")
_NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?:[eE]([-+]?\d+))?(?![\d_])")


def _normalize_source(text: str) -> str:
    text = text.replace("\u2212", "-").replace("\u2013", "-").replace("\u2009", "").replace("\u202f", "")
    # 1.2 × 10⁻⁵ / 1.2×10^-5 -> 1.2e-5 ; 10⁸ -> 1e8
    # superscript exponents first: 2.4 × 10⁻⁸ -> 2.4e-8, 10⁸ -> 1e8
    sup = "⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺"
    text = re.sub(rf"(\d(?:\.\d+)?)\s*[×x]\s*10([{sup}]+)",
                  lambda m: m.group(1) + "e" + m.group(2).translate(_SUPERSCRIPTS), text)
    text = re.sub(rf"(?<![\d.])10([{sup}]+)", lambda m: "1e" + m.group(1).translate(_SUPERSCRIPTS), text)
    text = text.translate(_SUPERSCRIPTS)
    # caret forms need the caret: 1.2 × 10^-5, 10^{8}
    text = re.sub(r"(\d(?:\.\d+)?)\s*[×x]\s*10\^\{?([-+]?\d+)\}?", r"\1e\2", text)
    text = re.sub(r"(?<![\d.])10\^\{?([-+]?\d+)\}?", r"1e\1", text)
    return text


def _normalize_tex(text: str) -> str:
    text = re.sub(r"(?<!\\)%.*", "", text)  # comments
    text = text.replace("\u2212", "-").replace("\u2013", "-")
    # drop commands whose arguments are not prose numbers
    text = re.sub(r"\\uat\{[^}]*\}\{[^}]*\}", " ", text)
    text = re.sub(r"\\(ref|eqref|cite[tp]?|citealt|citeyear|label|url|href|bibliography\w*|uat|"
                  r"includegraphics|usepackage|documentclass|begin|end|hspace|vspace|setlength)"
                  r"\*?(\[[^\]]*\])*(\{[^}]*\})?", " ", text)
    text = re.sub(r"\[(width|height|scale|angle)=[^\]]*\]", " ", text)
    text = re.sub(r"(\d)\\,(\d{3})", r"\1\2", text)  # 45\,000
    text = text.replace("\\,", " ").replace("{,}", ",").replace("\\!", "").replace("~", " ")
    text = re.sub(r"\\(?:times|cdot)\s*10\^\{?([-+]?\d+)\}?", r"e\1", text)
    text = re.sub(r"(\d)\s*e(?=[-+]?\d)", r"\1e", text)
    text = re.sub(r"(?<![\d.e])10\^\{?([-+]?\d+)\}?", r"1e\1", text)
    text = text.replace("$", " ").replace("\\%", "%")
    text = re.sub(r"\\[a-zA-Z]+", " ", text)  # remaining commands (\sim, \mu, ...)
    return text


def _numbers(text: str) -> Iterable[Tuple[float, str, int]]:
    for m in _NUM.finditer(text):
        whole, frac, exp = m.group(1), m.group(2) or "", m.group(3)
        literal = whole.replace(",", "") + frac
        try:
            value = float(literal + (f"e{exp}" if exp else ""))
        except ValueError:
            continue
        yield value, m.group(0), m.start()


def source_values(texts: Iterable[str]) -> Set[float]:
    values: Set[float] = set()
    for t in texts:
        for v, _, _ in _numbers(_normalize_source(t)):
            values.add(round(v, 12))
    return values


def check(tex: str, sources: Dict[str, str]) -> List[Dict]:
    known = source_values(sources.values())
    body = tex.split("\\begin{document}", 1)[-1].split("\\bibliography", 1)[0]
    offset_lines = tex[: len(tex) - len(tex.split("\\begin{document}", 1)[-1])].count("\n")
    findings = []
    for lineno, line in enumerate(body.splitlines(), start=offset_lines + 1):
        norm = _normalize_tex(line)
        for value, literal, pos in _numbers(norm):
            if value.is_integer() and 0 <= value <= 10:
                continue
            if round(value, 12) in known:
                continue
            context = norm[max(0, pos - 60): pos + 40].strip()
            findings.append({"line": lineno, "number": literal, "context": re.sub(r"\s+", " ", context)})
    return findings


# Text a language model leaves behind (prompts, notes to the user, placeholders). The Open
# Journal of Astrophysics rejects papers with "prompts in the text".
_ARTIFACTS = [
    r"\bas an ai\b", r"\blanguage model\b", r"\bhere is\b", r"\bhere's\b", r"\bas requested\b",
    r"\bI have (?:added|revised|rewritten|updated)\b", r"\bthe user\b", r"\bTODO\b", r"\bTBD\b",
    r"\bXXX\b", r"\bplaceholder\b", r"\bto be added\b", r"\[insert", r"\bcitation needed\b", r"\?\?",
    r"\bexperimental[_ ]log\b", r"\bidea\.md\b", r"\bcitation[_ ]map\b", r"\boutline\.json\b",
    r"\.json\b", r"\.py\b", r"\bopen question\b", r"\bplanned (?:paragraph|subsection)s?\b",
]


def artifact_check(tex: str) -> List[Dict]:
    """Lines of the document body that look like LLM leftovers or repository references."""
    body = tex.split("\\begin{document}", 1)[-1]
    offset_lines = tex[: len(tex) - len(body)].count("\n")
    hits = []
    for lineno, line in enumerate(body.splitlines(), start=offset_lines + 1):
        text = re.sub(r"(?<!\\)%.*", "", line)
        for pat in _ARTIFACTS:
            m = re.search(pat, text, re.I)
            if m:
                hits.append({"line": lineno, "match": m.group(0),
                             "context": re.sub(r"\s+", " ", text[max(0, m.start() - 60): m.end() + 40]).strip()})
                break
    return hits


def figure_pdf_text(figures_dir: str) -> str:
    """Text inside the figure PDFs (axis labels, annotations), via pdftotext if installed.
    The writer sees the figures, so numbers it reads off them count as sourced."""
    import glob
    import shutil
    import subprocess
    if not shutil.which("pdftotext"):
        return ""
    out = []
    for path in sorted(glob.glob(os.path.join(figures_dir, "*.pdf"))):
        r = subprocess.run(["pdftotext", "-q", path, "-"], capture_output=True, text=True)
        out.append(r.stdout)
    return "\n".join(out)


def figure_captions(figures_info_path: str) -> str:
    try:
        with open(figures_info_path) as f:
            return "\n".join(x.get("caption", "") for x in json.load(f))
    except (OSError, ValueError):
        return ""


def write_report(findings: List[Dict], tex_path: str, sources: Dict[str, str], out_path: str,
                 artifacts: List[Dict] = ()):
    lines = [
        f"# Fact check: {tex_path}",
        "",
        "Numbers in the draft that do not appear (by value) in any source:",
        "",
        *[f"- {name}" for name in sources],
        "",
        f"**{len(findings)} unsupported numbers.** Each one is either a fabrication, a "
        "rounding or unit change, or a value derived from the sources (check it).",
        "",
        "| line | number | context |",
        "|---|---|---|",
    ]
    for f in findings:
        ctx = f["context"].replace("|", "\\|")
        lines.append(f"| {f['line']} | {f['number']} | {ctx} |")
    lines += ["", f"## Possible LLM leftovers or repository references: {len(artifacts)}", "",
              "| line | match | context |", "|---|---|---|"]
    for f in artifacts:
        ctx = f["context"].replace("|", "\\|")
        lines.append(f"| {f['line']} | {f['match']} | {ctx} |")
    with open(out_path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
