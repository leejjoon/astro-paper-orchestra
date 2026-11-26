# AAS Journals (AASTeX v7) Paper Writing Guidelines

These instructions apply to a manuscript for an American Astronomical Society journal
(The Astrophysical Journal, The Astronomical Journal, ApJ Supplement, ApJ Letters, or the
Planetary Science Journal), written with the AASTeX v7.0.1 class (`aastex701.cls`).

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
* **Document class:** `\documentclass{aastex701}` (single column manuscript). Do not change
  the class options and do not add `twocolumn`, `linenumbers` or `anonymous`.
* **Packages:** The class already loads `natbib`, `graphicx`, `amsmath`, `hyperref` and the
  AAS journal-name macros (`\apj`, `\aj`, `\mnras`, `\aap`, `\pasp`, `\apjs`, `\apjl`, ...).
  Do not load these again. Do not add packages that are not installed in a standard TeX Live
  distribution.
* **Style files:** `aastex701.cls` and `aasjournalv7.bst` are in the working directory.

## Front Matter
* **Title:** Concise and informative. Capitalize as a title.
* **Authors:** The author block (`\author`, `\affiliation`, `\email`) between the
  `AUTHORS - KEEP AS PROVIDED` markers is filled in by the human authors. Never change,
  invent, or remove authors, affiliations, or e-mail addresses.
* **Abstract:** One paragraph, at most 250 words, no citations, no undefined acronyms.
  Summarize the context, what was done, the key quantitative results, and their implication.
* **Keywords:** Unified Astronomy Thesaurus (UAT) keywords are chosen by the human authors
  between the `KEYWORDS - KEEP AS PROVIDED` markers (format:
  `\keywords{\uat{Galaxies}{573} --- \uat{Cosmology}{343}}`). Do not change them or invent
  UAT IDs.

## Sections
* Standard order: Introduction, a review of previous work, Data (observations and reduction),
  Method, Results, Discussion, Summary. You may rename sections (e.g. "Related Work" can be
  titled "Previous Work" or "Existing Telluric Correction Methods"; "Data" can be
  "Observations and Data Reduction") or merge/split them when it serves the paper, but keep
  the existing `\label`s so cross references remain valid.
* Use `\section`, `\subsection`, `\subsubsection`. Do not create a single orphan subsection.
* The final section is a Summary (or Summary and Conclusions), often as a short numbered list
  of the main findings.
* **Page length:** AAS journals have no page limit, but be concise. A typical article is
  8-20 pages in this manuscript format.

## Citations and References
* Use natbib commands: `\citet{key}` for textual citations ("Smette et al. (2015) showed")
  and `\citep{key}` for parenthetical ones ("(Smette et al. 2015)"). Use
  `\citep[e.g.,][]{key1, key2}` for examples.
* Only cite keys that exist in `references.bib` / the provided citation map. Never invent
  citation keys or references.
* References use `\bibliography{references}{}` with `\bibliographystyle{aasjournalv7}`;
  do not change these lines.
* Cite the software you used in a `\software{}` line (e.g. `\software{Astropy
  \citep{key}, NumPy, SciPy}`) and the telescopes/instruments in a `\facilities{}` line, when
  this information is present in the raw materials. Leave them commented out otherwise.

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
* Use `\begin{figure}` (single column) with `\centering`; use `\begin{figure*}` only for very
  wide figures. Caption goes **below** the figure, label after the caption.
* Every figure and table must be referred to in the text (`Figure~\ref{fig:x}`,
  `Table~\ref{tab:x}`) before or near where it appears.
* Captions are complete sentences that explain what is shown, including the meaning of
  colors, symbols, line styles and units, so the figure can be understood on its own.
* Tables: AASTeX `deluxetable` is preferred, but a standard `table` with `tabular` is
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

## Important Rules
* Do not modify the document class, the author block, the keywords, the acknowledgments block,
  or the bibliography lines.
* Do not use `\cite` keys, figures, tables, or numbers that are not supported by the provided
  materials.
* Ensure the LaTeX compiles with `pdflatex` + `bibtex`: every `\begin{...}` has a matching
  `\end{...}`, special characters (`%`, `&`, `_`, `#`) are escaped in text.
