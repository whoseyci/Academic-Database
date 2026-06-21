# Citation Backtracking V0

## Status

A first non-perfect but working V0 has been added to `rh2.py`.

It can now:

```text
source paper A
→ extract reference-list entries
→ extract in-text citation contexts
→ classify rough citation function
→ try to match cited source B to local library
→ flag missing cited papers
→ show citing context and candidate claims from B if B exists locally
→ allow manual verification labels
```

This is the start of the "paper A uses paper B to support/contradict claim X" workflow.

## New DB tables

```text
source_references
citation_contexts
```

## New commands

Extract citation contexts:

```bash
python rh2.py extract-citations Canessa_2024 --clear
```

Show citation report:

```bash
python rh2.py citation-report --source-id Canessa_2024 --limit 20
```

Inspect one citation context and retrieve claims from cited paper if it exists locally:

```bash
python rh2.py citation-context CITCTX-Canessa_2024-00001 --limit 5
```

Verify/label a citation context:

```bash
python rh2.py verify-citation CITCTX-Canessa_2024-00001 verified_accurate --note "checked against cited source"
```

Allowed verification statuses:

```text
verified_accurate
verified_inaccurate
misleading
missing_source
needs_source
needs_context
not_relevant
```

## Current extraction results

After running V0 extraction:

```text
Canessa_2024:
  source_references: 111
  citation_contexts: 408
  matched local cited sources: 0
  missing cited source contexts: 408

BadenBohm_2023:
  source_references: 71
  citation_contexts: 12
  matched local cited sources: 0
  missing cited source contexts: 12
```

The zero local matches are expected because the local library currently only has Canessa and BadenBöhm. Their cited papers have not been ingested as sources yet.

## What this enables

The intended future loop:

```text
1. Extract citations from paper A.
2. See that paper A cites paper B for claim/context X.
3. If B is missing, flag/import B.
4. Once B exists locally, rerun extraction/matching.
5. Inspect citation context in A.
6. Retrieve candidate claims from B relevant to that context.
7. Human/model verifies whether A accurately uses B.
8. Store verification label.
```

Over time, this builds a grounded citation web:

```text
paper A cites paper B
paper A's citation context says X
paper B's actual source span says Y
verification: accurate / misleading / unsupported / needs context
```

## Why this is powerful

This moves beyond a normal citation graph.

Normal citation graph:

```text
A cites B
```

Harness citation backtracking:

```text
A cites B in sentence S to support claim X.
B contains evidence span Y.
A's use of B is accurate/misleading/unsupported.
```

That would let the system detect:

- citation laundering,
- overclaiming,
- unsupported literature-review statements,
- papers repeatedly used for claims they do not actually support,
- strong evidence chains,
- weak links in an argument.

## Realism check

This is a big project, but not too big if built in layers.

Hard parts:

- reference parsing is messy,
- citation contexts can cite multiple papers at once,
- citation intent classification is imperfect,
- source B may not be in the local library,
- cited source B may itself rely on source C,
- verifying whether A accurately uses B requires semantic judgment.

Therefore the correct framing is:

```text
automatic extraction = candidate citation contexts
human/model review = verification
verified citation links = trustworthy reference web
```

## Next upgrades needed

1. Better reference parsing with DOI/title normalization.
2. Import cited references by DOI/title from Crossref/OpenAlex/Semantic Scholar.
3. Citation-context function classifier.
4. `missing-source queue` command.
5. Claim-to-citation linking: identify which claim in A each citation supports.
6. Multi-source citation handling.
7. UI for citation verification.
8. Citation graph export.
9. Draft audit that checks whether your own citations accurately support your claims.

## Bottom line

This is feasible and potentially very powerful, but it must be treated as a gradual evidence-verification system, not a fully automatic truth machine.
