"""Validate a PDF-to-MD converted paper directory.

Usage:
  python -m pdf_pipeline.validate output/paper-slug
  python -m pdf_pipeline.validate output/paper-slug --json
"""
from __future__ import annotations

import argparse, json, re, sys
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def check_offsets(name: str, items: list[dict[str, Any]], md_len: int, warnings: list[str], errors: list[str]) -> None:
    prev_start = -1
    for i, item in enumerate(items):
        try:
            start = int(item.get("char_start")); end = int(item.get("char_end"))
        except Exception:
            errors.append(f"{name}[{i}] missing integer char_start/char_end")
            continue
        if not (0 <= start < end <= md_len):
            errors.append(f"{name}[{i}] invalid range {start}:{end} for md length {md_len}")
        if start < prev_start:
            warnings.append(f"{name}[{i}] starts before previous item; ordering may be unstable")
        prev_start = start


def validate_converted_dir(path: Path) -> dict[str, Any]:
    warnings: list[str] = []
    errors: list[str] = []
    md_path = path / "paper.md"
    parse_path = path / "paper.parse.json"
    paper_json = path / "paper.json"
    stats_json = path / "stats.json"
    refs_json = path / "references.json"

    if not md_path.exists():
        errors.append("missing paper.md")
        md = ""
    else:
        md = md_path.read_text(encoding="utf-8", errors="ignore")
    md_len = len(md)

    if not parse_path.exists():
        errors.append("missing paper.parse.json")
        parse = {}
    else:
        try:
            parse = load_json(parse_path)
        except Exception as e:
            errors.append(f"paper.parse.json is invalid JSON: {e}")
            parse = {}

    stats = {}
    if stats_json.exists():
        try: stats = load_json(stats_json)
        except Exception as e: warnings.append(f"stats.json invalid: {e}")
    paper = {}
    if paper_json.exists():
        try: paper = load_json(paper_json)
        except Exception as e: warnings.append(f"paper.json invalid: {e}")

    pages = parse.get("pages", []) if isinstance(parse, dict) else []
    sections = parse.get("sections", []) if isinstance(parse, dict) else []
    paragraphs = parse.get("paragraphs", []) if isinstance(parse, dict) else []
    references = parse.get("references", []) if isinstance(parse, dict) else []
    citations = parse.get("citations", []) if isinstance(parse, dict) else []
    tables = parse.get("tables", []) if isinstance(parse, dict) else []
    figures = parse.get("figures", []) if isinstance(parse, dict) else []

    for name, items in [("pages", pages), ("sections", sections), ("paragraphs", paragraphs), ("references", references), ("citations", citations), ("tables", tables), ("figures", figures)]:
        if isinstance(items, list):
            check_offsets(name, items, md_len, warnings, errors)
        else:
            errors.append(f"{name} is not a list")

    if not pages:
        warnings.append("no page spans in parse map")
    if not sections:
        warnings.append("no section spans in parse map")
    if sections and not any((s.get("section_role") == "references" or re.search(r"references|bibliography", str(s.get("heading", "")), re.I)) for s in sections):
        warnings.append("no References/Bibliography section detected")
    if citations and references:
        ref_ids = {str(r.get("reference_id")) for r in references}
        missing_refs = [c for c in citations if str(c.get("reference_id")) not in ref_ids]
        if missing_refs:
            warnings.append(f"{len(missing_refs)} citation(s) point to missing reference_id")
    if references and refs_json.exists():
        try:
            refs = load_json(refs_json)
            if isinstance(refs, list) and abs(len(refs) - len(references)) > max(3, len(refs) * 0.25):
                warnings.append(f"reference count mismatch: references.json={len(refs)} parse_map={len(references)}")
        except Exception:
            pass
    if figures:
        missing_files = []
        for f in figures:
            file = f.get("file")
            if file and not (path / file).exists():
                missing_files.append(file)
        if missing_files:
            warnings.append(f"{len(missing_files)} figure file(s) referenced but missing")

    n_linked = len(citations)
    n_refs = len(references)
    structure_score = sum(bool(x) for x in [pages, sections, references]) / 3
    citation_score = min(1.0, n_linked / max(1, n_refs)) if n_refs else 0.0
    coverage = stats.get("coverage_ratio") if isinstance(stats, dict) else None
    coverage_score = 1.0 if isinstance(coverage, (int, float)) and 0.6 <= coverage <= 1.35 else 0.65 if coverage else 0.0
    penalty = min(0.6, 0.08 * len(errors) + 0.03 * len(warnings))
    overall = max(0.0, min(1.0, 0.45 * structure_score + 0.25 * citation_score + 0.20 * coverage_score + 0.10 * (1.0 if md_len > 1000 else 0.0) - penalty))

    return {
        "path": str(path),
        "ok": not errors,
        "overall_score": round(overall, 3),
        "counts": {
            "markdown_chars": md_len,
            "pages": len(pages),
            "sections": len(sections),
            "paragraphs": len(paragraphs),
            "references": len(references),
            "citations": len(citations),
            "tables": len(tables),
            "figures": len(figures),
        },
        "paper_metadata": (paper.get("metadata") if isinstance(paper, dict) else {}) or {},
        "stats": stats,
        "warnings": warnings,
        "errors": errors,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("converted_dir")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    result = validate_converted_dir(Path(args.converted_dir))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        status = "OK" if result["ok"] else "ERROR"
        print(f"{status} parse validation: {args.converted_dir}")
        print(f"overall_score: {result['overall_score']}")
        print("counts:", result["counts"])
        if result["warnings"]:
            print("warnings:")
            for w in result["warnings"]: print(f"- {w}")
        if result["errors"]:
            print("errors:")
            for e in result["errors"]: print(f"- {e}")
    if result["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
