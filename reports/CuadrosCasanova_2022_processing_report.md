# Cuadros-Casanova et al. 2022 Processing Report

Source processed with the revised V2 thesis-writing pipeline.

## Source

- Source ID: `CuadrosCasanova_2022`
- Title: *Opportunities and challenges for Common Agricultural Policy reform to support the European Green Deal*
- Authors: Cuadros-Casanova; Cristiano; Biancolini; Cimatti; Sessa; Mendez Angarita; Dragonetti; Rondinini; Di Marco
- Year: 2022
- DOI: `10.1111/cobi.14052`
- Type: peer-reviewed article
- Disciplines: conservation biology; environmental policy; agricultural policy; sustainability science
- Geography: European Union
- Methodology: literature review; policy analysis

## Commands run

```bash
cd research_harness_v2

python rh2.py ingest '../uploads/paper (2).md' \
  --source-id CuadrosCasanova_2022 \
  --title "Opportunities and challenges for Common Agricultural Policy reform to support the European Green Deal" \
  --authors "Cuadros-Casanova; Cristiano; Biancolini; Cimatti; Sessa; Mendez Angarita; Dragonetti; Rondinini; Di Marco" \
  --year 2022 \
  --doi "10.1111/cobi.14052" \
  --source-type "peer-reviewed article" \
  --disciplines "conservation biology; environmental policy; agricultural policy; sustainability science" \
  --geography "European Union" \
  --methodology "literature review; policy analysis" \
  --theory "European Green Deal; Common Agricultural Policy; biodiversity strategy; farm to fork strategy" \
  --quality high

python rh2.py extract-source-ranges CuadrosCasanova_2022 \
  --min-score 2.4 \
  --limit 35 \
  --status candidate_needs_review

python rh2.py build-paper-index
python rh2.py extract-citations CuadrosCasanova_2022 --clear
python export_static_site.py
```

## Resulting database state for this source

```text
spans:              170
  chunk spans:       27
  evidence spans:    35
  page spans:        13
  paragraph spans:   95
claims:             35
claim tags:          94
source references:  47
citation contexts:  51
```

Claim types:

```text
policy implication: 17
background:          8
empirical finding:   5
limitation:          5
```

Claims by page:

```text
p.1:   6
p.3:   2
p.5:   8
p.7:   7
p.10:  9
p.11:  1
p.12:  2
```

## Generated outputs

```text
exports/paper_brief_CuadrosCasanova_2022.md
exports/writing_brief_CuadrosCasanova_2022_cap_design.md
docs/data/harness_export.json
```

The static UI export now contains:

```text
sources=3
claims=104
network nodes=309
network edges=684
```

## Key source-range claims extracted

The new extractor stores claims as exact source ranges. The displayed claim/evidence text is the source text from the selected character span, not an LLM rewrite.

High-value examples:

- `CLM-CuadrosCasanova_2022-0005` — proportional assignment of funds, quantitative targets, evidence-based interventions and monitoring indicators.
- `CLM-CuadrosCasanova_2022-0017` — CAP reform lacks specific targets and pathways for GHG reduction.
- `CLM-CuadrosCasanova_2022-0022` — only 30% of EAFRD Pillar II and around 7% of total CAP budget is bound to environment/climate investments, and allocation is not systematically linked to effective climate strategy.
- `CLM-CuadrosCasanova_2022-0024` — no strategies included to encourage pesticide reduction; lack of targets and monitoring likely limits CAP achievements.
- `CLM-CuadrosCasanova_2022-0029` — CAP conditionality requirements and voluntary schemes are functionally disconnected from EGD objectives.
- `CLM-CuadrosCasanova_2022-0031` — member states should allocate fixed portions of ecoscheme and AECM budgets to each environmental objective.
- `CLM-CuadrosCasanova_2022-0032` — long-term monitoring programs allow early trend identification and corrective actions, especially if payments are performance-based.

## Issues found and fixes implemented

## Issue 1: No batch source-range extraction existed

The user preference is now:

```text
claims should be source ranges, not LLM rewrites
```

Before this run, V2 could mark one exact span at a time but did not have a batch extractor.

Implemented:

```bash
python rh2.py extract-source-ranges SOURCE_ID
```

This command heuristically proposes exact source sentence ranges and stores them as:

```text
claim_representation = source_range
source_id + char_start + char_end = canonical claim identity
```

It deliberately does not ask an LLM to rewrite claim text.

## Issue 2: The paper contains translated Spanish abstract sections

The source has English abstract, Spanish abstract, Spanish keywords, and OCR/noise headings before the main English body.

Risk:

```text
duplicate claims from translated abstract
non-English duplicate evidence cluttering claim ledger
```

Fix implemented in `extract-source-ranges`:

- skip known low-value/non-English sections such as `PALABRAS CLAVE`, Spanish abstract headings and OCR-noise headings,
- skip figure/copyright/table boilerplate,
- focus on English abstract and main analytical sections.

## Issue 3: Page markers contain figure-only / near-empty pages

The parsed markdown has consecutive page markers such as page 4 then page 5, page 6 then page 7, page 8/9/10. This appears to come from figure-heavy pages in the PDF conversion.

Risk:

```text
page spans may exist but contain little/no prose
page assignment can look odd
```

Mitigation already in V2:

- claims use `char_start/char_end` as canonical anchors,
- page numbers are derived lazily from page spans,
- page numbers are treated as secondary metadata, not identity.

No destructive fix applied because the page markers may still be useful for human PDF cross-checking.

## Issue 4: Citation extractor falsely parsed reference-list entries as in-text citations

Initial extraction produced a false local match from a reference-list line:

```text
M. (2023) -> BadenBohm_2023
```

Cause:

```text
narrative citation regex was scanning the References section
```

Fix implemented:

```text
body_without_references(text)
```

`extract_intext_citations()` now runs only on source body before the References/Bibliography section.

Result:

```text
false local match removed
```

## Issue 5: Markdown-linked citations were not being captured well

Parsed papers often represent citations as:

```markdown
[Pe'er et al., 2022](#ref-042)
```

Initial citation extraction missed many of these.

Fix implemented:

- added markdown-linked citation parsing before ordinary parenthetical/narrative citation parsing.

After fix:

```text
CuadrosCasanova_2022 citation contexts: 51
source references: 47
```

All cited sources are currently missing locally, which is expected because the cited bibliography has not been ingested yet.

## Issue 6: Context retrieval should be sentence-aware, not fixed char windows

The new paper reinforced that local paragraph context is more useful than arbitrary character windows.

Already implemented and tested:

```bash
python rh2.py context CLAIM_ID --sentence-radius 1
python rh2.py context CLAIM_ID --sentence-radius 3
python rh2.py context CLAIM_ID --outside-paragraph
```

Default context is now sentence-aware and paragraph-bounded.

## Issue 7: Paper-level retrieval needed to recognize this source

Built/rebuilt paper-level index:

```bash
python rh2.py build-paper-index
```

Test query:

```bash
python rh2.py paper-search "CAP European Green Deal eco schemes environmental monitoring biodiversity climate sustainable agriculture" --limit 5
```

Top result:

```text
CuadrosCasanova_2022 score 0.2846
```

This confirms the paper-level vector/index works for source discovery.

## Current limitations / review needs

1. The 35 extracted source-range claims are **candidate** claims, not verified claims.
2. Some abstract claims duplicate arguments later developed in the body; review should prefer main-text claims for final citation where possible.
3. Claim-type inference is heuristic; some `background` claims may be better classified as `limitation` or `policy implication`.
4. Citation-context extraction is useful but still regex-based and imperfect.
5. No cited sources from this article have been imported yet, so all citation backtracking contexts are currently `missing_source`.
6. The source contains figure placeholders and multilingual content; future import cleaning should optionally remove translated abstracts and figure-only pages.

## Recommended use in thesis writing

This paper is useful mainly for:

- CAP 2023–2027 / EGD policy coherence,
- critique of CAP environmental ambition,
- evidence-based interventions and quantitative targets,
- monitoring and indicators,
- budget allocation to eco-schemes and AECMs,
- environmental conditionality and member-state flexibility,
- discussion of whether eco-scheme designs are sufficiently ambitious and measurable.

It is less directly useful for:

- farmer attitudes,
- subjective norms,
- institutional trust,
- individual adoption behaviour.

Best thesis use:

```text
RQ3 / policy-design discussion and chapter framing, not RQ2 behavioural determinant evidence.
```

## Next recommended action

Review the 35 source-range candidate claims and promote the strongest ones to `verified` after checking source context.

Useful commands:

```bash
python rh2.py writing-brief \
  "CAP Strategic Plans eco-schemes environmental monitoring quantitative targets funding allocation" \
  --source-id CuadrosCasanova_2022 \
  --section-type discussion \
  --limit 10

python rh2.py context CLM-CuadrosCasanova_2022-0031 --sentence-radius 2

python rh2.py review CLM-CuadrosCasanova_2022-0031 verified --note "checked against source"
```
