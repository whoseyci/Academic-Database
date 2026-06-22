# Agent Protocol for Research Harness V2

This repository is not a free-form “chat with papers” system. It is a claim-ledger harness. Agents should treat it as an evidence database with explicit provenance, status, scope and review constraints.

## Core rule

Do not let the model “remember” the literature. Make it operate over claim IDs.

A substantive academic statement should be traceable to one or more `claim_id`s, or it should be explicitly labelled as interpretation, synthesis, hypothesis, or a gap.

## Required writing workflow

1. **Start from a chapter profile**
   - Run `python rh2.py chapter-brief PROFILE_ID`.
   - Use the generated brief as the evidence packet for drafting.

2. **Draft with claim IDs attached**
   - Inline form is acceptable:
     - `... participation is shaped by trust and transaction costs. <!-- claims: CLM-Canessa_2024-0020, CLM-Canessa_2024-0016 -->`
   - Or parenthetical form:
     - `[claims: CLM-Canessa_2024-0020; CLM-Canessa_2024-0016]`

3. **Expand context before central use**
   - If a claim carries a key paragraph or argument, inspect context:
     - `python rh2.py context CLAIM_ID --window 600`

4. **Audit before final use**
   - Run:
     - `python rh2.py audit-draft chapter.md`
   - Fix unknown claim IDs, unverified central claims, weak evidence grades and uncited substantive sentences.

5. **Review claims explicitly**
   - Use:
     - `python rh2.py review CLAIM_ID verified --note "Checked source and page." --actor human`
   - Do not silently treat candidate claims as verified.

6. **Record tensions instead of smoothing them away**
   - Use:
     - `python rh2.py relate CLAIM_A CLAIM_B contradicts --note "Different geography/methodology."`
   - Contradictions, qualifications and methodological incompatibilities are valuable literature-review structure.

## Status policy

- `verified`: source/evidence/page/scope checked enough for publication use.
- `candidate_needs_review`: plausible, but not final.
- `needs_page_check`: source support may be valid, but page/location is not publication-safe.
- `needs_source_check`: support needs source-level verification.
- `rejected`: do not use.
- `superseded`: replaced by a better claim; keep only for audit trail.

## Evidence grades

The harness computes conservative evidence grades at retrieval/audit time:

- `A`: verified, page-anchored, source-like claim representation.
- `B`: verified or well-anchored but not perfect.
- `C`: usable candidate/paraphrase or page-uncertain support.
- `D`: weak/unanchored candidate.
- `X`: rejected or superseded.

Use `A/B` as publication-safe candidates and `C/D` as drafting/review material.

## Agent prohibitions

An agent must not:

- invent citations or claim IDs;
- upgrade scope beyond the source geography, method, population or case;
- cite `rejected` or `superseded` claims as support;
- hide uncertainty or status warnings;
- write a final academic paragraph whose core empirical/theoretical assertions cannot be traced to claim IDs;
- use large unbounded source dumps when a claim card plus context window is sufficient.

## Useful commands

```bash
python rh2.py retrieve "trust policy stability" --fields standard
python rh2.py context CLM-Canessa_2024-0020 --window 600
python rh2.py chapter-brief eco_schemes_discussion_rq2_rq3
python rh2.py audit-draft chapter.md
python rh2.py evidence-grades --grade C
python rh2.py relate CLAIM_A CLAIM_B qualifies --note "Same construct, different methodology."
python rh2.py relations --claim-id CLAIM_A
```

## Source-range and citation-backtracking tools

The latest harness also supports source-range candidates and citation backtracking. Use these when building a reference web around a paper:

```bash
python rh2.py extract-source-ranges SOURCE_ID --dry-run
python rh2.py mark-span SOURCE_ID CHAR_START CHAR_END --json
python rh2.py extract-citations SOURCE_ID --clear
python rh2.py citation-report --source-id SOURCE_ID
python rh2.py citation-context CITCTX-SOURCE-00001 --limit 5
python rh2.py verify-citation CITCTX-SOURCE-00001 verified_accurate --note "Checked against cited source."
```

Agent rule: source-range candidates are excellent review material, but they are not automatically publication-safe. They still need status review and, ideally, draft audit before final use.

## Sprint A review loop

The review workflow is now queue-driven:

```bash
python rh2.py review-queue --status candidate_needs_review --grade C --limit 20
python rh2.py review-packet CLAIM_ID
python rh2.py review CLAIM_ID verified --label good_claim --note "Checked source/page/scope."
```

Structured review labels should be used whenever possible. Useful labels include:

```text
good_claim, excellent, too_broad, too_narrow, not_substantive,
bad_evidence, needs_split, duplicate, scope_overreach,
method_only, background_only, page_verified, source_verified,
superseded, revised
```

If a claim is too broad or inferior to a tighter source-range candidate, do not merely reject it. Prefer preserving the audit trail:

```bash
python rh2.py revise-claim CLAIM_ID --scope-note "Narrowed scope..." --label revised
python rh2.py supersede OLD_CLAIM NEW_CLAIM --note "New source-range claim is tighter."
python rh2.py duplicate DUPLICATE_CLAIM CANONICAL_CLAIM --reject-duplicate
python rh2.py split-claim BROAD_CLAIM --file split_specs.json --inherit-tags --supersede-original
```

A split spec is a JSON array of narrower claim specs. Minimal example:

```json
[
  {
    "claim": "...",
    "evidence": "...",
    "claim_type": "empirical finding",
    "scope_note": "Split from a broader claim."
  }
]
```

## Sprint B citation-location suggestions

Citation backtracking now has a deterministic first-pass locator. It does not verify correctness by itself; it ranks likely support locations in the matched/backtracked source.

```bash
python rh2.py suggest-cited-claim-location CITCTX-SOURCE-00001 --limit 10
python rh2.py suggest-cited-claim-location CITCTX-SOURCE-00001 --store --json
python rh2.py suggest-cited-claim-locations --source-id SOURCE_ID --store
python rh2.py citation-location-suggestions --context-id CITCTX-SOURCE-00001
python rh2.py verify-location CLOC-... accepted --note "Best support location."
python rh2.py accept-citation-location CLOC-... --citing-claim-id CLAIM_ID --relation-type supports
```

The ranker uses citation context text, citation function, matched source, existing source cards, available source spans, page locators such as `p.`/`pp.` when present, section priors and transparent lexical scoring. Treat statuses as candidate triage labels until a reviewer checks support direction, scope and possible overstatement.
