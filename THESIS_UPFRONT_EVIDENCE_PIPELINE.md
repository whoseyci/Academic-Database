# How the Harness Should Provide Upfront Thesis Information to an LLM

## Core idea

The harness should not ask an LLM to "write a chapter from the literature" in a vague way.

Instead, the harness should compile a **chapter evidence brief** before writing begins.

```text
thesis profile
+ chapter profile
+ verified/candidate claim ledger
+ retrieval/ranking
+ source context links
= chapter evidence brief
```

Then the LLM sees a bounded, structured, source-backed packet rather than the whole literature database.

---

## The writing problem we are solving

Academic writing requires more than retrieving semantically similar passages. A chapter needs:

1. a purpose,
2. an argument structure,
3. definitions,
4. core empirical evidence,
5. theory links,
6. methodological context,
7. contradictions and limitations,
8. source scope warnings,
9. citation-ready evidence anchors,
10. a way to inspect the source text when needed.

So the harness needs to produce **writing context**, not just search results.

---

## Required objects

## 1. Thesis/project profile

The thesis profile defines the overall research problem.

Example fields:

```json
{
  "project_title": "Understanding Farmer Attitudes Toward CAP Eco-Schemes...",
  "discipline": ["agricultural policy", "rural sociology", "environmental governance"],
  "geography": ["Andalusia", "Spain", "European Union"],
  "methodology": ["survey", "mixed methods"],
  "research_questions": [
    {"id": "RQ1", "text": "...", "keywords": ["attitudes", "opinions", "typologies"]},
    {"id": "RQ2", "text": "...", "keywords": ["trust", "social norms", "administrative burden"]},
    {"id": "RQ3", "text": "...", "keywords": ["design", "flexibility", "payments"]}
  ],
  "constructs": ["institutional trust", "subjective norms", "perceived behavioural control", "risk perception"],
  "scope_rules": [
    "European AECM evidence can inform but not prove Andalusian olive-farmer behaviour.",
    "Survey evidence from convenience samples should not be framed as representative."
  ]
}
```

## 2. Chapter profile

A chapter profile converts the general thesis into a specific writing task.

Example:

```json
{
  "chapter_id": "discussion_rq2_rq3",
  "chapter_title": "Discussion of behavioural drivers and eco-scheme design",
  "purpose": "Compare thesis survey findings with literature on AECM participation and derive design implications.",
  "sections": [
    {
      "heading": "Behavioural drivers",
      "queries": ["trust social norms perceived behavioural control AECM participation"],
      "claim_types": ["empirical finding", "theoretical claim"],
      "limit": 10
    },
    {
      "heading": "Contract design and flexibility",
      "queries": ["contract design flexibility bureaucracy payments opportunity costs participation"],
      "claim_types": ["empirical finding", "policy implication"],
      "limit": 10
    }
  ]
}
```

## 3. Claims

A claim should be an auditable unit of evidence.

Fields needed for writing:

```text
claim_id
claim
claim_representation
source_id
citation_hint
page
verification_status
claim_type
evidence quote
source span
scope note
constructs/RQ tags
```

Important: V2 allows claims to be exact source sentences. This is often better than forcing a model rewrite.

## 4. Source context

The chapter brief should not include full papers. It should include claim cards and deep-dive commands:

```bash
python rh2.py context CLM-Canessa_2024-0020 --window 600
```

This gives the LLM context only when needed.

---

## Chapter evidence brief structure

A chapter brief should contain:

```text
1. Chapter title and purpose
2. Writing contract / rules
3. Coverage summary
4. Warnings
5. Section-by-section claim groups
6. Deep-dive commands
7. Evidence gaps
8. Suggested argument skeleton
```

Current V2 command:

```bash
python rh2.py chapter-brief eco_schemes_discussion_rq2_rq3
```

Outputs:

```text
exports/chapter_brief_eco_schemes_discussion_rq2_rq3.json
exports/chapter_brief_eco_schemes_discussion_rq2_rq3.md
```

---

## What the LLM should receive

The LLM should receive something like this:

```text
You are drafting Chapter 6 Discussion.
Use only the claims in this packet unless you retrieve more.
Every major statement must map to a claim_id.
Candidate claims must be flagged for review.
Do not generalize beyond source scope.
If a claim is central, call context before using it.
```

Then the packet provides grouped claims:

```text
Section: Behavioural drivers
- CLM-Canessa_2024-0020: Trust and policy stability were positively associated...
- CLM-Canessa_2024-0022: Neighbouring farmers' opinions showed positive effects...
- CLM-Canessa_2024-0021: Previous participation predicts later participation...
```

The LLM can write from these cards and cite claim IDs internally.

---

## Ideal writing workflow

## Step 1: Build/update project profile

Define RQs, constructs, geography, discipline, and scope rules.

## Step 2: Ingest sources

Markdown input only for now:

```bash
python rh2.py ingest paper.md --source-id Source_2024 ...
```

## Step 3: Mark/import claims

Preferred model workflow:

```text
LLM selects exact source sentence
→ calls mark-claim
→ harness finds source/page/span
→ claim enters ledger
```

No unnecessary rewriting.

## Step 4: Review high-value claims

Before publication-safe use:

```bash
python rh2.py review CLM-... verified --note "checked against source"
```

## Step 5: Build chapter brief

```bash
python rh2.py chapter-brief chapter_profile_id
```

## Step 6: Draft with claim IDs

The LLM drafts the chapter with hidden or visible claim IDs, e.g.:

```text
Institutional trust appears to be a recurring determinant of AECM participation [CLM-Canessa_2024-0020].
```

## Step 7: Audit draft

Future command:

```bash
python rh2.py audit-draft chapter6.md
```

Audit should flag:

- uncited substantive statements,
- claims without verified status,
- missing page/source anchors,
- overgeneralization beyond source scope,
- unsupported synthesis leaps.

---

## The LLM should not do these things

The LLM should not:

- browse all sources freely,
- invent literature synthesis without claim IDs,
- cite candidate claims as final evidence,
- ignore source geography/methodology/scope,
- smooth contradictions into false consensus,
- write from semantic similarity alone.

---

## Evidence ranking for chapter briefs

A good chapter brief should rank claims using:

1. query match,
2. research-question tag,
3. construct tag,
4. claim type fit,
5. source quality,
6. verification status,
7. page/source anchor quality,
8. methodology match,
9. geographic match,
10. recency if relevant,
11. source diversity constraints.

A claim that is semantically relevant but geographically/methodologically weak should still appear, but with a scope warning.

---

## The critical difference from NotebookLM

NotebookLM gives the model source access.

This harness gives the model **argument-ready evidence objects**:

```text
claim card
+ evidence quote
+ page/span
+ verification status
+ scope note
+ chapter role
```

That is the value.

---

## Next features needed

1. `audit-draft` command.
2. Batch `mark-claims` import from model output.
3. Claim relation graph: supports/contradicts/qualifies.
4. Chapter coverage dashboard.
5. Verified-only writing mode.
6. Source diversity and scope-fit scoring.
7. UI where a human can promote claims from candidate to verified.

---

## Bottom line

For a thesis, the harness should not try to automatically write from papers.

It should first compile a controlled evidence environment:

```text
chapter profile
→ relevant claim cards
→ warnings/gaps
→ deep-dive source commands
→ writing contract
```

Then the LLM can draft a chapter that is much more likely to be scientifically grounded, auditable, and honestly scoped.
