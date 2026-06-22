# Chapter-Aware Citation Backtracking

## Idea

Citation backtracking should not only answer:

```text
Which sources does paper A cite?
```

It should answer:

```text
Which upstream sources support the evidence used in this chapter/section?
```

A cited source can play different roles depending on where it enters the thesis:

```text
Ajzen 1991 in a theory section      -> theory_source
Ajzen 1991 in a methods section     -> methods_source / framework_source
Ducos 2009 in a results discussion  -> empirical_support_source
Pe'er 2022 in policy critique       -> policy_design_source
```

## Implemented command

```bash
python rh2.py chapter-citations PROFILE_ID
```

Example:

```bash
python rh2.py chapter-citations eco_schemes_discussion_rq2_rq3 \
  --context-scope paragraph \
  --limit 10
```

Write outputs:

```bash
python rh2.py chapter-citations eco_schemes_discussion_rq2_rq3 \
  --out exports/chapter_citations_eco_schemes_discussion_rq2_rq3.md

python rh2.py chapter-citations eco_schemes_discussion_rq2_rq3 \
  --out exports/chapter_citations_eco_schemes_discussion_rq2_rq3.json
```

## What it does

1. Loads the chapter profile.
2. Retrieves the source cards selected for each chapter section.
3. For every selected source card, finds nearby citation contexts in the original paper.
4. Aggregates upstream cited sources by chapter section and writing role.
5. Flags missing upstream sources that are not yet in the local library.

## Context scopes

```text
overlap    only citations whose extracted context overlaps the source card span
paragraph  citations in the same paragraph as the source card (default)
section    citations in the same source-paper section as the source card
```

Default is `paragraph`, because a source card may not contain the citation itself but often depends on citations in the same paragraph.

## Chapter citation roles

The tool infers roles such as:

```text
methods_source
theory_source
empirical_support_source
policy_design_source
limitation_or_gap_source
contradiction_or_qualification_source
background_source
```

Role inference uses:

- chapter section heading,
- chapter section type,
- writing goal,
- source card role,
- citation function.

Section profiles can override this with:

```json
"citation_role": "methods_source"
```

or:

```json
"section_type": "methods"
```

## Why this matters

A chapter brief tells us:

```text
This thesis chapter uses source card X from paper A.
```

Chapter-aware citation backtracking tells us:

```text
Source card X depends on paper B/C/D as cited by paper A.
Paper B is a theory source in this chapter.
Paper C is an empirical-support source in this chapter.
Paper D is missing and should be imported before verification.
```

This makes the reference web chapter-specific instead of globally flat.

## Current example

For `eco_schemes_discussion_rq2_rq3`, current output is in:

```text
exports/chapter_citations_eco_schemes_discussion_rq2_rq3.md
exports/chapter_citations_eco_schemes_discussion_rq2_rq3.json
```

The current profile selects 22 source cards and identifies 66 upstream cited sources from the selected evidence.

All upstream sources are currently missing locally, which is expected because the cited papers have not yet been imported.

## Caveats

This is still V0.

Limitations:

- Citation-context extraction is regex/parser based.
- Source-card-to-citation dependency is approximate, especially with paragraph scope.
- Same citation context may support multiple nearby source cards; duplicates are deduplicated per section/context/source.
- Chapter role inference is heuristic unless `citation_role` is explicitly set in the chapter profile.
- Verification still requires importing/inspecting the cited source.

## Recommended next step

Add section-level metadata to chapter profiles:

```json
{
  "section_id": "methods_framework",
  "heading": "Theoretical and methodological framework",
  "section_type": "methods",
  "citation_role": "methods_source",
  "queries": ["Theory of Planned Behaviour survey measurement subjective norms perceived control"]
}
```

Then run:

```bash
python rh2.py chapter-citations PROFILE_ID --context-scope paragraph
```

This will classify upstream cited sources by their role in the actual chapter, rather than only by global citation frequency.
