# Fresh V2 Reprocess Report — 2026-06-22

## Why this run was done

The user asked to run the papers through the current harness pipeline **as if they were new**, after the shift from generic `claims` to canonical `source_cards` and source-range-first extraction.

This run intentionally did **not** import the old V1 curated claim JSON. It rebuilt the V2 database from the paper markdown sources and extracted exact source-range source cards using the current V2 pipeline.

## Important note on context compression

The assistant cannot manually trigger Arena/LM Arena system-level context compression. That is controlled by the platform. What the harness can do locally is provide its own context-compression-like tools:

- `source-map`
- `citation-summary`
- `writing-brief`
- `paper-brief`
- `resolve-handle`
- sentence-aware `context`

These reduce what is sent to the LLM while preserving exact source range retrieval.

## Sources reprocessed

1. `Canessa_2024`
   - File: `uploads/High - Canessa et al., 2024.md`
   - Title: *What matters most in determining European farmers’ participation in agri-environmental measures?*
   - Important: ingested with `--clean-markup` because the source markdown contained old annotation noise (`==`, `[PAGE UNVERIFIED]`, `#MA/...`).

2. `BadenBohm_2023`
   - File: `uploads/paper.md`
   - Title: *Biodiversity measures providing food and nesting habitat increase the number of bumblebee colonies...*

3. `CuadrosCasanova_2022`
   - File: `uploads/paper (2).md`
   - Title: *Opportunities and challenges for Common Agricultural Policy reform to support the European Green Deal*

## Commands run

The database and paper index were backed up, then rebuilt:

```bash
rm -f harness_v2.db harness_v2.db-wal harness_v2.db-shm paper_index.pkl
rm -f blobs/*.gz
python rh2.py init
```

Then sources were ingested and source-range source cards were extracted:

```bash
python rh2.py ingest '../uploads/High - Canessa et al., 2024.md' --source-id Canessa_2024 ... --clean-markup
python rh2.py ingest '../uploads/paper.md' --source-id BadenBohm_2023 ...
python rh2.py ingest '../uploads/paper (2).md' --source-id CuadrosCasanova_2022 ...

python rh2.py extract-source-ranges Canessa_2024 --min-score 1.5 --limit 40
python rh2.py extract-source-ranges BadenBohm_2023 --min-score 1.5 --limit 30
python rh2.py extract-source-ranges CuadrosCasanova_2022 --min-score 2.4 --limit 40
```

Citation extraction, indexing and exports:

```bash
python rh2.py extract-citations Canessa_2024 --clear
python rh2.py extract-citations BadenBohm_2023 --clear
python rh2.py extract-citations CuadrosCasanova_2022 --clear
python rh2.py build-paper-index
python rh2.py chapter-brief eco_schemes_discussion_rq2_rq3
python rh2.py chapter-brief example_ecology_discussion_badenbohm
python rh2.py chapter-citations eco_schemes_discussion_rq2_rq3 --context-scope paragraph
python export_static_site.py
```

## Final database stats

```text
sources:                       3
spans:                         690
source_cards:                  85
claim_tags:                    157
claim_relations:               0
source_references:             229
citation_contexts:             542
citation_location_suggestions: 0
review_events:                 0
review_labels:                 0
db_bytes:                      1,155,072
blob_bytes:                    76,461
```

## Source cards by source

```text
Canessa_2024:            36
BadenBohm_2023:          14
CuadrosCasanova_2022:    35
Total:                   85
```

## Source cards by type

### Canessa_2024

```text
background:              7
definition:              1
empirical finding:       8
limitation:              3
methodological claim:    5
policy implication:      12
```

### BadenBohm_2023

```text
background:              4
empirical finding:       3
limitation:              1
methodological claim:    5
policy implication:      1
```

### CuadrosCasanova_2022

```text
background:              8
empirical finding:       5
limitation:              5
policy implication:      17
```

## Citation extraction results

```text
Canessa_2024:
  source_references:      111
  citation_contexts:      397
  linked to ref list:     388
  matched local sources:  0

BadenBohm_2023:
  source_references:      71
  citation_contexts:      94
  linked to ref list:     93
  matched local sources:  0

CuadrosCasanova_2022:
  source_references:      47
  citation_contexts:      51
  linked to ref list:     50
  matched local sources:  0
```

No local upstream cited sources are matched yet because the cited papers have not been imported as separate sources.

## Generated outputs

```text
reports/reprocess_Canessa_2024_extract.json
reports/reprocess_BadenBohm_2023_extract.json
reports/reprocess_CuadrosCasanova_2022_extract.json
reports/reprocess_Canessa_2024_citations.json
reports/reprocess_BadenBohm_2023_citations.json
reports/reprocess_CuadrosCasanova_2022_citations.json
reports/reprocess_final_stats.txt

exports/chapter_brief_eco_schemes_discussion_rq2_rq3.json
exports/chapter_brief_eco_schemes_discussion_rq2_rq3.md
exports/chapter_brief_example_ecology_discussion_badenbohm.json
exports/chapter_brief_example_ecology_discussion_badenbohm.md
exports/chapter_citations_eco_schemes_discussion_rq2_rq3.json
exports/chapter_citations_eco_schemes_discussion_rq2_rq3.md
exports/paper_brief_CuadrosCasanova_2022.md
exports/writing_brief_CuadrosCasanova_2022_cap_design.md

docs/data/harness_export.json
paper_index.pkl
```

Static export now contains:

```text
sources=3
source cards/claims=85
network nodes=139
network edges=437
chapter briefs=2
```

## Fixes / hardening implemented during this run

### 1. Clean markup ingestion for annotated markdown

Problem:

`High - Canessa et al., 2024.md` contained old harness/thesis annotations:

```text
==highlight==
[PAGE UNVERIFIED]
#MA/...
```

These were contaminating source-range source cards.

Fix:

Added:

```bash
python rh2.py ingest SOURCE.md --clean-markup
```

This removes known annotation noise before canonical ingest.

Result:

```text
Canessa source cards now contain 0 occurrences of #MA, [PAGE UNVERIFIED], or == markers.
```

### 2. Source-range extractor now rejects sentence fragments

Problem:

PDF page breaks produced incomplete source card candidates, e.g. fragments ending mid-sentence.

Fix:

The candidate extractor now skips sentence-like spans that do not end with sentence punctuation and rejects table/page-break fragments beginning with numeric remnants.

Result:

The worst BadenBöhm fragments such as `4; Appendix...` were removed.

### 3. Method-section classification improved

Problem:

Some method/study-area cards were misclassified as policy implications.

Fix:

Heading-aware classification now treats sections like `Study areas`, `Modelling approach`, `Material and methods`, etc. as methodological unless stronger evidence suggests otherwise.

## Issues still flagged

### 1. Extraction quality is heuristic

The new run uses the current source-range extractor, not a human/LLM-reviewed extraction packet. It is cleaner and more principled, but still heuristic.

The source cards are all:

```text
candidate_needs_review
```

They should not be treated as final publication-safe evidence.

### 2. Canessa has no page numbers

The cleaned Canessa markdown still has no reliable page markers, so page remains blank for Canessa cards.

Canonical anchor remains:

```text
source_id + char_start + char_end
```

### 3. BadenBöhm extraction remains sparse

Only 14 source cards were extracted from BadenBöhm. This is acceptable for a strict heuristic run, but it misses some known useful result statements from the abstract/results.

Future improvement:

```text
batch LLM source-range extraction
```

where the model selects exact source ranges but does not rewrite them.

### 4. Some background/context cards remain

Not all source cards are result claims. Use `card_role` to filter:

```bash
python rh2.py retrieve "query" --card-role result_claim
python rh2.py retrieve "query" --card-role method_card
python rh2.py retrieve "query" --card-role policy_design_card
```

### 5. Citation backtracking remains missing-source-heavy

Citation contexts are now extracted and coupled to references with stable IDs, but upstream cited papers are not imported locally.

Next logical step:

```text
missing-source queue + import cited high-priority sources
```

## Recommended next action

Review the high-priority source cards before writing from them:

```bash
python rh2.py review-queue --limit 20
python rh2.py review-packet CLM-Canessa_2024-0003
```

For thesis writing, use role-filtered briefs:

```bash
python rh2.py writing-brief \
  "trust policy stability participation" \
  --source-id Canessa_2024 \
  --card-role result_claim \
  --limit 8
```

For policy design:

```bash
python rh2.py writing-brief \
  "eco-schemes monitoring quantitative targets funding allocation" \
  --source-id CuadrosCasanova_2022 \
  --card-role policy_design_card \
  --limit 10
```
