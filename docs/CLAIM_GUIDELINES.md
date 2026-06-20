# Claim Guidelines

## What is a claim?

A claim is an atomic, source-backed statement that could support a sentence, paragraph, argument, method justification, limitation, or interpretation in academic writing.

In this harness, a good claim has:

1. **One idea** — not a whole paragraph of mixed points.
2. **A source anchor** — exact evidence text with page/span/line metadata where possible.
3. **A scope** — where the claim applies and where it should not be overgeneralized.
4. **A type** — empirical finding, theory, method, definition, policy implication, limitation, background, contradiction.
5. **A review status** — candidate, verified, rejected, etc.

---

## Evidence ratio rule

Evidence is **not** the same as context.

For one claim, evidence should normally be:

```text
1 exact source sentence, or
2 short adjacent sentences if necessary,
not a whole paragraph/chunk.
```

Target size:

```text
ideal:       100–400 characters
acceptable: 400–800 characters when genuinely needed
suspicious: >800 characters for one atomic claim
bad:        entire paragraphs with multiple unrelated claims
```

If the writer/model needs more surrounding material, use the context view. Do not stuff context into the evidence field.

---

## The claim test

Before marking something as a claim, ask:

### 1. Could this support academic prose?

Good:

> Trust and perceived policy stability were positively associated with AECM uptake in most significant cases.

Bad:

> Table 4 shows model results.

### 2. Is it atomic?

Good:

> Semi-natural habitat area had a stronger positive effect on bumblebee colony numbers than biodiversity-measure area.

Too broad:

> The paper discusses biodiversity measures, food resources, nesting habitat, landscape context, and model limitations.

### 3. Is it source-backed?

Good:

> Claim has an exact evidence quote and source span.

Bad:

> Model inferred a conclusion loosely inspired by a section.

### 4. Is the scope clear?

Good:

> Applies to modelled Bombus terrestris colony density in three German agricultural landscapes.

Bad:

> Biodiversity measures always increase pollinators.

---

## Claim representation modes

### `source_quote`

Use when the source sentence is already clear and atomic.

Example:

> The area of semi-natural habitats had a stronger positive effect on number of bumblebee colonies than the area of biodiversity measures.

This is often the best option.

### `lightly_normalized_source`

Use when the source sentence is clear but has formatting issues, OCR noise, broken line breaks, or awkward punctuation.

### `paraphrase`

Use only when needed:

- the source sentence is too long,
- the sentence contains multiple ideas and needs splitting,
- pronouns/referents need clarification,
- the claim needs to be normalized into a reusable academic statement.

Always keep the exact source sentence as `evidence`.

---

## Claim types

### Empirical finding

A result from data, analysis, modelling, experiment, survey, interview, review synthesis, etc.

Example:

> Neighbouring farmers’ opinions or participation, when significant, consistently showed a positive effect on AECM adoption.

### Theoretical claim

A conceptual or explanatory statement.

Example:

> The Theory of Planned Behaviour links intentions and behaviour to attitudes, subjective norms and perceived behavioural control.

### Methodological claim

A claim about method, design, sample, model, measurement, or analytical strategy.

Example:

> The study used the BumbleBEEHAVE agent-based model to simulate colony development.

### Definition

A source-backed definition of a concept.

Example:

> Alignment refers to how well a measure fits a farmer’s interests, identity, values or production system.

### Policy implication

A source-backed implication for design, governance, intervention or practice.

Example:

> Bureaucratic simplification and flexible contracts may improve uptake.

### Limitation

A constraint, uncertainty, weakness, or boundary condition.

Example:

> Grassland and forest were not defined as food or nesting habitats, likely underrating their importance for bumblebees.

### Background

Useful contextual information, but not a core result or theory.

### Contradiction

A claim that explicitly conflicts with or qualifies another claim.

---

## What is not a claim?

Usually not claims:

- paper titles,
- headings,
- keywords,
- isolated references,
- generic topic labels,
- figure captions without interpretive content,
- vague summaries,
- unsupported model conclusions,
- whole paragraphs with several unrelated ideas.

---

## Best LLM behaviour

The LLM should not rewrite everything.

Preferred workflow:

```text
1. Find a sentence/excerpt that matters.
2. If atomic and clear, mark it as source_quote.
3. If compound, split it into multiple claims.
4. If paraphrasing, preserve exact evidence quote.
5. Add claim_type, tags, scope note and confidence.
```

Best tool:

```bash
python rh2.py mark-claim "exact source sentence" --source-id SOURCE_ID --json
```

---

## Publication-safety rule

A claim is not publication-safe until it is reviewed.

Statuses:

```text
candidate_needs_review  -> useful but provisional
needs_source_check       -> source anchor or interpretation needs checking
needs_page_check         -> source evidence found, page missing/uncertain
verified                 -> checked and usable
rejected                 -> not usable
```

---

## Writing rule

When writing a paper or thesis chapter:

```text
Every substantive statement should map to at least one claim_id.
Central claims should be deep-dived with context before use.
Scope limitations should be preserved.
```
