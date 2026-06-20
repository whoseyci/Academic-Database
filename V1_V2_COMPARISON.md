# V1 vs V2 Comparison

## Summary

V1 is the working prototype. V2 is the cleaner architecture built around source spans, source-sentence claims, compact retrieval, and chapter-level evidence packets.

## Measured local storage after cleanup/import

```text
research_harness/     ~3.9 MB
research_harness_v2/  ~916 KB
```

Selected files:

```text
V1 database:           ~2.1 MB
V1 compact index:      ~217 KB
V2 database:           ~592 KB
V2 compressed blobs:   ~60 KB total for two sources
```

These numbers are not final benchmarks, but they show the direction: V2 avoids storing repeated full text files for chunks/pages/paragraphs and uses compressed source blobs.

## Feature comparison

| Dimension | V1 | V2 |
|---|---|---|
| Project role | Prototype/debug harness | Cleaner production architecture |
| Primary store | JSONL + SQLite mirror | SQLite primary |
| Source storage | Markdown copied in `sources/`; chunks/pages/paragraphs also store text | Canonical gzip source blob; spans store offsets |
| Pages/chunks/paragraphs | JSONL records with duplicated text | `spans` table with char/line/page offsets |
| Claim representation | Mostly paraphrased model/human claims | Source sentence can be the claim; paraphrase optional |
| Claim marking | `mark-claim` added late to V1 | `mark-claim` is central design primitive |
| Retrieval | `search`, `fts-search`, `vsearch`, `retrieve` added | `retrieve` is primary compact model-facing endpoint |
| Context expansion | `locate` / `context` | `context` over canonical source blobs |
| Chapter preparation | Evidence packet command | Chapter profiles + chapter briefs |
| Artifact policy | Many generated files kept | More rebuildable-by-design |
| Model ergonomics | CLI usable but broader/debuggy | ID-first, compact cards, context on demand |
| Storage efficiency | Medium; duplicate text remains | Better; compressed source + spans |
| Best use | Development, inspection, backward compatibility | Future foundation |

## Retrieval philosophy

### V1

```text
claims/chunks/paragraphs/pages can all be indexed
model may call multiple search tools
debug visibility is high
```

### V2

```text
retrieve compact claim cards first
expand source context only for selected claim IDs
build chapter-specific evidence briefs upfront
```

## Claim representation philosophy

### V1 default tendency

```text
source text -> model rewrites -> claim + evidence
```

### V2 preferred tendency

```text
source sentence is already atomic -> use source sentence as claim
source sentence is compound/noisy -> split or lightly normalize
interpretive synthesis needed     -> paraphrase, but keep exact evidence quote
```

This reduces distortion and makes verification easier.

## Chapter-writing pipeline comparison

### V1 evidence packet

Good for collecting claims for a query.

### V2 chapter brief

Better for academic writing because it provides:

- chapter purpose,
- section-level retrieval queries,
- writing contract,
- grouped claim cards,
- warnings about unverified evidence,
- source/status coverage,
- deep-dive commands for claim context.

Example:

```bash
cd research_harness_v2
python rh2.py chapter-brief eco_schemes_discussion_rq2_rq3
```

Outputs:

```text
exports/chapter_brief_eco_schemes_discussion_rq2_rq3.json
exports/chapter_brief_eco_schemes_discussion_rq2_rq3.md
```

## What V2 still needs

V2 is not finished. Next work:

1. Add dense embeddings as BLOBs.
2. Add ANN backend or sqlite-vec.
3. Add citation/draft audit.
4. Add batch `mark-claims` for model outputs.
5. Add write-back review UI.
6. Add source-quality weighting and chapter-specific source diversity constraints.
7. Add relation detection: supports, contradicts, qualifies, extends.
8. Add claim coverage dashboards per chapter/RQ.

## Recommendation

Keep V1 as a debug/reference implementation for now, but develop V2 as the future path.

Practical rule:

```text
Use V1 when you want transparency/debug artifacts.
Use V2 when you want scalable thesis/chapter evidence preparation.
```
