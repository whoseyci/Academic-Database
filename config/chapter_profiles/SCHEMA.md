# Chapter Profile Schema

Chapter profiles live in `config/chapter_profiles/*.json` and define the evidence contract for a writing task.

## Minimal shape

```json
{
  "chapter_id": "eco_schemes_discussion_rq2_rq3",
  "chapter_title": "Discussion evidence brief",
  "purpose": "What the chapter section needs evidence for.",
  "research_questions": ["RQ2", "RQ3"],
  "required_topics": ["institutional trust", "contract design"],
  "avoid": ["Do not generalize EU AECM findings directly to Andalusian olive farms."],
  "allowed_statuses": ["verified", "candidate_needs_review", "needs_page_check"],
  "minimum_source_diversity": 3,
  "global_filters": {
    "source_ids": ["Canessa_2024"],
    "statuses": ["verified", "candidate_needs_review", "needs_page_check"]
  },
  "writing_contract": [
    "Every substantive sentence should map to at least one claim_id."
  ],
  "sections": [
    {
      "section_id": "behavioural_drivers",
      "heading": "Behavioural and institutional drivers",
      "writing_goal": "Explain how trust, attitudes and social norms shape participation.",
      "queries": ["trust policy stability social norms participation farmers"],
      "claim_types": ["empirical finding", "theoretical claim"],
      "statuses": ["verified", "candidate_needs_review"],
      "limit": 8
    }
  ]
}
```

## Notes

- `allowed_statuses` is used as a default retrieval filter if `global_filters.statuses` is absent.
- `required_topics` are checked after retrieval. Missing topics become chapter-brief warnings.
- `avoid` is exported into the evidence packet so agents know what not to overclaim.
- Section-level `statuses`, `source_ids` and `claim_types` override or extend global filters for that section.
- Use conservative source-diversity targets. A narrow single-source profile can set `minimum_source_diversity` to `1`.
