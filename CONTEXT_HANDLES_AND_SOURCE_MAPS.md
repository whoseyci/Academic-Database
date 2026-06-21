# Source Maps, Citation Summaries, Ranges, and Retrieval Handles

This document covers the lightweight context-compression layer added to V2.

The goal is to let an LLM see a compact map first, then retrieve exact source text only when needed.

## Core idea

Do not send whole papers or huge citation reports by default.

Instead:

```text
source-map / citation-summary / writing-brief
→ compact IDs and handles
→ exact retrieval only on demand
```

## 1. Source maps

Command:

```bash
python rh2.py source-map SOURCE_ID
```

Example:

```bash
python rh2.py source-map CuadrosCasanova_2022 --max-claims-per-section 2
```

Returns:

- source metadata,
- section headings,
- char ranges,
- page range when resolvable,
- estimated tokens,
- number of claims per section,
- number of citation contexts per section,
- claim handles,
- citation-context handles.

Example handle:

```text
SOURCE_RANGE[CuadrosCasanova_2022:32228-36564]
```

## 2. Citation summaries

Command:

```bash
python rh2.py citation-summary --source-id SOURCE_ID
```

Example:

```bash
python rh2.py citation-summary --source-id CuadrosCasanova_2022 --limit 10
```

Returns:

- reference count,
- citation context count,
- verification status counts,
- citation-function counts,
- top missing cited sources,
- priority citation contexts with handles.

Example handle:

```text
CITATION_CONTEXT[CITCTX-CuadrosCasanova_2022-00003]
```

## 3. Exact source range retrieval

Command:

```bash
python rh2.py source-text SOURCE_ID --range START:END
```

Example:

```bash
python rh2.py source-text CuadrosCasanova_2022 --range 32175:32475
```

This returns exact canonical source text for that character range.

## 4. Generic handle resolution

Command:

```bash
python rh2.py resolve-handle HANDLE
```

Supported handles:

```text
SOURCE_RANGE[source_id:start-end]
CLAIM[claim_id]
CITATION_CONTEXT[context_id]
PAPER[source_id]
```

Examples:

```bash
python rh2.py resolve-handle 'SOURCE_RANGE[CuadrosCasanova_2022:32175-32475]'
python rh2.py resolve-handle 'CLAIM[CLM-CuadrosCasanova_2022-0031]'
python rh2.py resolve-handle 'CITATION_CONTEXT[CITCTX-CuadrosCasanova_2022-00003]'
python rh2.py resolve-handle 'PAPER[CuadrosCasanova_2022]' --max-chars 5000
```

## Why this matters

This gives the LLM a reversible context protocol:

```text
compact map first
exact source later
```

It is inspired by context-compression / retrieval-handle systems but keeps academic evidence exact.

Never compress away:

- claim ID,
- source ID,
- char_start / char_end,
- evidence quote,
- verification status,
- scope note.

## Recommended LLM workflow

```text
1. Use paper-search or source-map to understand available source structure.
2. Use paper-brief or writing-brief to get relevant claim cards.
3. Use resolve-handle or context for exact source text only when needed.
4. Use citation-summary for citation backtracking overview.
5. Use citation-context for individual citation verification.
```

## Practical examples

Find relevant paper:

```bash
python rh2.py paper-search "CAP Green Deal monitoring targets" --limit 3
```

Map source:

```bash
python rh2.py source-map CuadrosCasanova_2022
```

Build writing evidence:

```bash
python rh2.py writing-brief \
  "CAP Strategic Plans eco-schemes monitoring targets" \
  --source-id CuadrosCasanova_2022 \
  --section-type discussion \
  --limit 8
```

Expand one claim:

```bash
python rh2.py resolve-handle 'CLAIM[CLM-CuadrosCasanova_2022-0031]' --sentence-radius 2
```

Exact section range:

```bash
python rh2.py resolve-handle 'SOURCE_RANGE[CuadrosCasanova_2022:32228-36564]'
```
