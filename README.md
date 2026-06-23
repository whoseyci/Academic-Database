# Research Harness V2

V2 is the cleaner, SQLite-first version of the source-card / claim-ledger harness.

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

## Agent protocol

See [`AGENT_PROTOCOL.md`](AGENT_PROTOCOL.md). Short version: agents should operate over claim IDs, evidence grades, review statuses and bounded source context — not vague model memory of the literature.

## Integrated PDF → Markdown pipeline

This repo now contains the local parser under `pdf_pipeline/` (merged from `PDF-to-MD`). The parser is optional and has separate dependencies; the core harness remains stdlib/SQLite-first.

```bash
# Convert PDF to paper.md + paper.parse.json
python -m pdf_pipeline.convert path/to/paper.pdf --out data/converted
python -m pdf_pipeline.validate data/converted/paper-slug

# Ingest the converted output directory into the claim ledger
python rh2.py ingest-converted data/converted/paper-slug --clean-markup
```

The parser emits `paper.parse.json`, a harness-compatible sidecar with page, section, paragraph, table, figure, reference and citation offsets over the final Markdown. This sidecar is what enables page-aware backtracking, source maps, source-card suggestions and claim-network construction.

Parser dependencies are intentionally separate:

```bash
pip install -r requirements-parser.txt
# optional figure/vision extras
pip install -r requirements-vision.txt
```

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

If your PDF→MD parser emits a sidecar map, ingest it too:

```bash
python rh2.py ingest ../uploads/paper.md \
  --source-id Paper_2025 \
  --parse-map ../uploads/paper.parse.json \
  --clean-markup
```

Mark a claim directly from a source sentence:

```bash
python rh2.py mark-claim \
  "The source sentence to mark." \
  --source-id BadenBohm_2023 \
  --json
```

Retrieve compact source cards / claim cards:

```bash
python rh2.py retrieve \
  "semi-natural habitat colony density landscape" \
  --source-id BadenBohm_2023 \
  --limit 5 \
  --fields minimal \
  --json
```

Use `card_role` to distinguish result claims from context cards:

```bash
python rh2.py retrieve "monitoring targets" \
  --source-id CuadrosCasanova_2022 \
  --card-role result_claim

python rh2.py retrieve "monitoring targets" \
  --source-id CuadrosCasanova_2022 \
  --card-role policy_design_card
```

Current roles include `result_claim`, `method_card`, `definition_card`, `theory_card`, `background_card`, `limitation_card`, `policy_design_card`, and `contradiction_card`.

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
python rh2.py reference-report --source-id Canessa_2024 --limit 20
python rh2.py citation-summary --source-id Canessa_2024 --limit 20
python rh2.py citation-report --source-id Canessa_2024 --limit 20
python rh2.py citation-context CITCTX-Canessa_2024-00001 --limit 5
```

Build chapter-aware citation backtracking:

```bash
python rh2.py chapter-citations eco_schemes_discussion_rq2_rq3 \
  --context-scope paragraph
```

This classifies upstream cited sources by how they support a chapter section, e.g. `methods_source`, `theory_source`, `empirical_support_source`, or `policy_design_source`.

Reference identities are now stable: DOI-backed references use DOI-derived canonical IDs, while non-DOI references use deterministic `author + first title word + year` IDs.

Suggest likely support locations inside the backtracked/cited paper:

```bash
python rh2.py repair-citation-contexts --source-id Canessa_2024 --dry-run
python rh2.py suggest-cited-claim-location CITCTX-Canessa_2024-00001 --limit 10
python rh2.py suggest-cited-claim-location CITCTX-Canessa_2024-00001 --store --json
# Tip: ingest parsed markdown with --clean-markup before backtracking annotated papers.
python rh2.py citation-location-suggestions --context-id CITCTX-Canessa_2024-00001
python rh2.py verify-location CLOC-... accepted --note "Best support location."
python rh2.py accept-citation-location CLOC-... --citing-claim-id CLAIM_ID --relation-type supports
```

Suggest source-card candidates directly from a parsed markdown source:

```bash
python rh2.py suggest-source-cards Dessart_2019 --store
python rh2.py source-card-suggestions --source-id Dessart_2019 --card-role result_claim
python rh2.py accept-source-card-suggestion SCSUG-... --status candidate_needs_review
```

Learn from reviewed suggestions and triage what to read/import next:

```bash
python rh2.py train-rankers
python rh2.py reference-match-queue --source-id Canessa_2024
python rh2.py resolve-reference REF-Canessa_2024-ref_dessart_behavioural_2019 --matched-source-id Dessart_2019
python rh2.py reading-priority --query "contract flexibility transaction costs" --missing-only
python rh2.py assess-citation-location CLOC-...
```

Audit a draft for claim traceability:

```bash
python rh2.py audit-draft chapter.md
```

Prioritize and inspect claims for review:

```bash
python rh2.py review-queue --status candidate_needs_review --grade C --limit 20
python rh2.py review-packet CLM-Canessa_2024-0020
python rh2.py review CLM-Canessa_2024-0020 verified --label good_claim --note "Checked source/page/scope."
```

Revise, supersede, mark duplicates, or split broad claims:

```bash
python rh2.py revise-claim CLAIM_ID --scope-note "Narrowed to EU AECM evidence." --label revised
python rh2.py supersede OLD_CLAIM NEW_CLAIM --note "New source-range claim is tighter."
python rh2.py duplicate DUPLICATE_CLAIM CANONICAL_CLAIM --reject-duplicate
python rh2.py split-claim BROAD_CLAIM --file split_specs.json --inherit-tags --supersede-original
```

List computed evidence grades:

```bash
python rh2.py evidence-grades --grade C
```

Record a contradiction/qualification/tension between claims:

```bash
python rh2.py relate CLAIM_A CLAIM_B contradicts --note "Different geography or method."
python rh2.py relations --claim-id CLAIM_A
```

Check stats:

```bash
python rh2.py stats
```

## Important concept: source cards, not generic claims

The canonical evidence object is now a **source card**: an exact source-backed character range with a writing role.

The database table is:

```text
source_cards
```

A compatibility SQL view named `claims` remains for older queries/UI code.

A source card can be a direct empirical result claim, but it can also be a method card, definition card, limitation card, background card, or policy design card. Use `card_role` to distinguish these.

## Important concept: source-sentence/source-range claims

V2 does not force the model to rewrite source text.

Recommended policy:

```text
source sentence clear and atomic      -> claim_representation = source_quote
source sentence noisy but usable      -> claim_representation = lightly_normalized_source
source sentence compound/ambiguous    -> claim_representation = paraphrase
```

The exact source evidence is always stored and anchored by character offsets.

## Evidence grades and draft traceability

Claim cards include a conservative computed `evidence_grade`:

```text
A = verified, page-anchored, source-like representation
B = verified or strongly anchored but not perfect
C = usable candidate/paraphrase or page-uncertain support
D = weak/unanchored candidate
X = rejected or superseded
```

Drafts should attach claim IDs to substantive statements, for example:

```markdown
Trust and perceived policy stability are associated with AECM uptake. <!-- claims: CLM-Canessa_2024-0020 -->
```

Then run `python rh2.py audit-draft chapter.md` before final use.

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

Chapter profiles are documented in [`config/chapter_profiles/SCHEMA.md`](config/chapter_profiles/SCHEMA.md).
