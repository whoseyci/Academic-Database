# Retrieval, Claim Indexing, Pages, Chunks, and Reference-Web Revision

## 1. Page numbers vs character spans

The canonical source anchor should be:

```text
source_id + char_start + char_end
```

Page numbers are secondary convenience metadata.

Recommended rule:

```text
Store char_start/char_end permanently.
Derive page number lazily when a claim is used or exported.
```

Why:

- Markdown/PDF conversions often lose or distort pages.
- Page numbers can change across PDF versions, preprints, publisher versions, OCR exports.
- Character spans against the canonical imported source are more stable inside the harness.
- Page lookup can be reconstructed from page spans whenever page markers exist.

Implemented in V2:

- `page_for_char(source_id, char_start)`
- `page_for_claim_dict(claim)`
- `claim_card()` now derives page lazily if the stored page is missing.

So the source anchor hierarchy is now:

```text
primary:   source_id + char_start + char_end
secondary: line_start + line_end
tertiary:  page number, resolved from page spans when available
```

## 2. Do we need chunks?

Chunks are not a scholarly data unit.

They are an **LLM batching artifact**.

Useful for:

- splitting a long source into model-sized extraction packets,
- feeding bounded source windows into an LLM,
- re-running extraction/evaluation on manageable segments.

Not useful as permanent evidence identity:

```text
claim should not depend on chunk_id
citation should not cite chunk_id
review should not trust chunk_id as source location
```

The final durable evidence object should be:

```text
claim_id
source_id
char_start
char_end
evidence quote
claim type
scope note
review status
```

Therefore, chunks may remain as ephemeral extraction spans, but should not be central to V2.

## 3. Which parts of a paper should be claim-indexed?

Not every sentence in a paper deserves claim indexing.

### High-value sections

#### Abstract

Index sparingly.

Useful for:

- headline findings,
- research aim,
- strongest conclusions.

Risk:

- abstract compresses nuance; verify important claims against main text.

#### Introduction

Index selectively.

Useful for:

- problem framing,
- definitions,
- research gaps,
- motivation,
- high-level theoretical claims.

Avoid:

- generic background everyone knows,
- unsupported rhetorical claims.

#### Literature review / theory

Index strongly.

Useful for:

- definitions,
- conceptual frameworks,
- known determinants,
- contradictions,
- theoretical mechanisms.

#### Methods

Index strongly, but differently.

Useful method claims:

- sample/population,
- geography/scope,
- research design,
- measurement instruments,
- modelling assumptions,
- statistical approach,
- limitations of data/method.

These support methods justification and scope interpretation.

#### Results

Index heavily.

Most empirical findings should come from here.

Good claims:

- measured effects,
- statistical associations,
- descriptive patterns,
- model outcomes,
- qualitative themes,
- comparative findings.

#### Discussion

Index carefully.

Useful for:

- authors' interpretation,
- mechanism explanations,
- policy implications,
- limitations,
- comparison with prior literature.

Risk:

- discussion often generalizes; claims need scope notes.

#### Conclusion

Index lightly.

Useful for:

- final policy implications,
- clearly bounded takeaways.

Risk:

- often repeats results at higher abstraction; avoid duplicate claims.

#### References

Do not claim-index references as claims.

Use references for the citation graph/reference-web.

### Practical section-indexing policy

```text
Abstract:       1–5 claims max, verify against body
Intro:          definitions, problem, gap only
Theory/Lit:     definitions, frameworks, determinants, contradictions
Methods:        design, sample, measures, scope, limitations
Results:        empirical findings heavily
Discussion:     interpretation, mechanisms, implications, limitations
Conclusion:     final takeaways sparingly
References:     citation graph only
```

## 4. LLM retrieval without context clogging

The LLM should not receive whole papers, whole chunks, or long paragraphs by default.

It should receive compact evidence cards:

```text
claim_id
claim
short evidence quote
citation hint
page if available
status
claim type
scope note
deep-dive command
```

Then the LLM may ask for context only when needed:

```bash
python rh2.py context CLAIM_ID --window 500
```

Implemented command:

```bash
python rh2.py writing-brief "query" --section-type discussion --limit 8 --token-budget 1800
```

Example:

```bash
python rh2.py writing-brief \
  "trust policy stability participation" \
  --section-type discussion \
  --limit 5 \
  --token-budget 900
```

This produces:

- a compact writing contract,
- warnings,
- budgeted claim cards,
- no source context unless explicitly requested,
- deep-dive commands for each claim.

The intended LLM workflow is:

```text
1. User gives chapter/paragraph task.
2. Harness builds writing-brief.
3. LLM drafts only from evidence cards.
4. LLM requests context for central/ambiguous claims only.
5. Draft is later audited against claim IDs.
```

## 5. Gold standard reference-web

A high-quality reference-web is not just a graph of papers that mention each other.

It should be a typed, multi-layer graph:

### Nodes

```text
Paper / source
Author
Year
Venue
Reference
Claim
Concept / construct
Method
Geography
Dataset / sample
Theory
Chapter / draft section
```

### Edges

```text
paper CITES paper
paper USES_METHOD method
paper STUDIES geography/population
paper USES_THEORY theory
paper HAS_CLAIM claim
claim SUPPORTS claim
claim CONTRADICTS claim
claim QUALIFIES claim
claim EXTENDS claim
claim APPLIES_TO geography/population
claim DERIVED_FROM evidence_span
chapter USES_CLAIM claim
```

### Citation graph levels

#### Level 1: Bibliographic citation graph

```text
source A cites source B
```

Requires reference parsing.

#### Level 2: Citation-context graph

```text
source A cites source B for reason X
```

Requires citation context extraction.

Example labels:

```text
background
method precedent
theoretical support
empirical comparison
contradiction
limitation
```

#### Level 3: Claim relation graph

```text
claim A supports/contradicts/qualifies claim B
```

This is more useful for writing than paper-level citation alone.

#### Level 4: Project relevance graph

```text
claim/source relates to RQ2, construct institutional trust, Andalusia scope, survey method
```

This helps choose evidence for thesis chapters.

### Gold standard build pipeline

```text
1. Parse references from source markdown/PDF output.
2. Normalize references with DOI/title/year matching.
3. Build source-to-source citation edges.
4. Extract citation contexts from body text.
5. Classify citation function.
6. Link claims to references where a claim depends on cited literature.
7. Retrieve similar claims and propose support/contradiction/qualification edges.
8. Human/model review confirms high-value relation edges.
9. Chapter briefs use both relevance and graph position.
```

### Graph storage recommendation

Start in SQLite:

```text
sources
references
citation_edges
citation_contexts
claims
claim_relations
concepts
claim_concepts
chapter_claims
```

Do not jump to Neo4j too early.

SQLite is enough until the schema stabilizes.

## 6. What changed in V2 now

Implemented:

- lazy page lookup in claim cards,
- `writing-brief` command for compact LLM evidence packets,
- section-type specific claim-type defaults,
- token-budgeted evidence card selection,
- warnings for unverified/page-missing evidence,
- stronger separation between evidence cards and source context.

New command:

```bash
python rh2.py writing-brief "query" \
  --section-type discussion \
  --limit 8 \
  --token-budget 1800
```

This is the preferred retrieval interface for LLM writing tasks.
