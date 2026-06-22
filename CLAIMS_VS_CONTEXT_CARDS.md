# Claims vs Context Cards

## Why this matters

The harness originally used the word `claim` for every source-backed proposition. That is technically defensible in argument-mining terms, but it is too blunt for thesis writing.

A results statement, a methods description, a definition, and a literature-background sentence are not used the same way in academic prose.

So the better model is:

```text
source range card = exact source span stored in the database
card_role         = how this span should be used in writing
```

The table is still called `claims` for backwards compatibility, but LLMs and humans should use `card_role` to decide how to treat a card.

---

## Pushback / nuance

The user intuition is mostly right:

> Everything apart from the paper results is not really a claim.

For thesis-writing purposes, yes: **results/finding statements are the core evidence claims**.

But academically, methods, definitions, limitations and theory statements are still propositions that can be cited. They should not disappear. They should be separated into different card roles.

Example:

```text
"The study used a survey of 83 Andalusian olive farmers."       -> method_card
"Subjective norms refer to perceived social pressure."          -> definition_card
"Payments were positively associated with adoption."            -> result_claim
"The authors recommend fixed eco-scheme budget allocations."    -> policy_design_card
"The sample is not representative."                             -> limitation_card
```

Only the third is a direct empirical/result claim. The others are context cards that help interpret or frame claims.

---

## Current card roles

Implemented in `rh2.py` and static export:

```text
result_claim          empirical finding / result-like evidence
method_card           methods, sample, model, measurement, research design
definition_card       definitions of concepts/constructs
theory_card           theoretical mechanisms/frameworks
background_card       context/literature/problem framing
limitation_card       caveats, constraints, uncertainty
policy_design_card    recommendations/design implications
contradiction_card    explicit contradiction/qualification
unknown_card          fallback
```

## Mapping from existing `claim_type`

```text
empirical finding      -> result_claim
methodological claim   -> method_card
definition             -> definition_card
theoretical claim      -> theory_card
background             -> background_card
limitation             -> limitation_card
policy implication     -> policy_design_card
contradiction          -> contradiction_card
```

This is a first-pass mapping. Later we can improve it using section information and citation function.

---

## How LLMs should use cards

## Result claims

Use as evidence for substantive empirical statements.

```text
CLM-... supports: "X was associated with Y".
```

## Method cards

Use to define source scope and comparability.

```text
This was a modelled study, not direct field observation.
This was a European review, not Andalusian survey evidence.
```

## Definition cards

Use when defining concepts in theory/literature sections.

## Background cards

Use sparingly for framing. They are not primary evidence unless the chapter is background/introduction.

## Limitation cards

Use to avoid overclaiming and to audit interpretation.

## Policy design cards

Use for discussion/recommendation sections, but distinguish them from empirical results.

---

## Retrieval implications

For empirical discussion:

```bash
python rh2.py writing-brief "query" --card-role result_claim
```

For methods comparison:

```bash
python rh2.py writing-brief "survey sample measurement" --card-role method_card
```

For policy recommendations:

```bash
python rh2.py writing-brief "eco-scheme monitoring targets" --card-role policy_design_card
```

For source inventory:

```bash
python rh2.py paper-brief "query" --paper-limit 2
```

This returns all selected paper cards unless filtered.

---

## Static UI context snippets

The static export previously used a fixed character window around claims. That is now corrected.

`context_snippet` is sentence-aware and paragraph-bounded:

```text
claim sentence + one sentence before + one sentence after
within the same paragraph
```

This mirrors the CLI `context` default and avoids arbitrary cutoffs.

---

## Future improvement

Long-term, the schema should probably rename `claims` to something like:

```text
source_cards
```

and reserve `claim` for:

```text
result_claim
interpretive_claim
argument_claim
```

But renaming the table now would create churn. The practical compromise is:

```text
keep table name: claims
add field: card_role
teach LLM/UI to respect card_role
```

---

## Bottom line

The harness should not treat all source spans as equal evidence claims.

It should treat them as typed source cards:

```text
result claims = direct evidence
method/definition/background/limitation cards = interpretive scaffolding
policy cards = recommendations/design implications
```

This preserves useful academic material without clogging thesis evidence with non-results as if they were empirical claims.
