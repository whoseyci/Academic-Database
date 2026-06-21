# Research Harness V2

V2 is the cleaner, SQLite-first version of the claim-ledger harness.

## Design shift from V1

V1 is a debug-friendly prototype: JSONL files, duplicated chunk/page/paragraph text, and many generated artifacts.

V2 is a production-oriented architecture:

```text
canonical source text once as compressed blob
+ spans over source text
+ source-sentence or paraphrased claims
+ compact model-facing retrieval
+ chapter evidence briefs
```

## GitHub Pages UI

The repo includes a static GitHub Pages interface in:

```text
docs/index.html
```

The UI reads:

```text
docs/data/harness_export.json
```

The main writing use-case is the **Topic Explorer** tab: enter a topic/chapter idea such as `trust policy stability`, `contract flexibility`, or `semi-natural habitats`, and the page returns related claim cards, evidence excerpts, source/status/topic observations, and a copyable evidence packet. This is client-side lexical retrieval over the exported claim ledger, not an LLM and not full semantic embedding search yet.

To enable it on GitHub if it is not already active:

1. Open the repository on GitHub.
2. Go to **Settings → Pages**.
3. Under **Build and deployment**, choose **Deploy from a branch**.
4. Choose branch `main` and folder `/docs`.
5. Save.

The expected URL is:

```text
https://whoseyci.github.io/Academic-Database/
```

Note: full source papers are intentionally ignored via `.gitignore` (`blobs/*.gz`) to avoid publishing copyrighted source text. The GitHub Pages export includes metadata, claim/evidence excerpts, tags, network data and bounded context snippets.

## Key commands

Initialize:

```bash
python rh2.py init
```

Import the current V1 project:

```bash
python rh2.py import-v1 ../research_harness
```

Ingest a markdown source directly:

```bash
python rh2.py ingest ../uploads/paper.md \
  --source-id BadenBohm_2023 \
  --title "..." \
  --authors "..." \
  --year 2023 \
  --disciplines "landscape ecology; agroecology" \
  --geography "Germany" \
  --methodology "agent-based simulation modelling" \
  --quality high
```

Mark a claim directly from a source sentence:

```bash
python rh2.py mark-claim \
  "The source sentence to mark." \
  --source-id BadenBohm_2023 \
  --json
```

Retrieve compact claim cards:

```bash
python rh2.py retrieve \
  "semi-natural habitat colony density landscape" \
  --source-id BadenBohm_2023 \
  --limit 5 \
  --fields minimal \
  --json
```

Expand source context for one claim:

```bash
python rh2.py context CLM-BadenBohm_2023-0015 --window 500 --json
```

Build a compact writing brief for an LLM/human paragraph or section:

```bash
python rh2.py writing-brief \
  "trust policy stability participation" \
  --section-type discussion \
  --limit 8 \
  --token-budget 1800
```

Build a chapter evidence brief:

```bash
python rh2.py chapter-brief eco_schemes_discussion_rq2_rq3
```

Compact source map with retrieval handles:

```bash
python rh2.py source-map CuadrosCasanova_2022 --max-claims-per-section 2
```

Retrieve exact source text by range:

```bash
python rh2.py source-text CuadrosCasanova_2022 --range 32175:32475
```

Resolve handles emitted by source maps, citation summaries and briefs:

```bash
python rh2.py resolve-handle 'SOURCE_RANGE[CuadrosCasanova_2022:32175-32475]'
python rh2.py resolve-handle 'CLAIM[CLM-CuadrosCasanova_2022-0031]'
python rh2.py resolve-handle 'CITATION_CONTEXT[CITCTX-CuadrosCasanova_2022-00003]'
```

Extract citation contexts for reference-web/backtracking:

```bash
python rh2.py extract-citations Canessa_2024 --clear
python rh2.py citation-summary --source-id Canessa_2024 --limit 20
python rh2.py citation-report --source-id Canessa_2024 --limit 20
python rh2.py citation-context CITCTX-Canessa_2024-00001 --limit 5
```

Check stats:

```bash
python rh2.py stats
```

## Important concept: source-sentence claims

V2 does not force the model to rewrite source text.

Recommended policy:

```text
source sentence clear and atomic      -> claim_representation = source_quote
source sentence noisy but usable      -> claim_representation = lightly_normalized_source
source sentence compound/ambiguous    -> claim_representation = paraphrase
```

The exact source evidence is always stored and anchored by character offsets.

## Current imported trial stats

After importing V1:

```text
sources: 2
spans: 543
claims: 69
claim_tags: 771
db: ~592 KB
compressed source blobs: ~60 KB
```

## Chapter brief philosophy

The LLM should not see the whole database. It should receive a chapter brief:

```text
chapter purpose
writing contract
section-specific claim cards
warnings/gaps
claim IDs for deep dives
```

Then the model can call:

```bash
python rh2.py context CLAIM_ID --window 600
```

for claims it intends to use centrally.
