# The Open Journal of Astrophysics (openjournal.cls) Paper Writing Guidelines

These instructions apply to a manuscript for The Open Journal of Astrophysics (OJAp), an
arXiv overlay journal, written with its LaTeX class `openjournal.cls` (based on emulateapj,
built on REVTeX 4.1).

## Audience and Tone
* The readers are professional astronomers. Write in the register of a refereed astronomy
  journal article: precise, quantitative, and measured. Avoid marketing language
  ("groundbreaking", "revolutionary", "novel paradigm") and machine-learning conference
  idioms ("we beat SOTA", "baselines", "benchmarks", "ablations") unless they are literally
  what was done.
* State the physical motivation of the work, not only the technical one.
* Every quantitative statement (numbers, uncertainties, sample sizes, wavelengths,
  resolutions, signal-to-noise ratios, dates) MUST come from `experimental_log.md` or
  `idea.md`. Never invent or round values differently than given. Quote uncertainties when
  they are provided.
* Do not overclaim. If a result is shown only for one instrument, band, target class, or
  data set, say so explicitly.

## General Setup
* **Document class:** `\documentclass[onecolumn]{openjournal}`. Do not change the class
  options.
* **Packages:** The class loads `natbib` (author-year), `graphicx`, `amssymb` and `longtable`
  and provides `deluxetable`. The template preamble adds `amsmath`, `hyperref` and
  `aasjournalmacros` (the journal-name macros `\apj`, `\aj`, `\mnras`, `\aap`, `\pasp`,
  `\jqsrt`, ... that the bibliography uses). Do not load these again and do not change the
  preamble. Do not add packages that are not installed in a standard TeX Live distribution.
* **Style files:** `openjournal.cls`, `aasjournalmacros.sty` and `aasjournal.bst` are in the
  working directory.

## Front Matter
* **Title:** Concise and informative. Capitalize as a title.
* **Authors:** The author block (`\author`, `\email`, `\affiliation`) between the
  `AUTHORS - KEEP AS PROVIDED` markers is filled in by the human authors. Never change,
  invent, or remove authors, affiliations, or e-mail addresses.
* **Abstract:** One paragraph, at most 250 words, no citations, no undefined acronyms.
  Summarize the context, what was done, the key quantitative results, and their implication.
* **Keywords:** Chosen by the human authors between the `KEYWORDS - KEEP AS PROVIDED`
  markers, as plain text separated by ` --- `. Do not change them.
* **Front-matter order:** `\title`, author block, `abstract` environment, `\keywords`,
  then `\maketitle`. Keep this order (REVTeX typesets the front matter at `\maketitle`).

## Sections
* Standard order: Introduction, a review of previous work, Data (observations and reduction),
  Method, Results, Discussion, Summary. You may rename sections (e.g. "Related Work" can be
  titled "Previous Work" or "Existing Telluric Correction Methods"; "Data" can be
  "Observations and Data Reduction") or merge/split them when it serves the paper, but keep
  the existing `\label`s so cross references remain valid.
* Use `\section`, `\subsection`, `\subsubsection`. Do not create a single orphan subsection.
* The final section is a Summary (or Summary and Conclusions), often as a short numbered list
  of the main findings.
* **Length:** No page limit is assumed here; be concise.

## Citations and References
* Use natbib commands: `\citet{key}` for textual citations ("Smette et al. (2015) showed")
  and `\citep{key}` for parenthetical ones ("(Smette et al. 2015)"). Use
  `\citep[e.g.,][]{key1, key2}` for examples.
* Only cite keys that exist in `references.bib` / the provided citation map. Never invent
  citation keys or references.
* References use `\bibliographystyle{aasjournal}` and `\bibliography{references}`; do not
  change these lines.
* Cite the software you used in a `\software{}` line (e.g. `\software{Astropy
  \citep{key}, NumPy, SciPy}`) and the telescopes/instruments in a `\facilities{}` line, when
  this information is present in the raw materials. Leave them commented out otherwise.
  Both commands are defined in the template preamble and take one argument.
* The `Data Availability` section (unnumbered) states where the code and data are; use the
  materials' wording and placeholders, never invented DOIs or URLs.
* Acknowledgments follow the `\acknowledgments` command (a command, not an environment).

## Units, Numbers and Notation
* Write units in roman type with a thin space: `1.65\,$\mu$m`, `$R \sim 45\,000$`,
  `10\,km\,s$^{-1}$`. Use `\AA` for Angstrom. Use SI or conventional astronomical units
  (\AA, $\mu$m, km\,s$^{-1}$, mag, Jy, $M_\odot$, pc).
* Spectral bands and instrument names are roman (H band, K band, IGRINS, CRIRES+).
* Define each acronym at first use in the abstract and again at first use in the main text.
* Number equations only when referenced; use `\begin{equation}` with `\label`.

## Figures and Tables
* All figures reside in the `figures/` directory. Always include the `figures/` prefix in
  `\includegraphics` (e.g. `\includegraphics[width=\linewidth]{figures/figure_1.jpg}`).
* Use `\begin{figure}` with `\centering`. Caption goes **below** the figure, label after the caption.
* Every figure and table must be referred to in the text (`Figure~\ref{fig:x}`,
  `Table~\ref{tab:x}`) before or near where it appears.
* Captions are complete sentences that explain what is shown, including the meaning of
  colors, symbols, line styles and units, so the figure can be understood on its own.
* Tables: `deluxetable` (provided by the class) is preferred, but a standard `table` with `tabular` is
  acceptable. Caption goes **above** the table. Give units in column headers.

### Example figure
```latex
\begin{figure}
\centering
\includegraphics[width=\linewidth]{figures/figure_1.jpg}
\caption{Telluric transmission spectrum in the H band (black) and the best-fit model (red).
The lower panel shows the residuals.}
\label{fig:transmission}
\end{figure}
```

### Example table
```latex
\begin{deluxetable}{lcc}
\tablecaption{Summary of observations. \label{tab:obs}}
\tablehead{\colhead{Target} & \colhead{Date} & \colhead{Exposure (s)}}
\startdata
HD 12345 & 2024-01-02 & 600 \\
HD 67890 & 2024-01-03 & 900 \\
\enddata
\end{deluxetable}
```

## Journal policy (The Open Journal of Astrophysics, "For Authors")
* The journal accepts generative-AI assistance only if it is declared in the acknowledgments,
  and it rejects papers that contain hallucinated references or prompts in the text. So:
  - Never write text addressed to the reader about the writing process ("As requested",
    "Here is the revised section", "Note:", "TODO", "[placeholder]", "as an AI").
  - Cite only keys in the citation map; never describe a reference you have not been given.
  - The acknowledgments, including the AI-use statement, are written by the human authors
    and are KEEP AS PROVIDED.
* The journal does no copy-editing: the text must be final, proofread prose.

## Data and citation practice (Chen et al. 2022, recommended by the journal)
* **Times:** give dates of observations with their time system (e.g. UTC) and say whether a
  time is the start, middle or end of an exposure, when the materials say so.
* **Velocities:** state the reference frame of every velocity (e.g. observatory/telluric frame,
  barycentric) when the materials give it; never assume one.
* **Tables:** units in every column header, one kind of quantity per column, no flags appended
  to values; a number given in both a table and the text must be identical.
* **Figures:** captions must stand alone and explain every color, symbol and line style.
* **Provenance:** cite the original source of every data set, line list and atlas, with the
  version or edition given in the materials (e.g. AER line file 3.9, MT_CKD 4.3, LBLRTM 12.17).
  Use "this work" to distinguish new measurements from earlier ones.
* **Software:** list the software used with versions in `\software{}`, citing papers where
  they exist in the citation map.
* **Data availability:** data and code go to repositories that issue DOIs; never give personal
  or institutional web URLs as the place where data live. Use the placeholders in the
  materials for DOIs not yet minted.

## Important Rules
* Do not modify the document class, the preamble, the author block, the keywords, the
  acknowledgments block, or the bibliography lines.
* Do not use `\cite` keys, figures, tables, or numbers that are not supported by the provided
  materials.
* Ensure the LaTeX compiles with `pdflatex` + `bibtex`: every `\begin{...}` has a matching
  `\end{...}`, special characters (`%`, `&`, `_`, `#`) are escaped in text.
