# Citation Extractor Revision: Stable IDs and Reliable Coupling

## Why this revision was needed

The first citation extractor produced weak cited-source labels such as:

```text
commission 2020
union 2021
```

That is not sufficient for a high-stakes citation backtracking system. If the actual cited source is later uploaded as `European Commission. (2020a). Analysis of links between CAP Reform and Green Deal`, a vague key like `commission 2020` is too lossy and can cause duplicate or failed matches.

The citation extractor now distinguishes:

```text
reference instance inside a citing paper
canonical cited-source identity
local matched source_id, if the cited source is in the library
```

---

## New identity rules

## 1. DOI-backed references

If a reference has a DOI, its canonical source identity is DOI-derived:

```text
doi_10_1016_j_baae_2014_10_001
doi_10_1111_1365_2664_13165
```

The raw DOI is still stored in the `doi` field.

This means the same publication cited by many papers maps to the same canonical identity.

## 2. Non-DOI references

If there is no DOI, the canonical identity is deterministic:

```text
ref_{author_key}_{first_title_word}_{year}
```

Examples:

```text
ref_european_commission_analysis_2020a
ref_council_of_the_european_union_proposal_2021
ref_scown_billions_2020
ref_pe_er_how_2022
```

This is predictable, stable and much better than short keys like `commission 2020`.

## 3. Reference instances

A reference inside a specific citing source still receives an instance ID:

```text
REF-CuadrosCasanova_2022-ref-011
```

This preserves the exact reference-list anchor and supports in-text citation coupling.

---

## Improved coupling strategy

The extractor now couples in-text citations to reference-list entries in this order:

1. **Markdown anchor coupling**

   Parsed markdown often has citations like:

   ```markdown
   [Pe'er et al., 2019](#ref-041)
   ```

   This is the strongest coupling. The extractor stores:

   ```text
   reference_anchor = ref-041
   reference_id     = REF-source-ref-041
   canonical_id     = canonical ID from the reference-list entry
   ```

2. **Author + full year token**

   Handles suffixes such as:

   ```text
   European Commission, 2020a
   European Commission, 2020b
   ```

3. **Author + base year fallback**

   Used only when the citation omits suffix but the reference list has one.

4. **DOI / canonical source matching to local library**

   If an uploaded local source has the same DOI, it can be matched even when the local `source_id` is human-readable rather than DOI-derived.

---

## Schema additions

`source_references` now includes:

```text
reference_anchor
canonical_source_id
```

`citation_contexts` now includes:

```text
reference_anchor
canonical_source_id
```

This separates:

```text
reference_id          = instance in a citing paper
canonical_source_id   = stable identity of cited source
matched_source_id     = actual local source if uploaded
```

---

## New / improved commands

List parsed references with stable IDs:

```bash
python rh2.py reference-report --source-id CuadrosCasanova_2022 --limit 20
```

Extract citations:

```bash
python rh2.py extract-citations CuadrosCasanova_2022 --clear
```

Summarize citation contexts:

```bash
python rh2.py citation-summary --source-id CuadrosCasanova_2022
```

Inspect a context:

```bash
python rh2.py citation-context CITCTX-CuadrosCasanova_2022-00003
```

---

## Current extraction quality after revision

Current database totals:

```text
sources:             3
source_references:   229
citation_contexts:   542
```

Per source:

```text
CuadrosCasanova_2022:
  references:         47
  citation contexts:  51
  linked to ref list: 50/51

BadenBohm_2023:
  references:         71
  citation contexts:  94
  linked to ref list: 93/94

Canessa_2024:
  references:         111
  citation contexts:  397
  linked to ref list: 389/397
```

The remaining unlinked contexts are mostly messy cases such as:

- acronyms not present in the reference key (`EEA, 2019`),
- prose-like parentheses,
- malformed OCR references,
- narrative citations where the parsed paper text dropped the first author,
- citations where the reference list itself is noisy.

---

## Known hard cases

## Acronyms

Example:

```text
(EEA, 2019)
```

May refer to `European Environment Agency`, but if the reference list does not include `EEA`, this requires alias resolution.

Future fix:

```text
reference_aliases table
```

## Multi-author narrative citations

Example:

```text
Knowler and Bradshaw (2007)
```

The extractor now prefers the first author for these, but malformed text can still cause issues.

## OCR-corrupted references

Example from Baden-Böhm:

```text
Baden-BBiodivers. Data J. 10, e83523 ohm...
```

The DOI still gives a stable canonical ID if recoverable, but author/title may be noisy.

## DOI line-break damage

The extractor now repairs common broken DOI patterns like:

```text
10.1016/j. biocon.2010.05.005
10.1186/ s12898-018-0210-z
```

---

## Remaining needed upgrades

1. `reference_aliases` table for acronyms and institutional abbreviations.
2. DOI/title lookup via Crossref/OpenAlex.
3. Better parsing of unstructured reference lists with GROBID/Anystyle.
4. Citation-context to claim linking.
5. UI for citation verification.
6. Missing-source import queue.
7. Automatic rerun of citation matching after new source ingestion.

---

## Bottom line

The extractor is now much closer to a high-stakes citation-backtracking foundation:

```text
in-text citation → exact reference-list entry → stable canonical source ID → local matched source if available
```

It is not perfect, but the previous lossy `commission 2020` style is replaced by predictable canonical IDs such as:

```text
ref_european_commission_analysis_2020a
ref_scown_billions_2020
doi_10_1016_j_baae_2014_10_001
```

This substantially reduces duplication risk and makes later source matching much more realistic.
