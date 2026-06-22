# Source Cards Migration

## Why this migration happened

The harness originally used the word `claims` for every source-backed text range. That became misleading.

A paper contains multiple kinds of useful source ranges:

```text
result claims
method descriptions
definitions
theoretical framing
background/context
limitations
policy design implications
citation contexts
```

Only some of these are literal claims in the narrow academic sense. The better primitive is therefore:

```text
source_card = an exact source-backed character range with a writing role
```

## Canonical table

The canonical table is now:

```text
source_cards
```

Existing rows were migrated from:

```text
claims
```

to:

```text
source_cards
```

## Backward compatibility

A read-only compatibility view remains:

```sql
CREATE VIEW claims AS SELECT * FROM source_cards;
```

This lets old dashboards or quick SQL queries continue to read from `claims`, but new code should use `source_cards`.

The Python CLI still has some old command names such as `mark-claim`, `context CLAIM_ID`, and `audit-draft` because changing every public term at once would create churn. Internally, the source-card model is now the canonical concept.

## Stable IDs

For now, source card IDs still use the historical `CLM-...` format:

```text
CLM-CuadrosCasanova_2022-0031
```

This preserves compatibility with existing chapter briefs, static exports, and draft audit markers.

Future versions may introduce `CARD-...` IDs, but this should be done with an alias table rather than breaking existing documents.

## Writing-time role

Each source card has a computed `card_role`:

```text
result_claim
method_card
definition_card
theory_card
background_card
limitation_card
policy_design_card
contradiction_card
unknown_card
```

This is the field that should guide whether a card can be used as direct evidence in a thesis paragraph.

## Retrieval examples

Only result claims:

```bash
python rh2.py retrieve "monitoring targets" \
  --source-id CuadrosCasanova_2022 \
  --card-role result_claim
```

Policy design cards:

```bash
python rh2.py retrieve "monitoring targets" \
  --source-id CuadrosCasanova_2022 \
  --card-role policy_design_card
```

Paper brief with result claims only:

```bash
python rh2.py paper-brief "CAP monitoring" \
  --paper-limit 1 \
  --card-role result_claim
```

## Migration status

After migration, the local DB contains:

```text
source_cards: 104
claims view:  104
```

The static export still includes a `claims` key for UI compatibility and also includes a `source_cards` alias.

## Design rule

Use this wording going forward:

```text
source card = exact source range in the database
result claim = source card with card_role=result_claim
context card = method/background/definition/theory/limitation card
```

This keeps the harness academically honest while preserving all useful context needed for writing and verification.
