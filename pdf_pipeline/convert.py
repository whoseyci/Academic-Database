"""
Academic PDF -> clean markdown + structured JSON. v2 production converter.

Pipeline:
  1. pymupdf4llm extraction (per-page chunks)
  2. Per-page postprocess: soft-hyphens, BR-tables reshape
  3. Cross-page dedup of running headers/footers
  4. Junk-table removal
  5. Split body / references
  6. Parse references
  7. Link in-text citations (3 styles)
  8. Extract structured metadata (title, authors, DOI, year, journal)
  9. Write paper.md, paper.json, references.json, provenance.json, audit/stats.json
"""
from __future__ import annotations
import argparse, hashlib, json, os, re, shutil, sys, time, unicodedata
from collections import Counter
from pathlib import Path

import fitz
import pymupdf4llm

sys.path.insert(0, str(Path(__file__).parent))
from postprocess_md import (
    clean_soft_hyphens,
    normalize_unicode_punct,
    collapse_blank_lines,
    find_and_reshape_br_tables,
    unwrap_reference_tables,
    reshape_article_info_abstract_table,
    dedupe_page_headers_footers,
    extract_title,
    clean_author_line,
    postprocess_full,
    reformat_picture_text_blocks,
    strip_stray_br,
    strip_boilerplate,
    fix_pdf_artifacts,
    convert_superscripts,
)
from references_v2 import (
    split_references_section,
    parse_references,
    link_citations,
    render_references_section,
)
from tables_v2 import remove_junk_tables
from metadata import extract_metadata
from front_matter import strip_front_matter
from first_page_layout import extract_first_page_abstract
from figures import reorganize_and_describe_figures


# ---------------------------------------------------------------------------
# Page-boundary markers
# ---------------------------------------------------------------------------
# We need to preserve "page N of T" information through many regex passes
# (boilerplate strip, citation linking, table reshape, etc.). The strategy
# is to inject an invisible token at every page boundary right after
# per-page cleanup and dedupe, and only render visible separators at the
# very end. The token uses U+2063 INVISIBLE SEPARATOR (×3) which is not
# matched by any of our existing regexes and is rendered as zero-width in
# viewers. We deliberately avoid underscores in the literal payload because
# the italic-touch fixer would mangle them.
PAGE_MARKER_OPEN = "\u2063\u2063\u2063PB"  # zero-width prefix + 'PB' (page-boundary)
PAGE_MARKER_CLOSE = "\u2063\u2063\u2063"
PAGE_MARKER_RE = re.compile(
    re.escape(PAGE_MARKER_OPEN) + r"(\d+)/(\d+)" + re.escape(PAGE_MARKER_CLOSE)
)


def _strip_page_markers(text: str) -> str:
    """Drop all page-boundary tokens. Used inside the references section
    and around the abstract block, where page markers would be noise."""
    return PAGE_MARKER_RE.sub("", text)


def _render_page_markers(text: str) -> str:
    """Convert the invisible PAGE_BOUNDARY tokens to visible markdown
    separators of the form `--- *Page N of T* ---`."""
    def repl(m):
        n, t = m.group(1), m.group(2)
        return f"\n\n---\n\n*— Page {n} of {t} —*\n\n"
    out = PAGE_MARKER_RE.sub(repl, text)
    # Collapse 3+ blank lines.
    out = re.sub(r"\n{4,}", "\n\n\n", out)
    # When two page markers come back-to-back (because pymupdf4llm emitted an
    # empty page in between), drop the trailing horizontal rule of the first
    # marker so we don't get a stack of `---` lines.
    out = re.sub(
        r"(\*— Page \d+ of \d+ —\*)\s*\n+\s*---\s*\n+\s*(\*— Page \d+ of \d+ —\*)",
        r"\1\n\n\2",
        out,
    )
    # When the marker's leading `---` immediately follows an existing `---`
    # separator (e.g. the metadata block's divider), drop the duplicate rule.
    out = re.sub(r"(?m)^---\s*\n+\s*---\s*\n+(\*— Page)", r"---\n\n\1", out)
    return out


def _strip_leading_page_marker(body: str, max_pre_chars: int = 200) -> str:
    """Remove the first PAGE_BOUNDARY token if it appears within the first
    few hundred characters of the body (i.e. right after the front-matter
    strip, with little or no real content before it). Readers don't need
    a 'Page 2 of N' marker hanging at the very top of the article body —
    page 1 is the title page itself."""
    m = PAGE_MARKER_RE.search(body[:max_pre_chars])
    if m:
        return body[:m.start()] + body[m.end():]
    return body


def slugify(s, maxlen=60):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^\w\s-]", "", s).strip().lower()
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:maxlen].rstrip("-") or "paper"


# Regex for image refs in markdown
IMG_RE = re.compile(r"!\[\]\(([^)]+)\)")


def _with_tesseract_hidden(fn):
    """
    pymupdf4llm 1.27+ auto-detects tesseract on PATH and, when found,
    runs OCR on every page — which has the side-effect of NOT exporting
    embedded raster images. We want our own (downstream) tesseract for
    figure-text OCR, but pymupdf4llm itself should not see it. So we
    temporarily strip tesseract from PATH while calling pymupdf4llm.
    """
    import functools
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        old_path = os.environ.get("PATH", "")
        new_parts = [p for p in old_path.split(os.pathsep) if p]
        # Drop any PATH entries that contain a tesseract binary
        clean = []
        for p in new_parts:
            if os.path.exists(os.path.join(p, "tesseract")):
                continue
            clean.append(p)
        os.environ["PATH"] = os.pathsep.join(clean)
        try:
            return fn(*args, **kwargs)
        finally:
            os.environ["PATH"] = old_path
    return wrapper


@_with_tesseract_hidden
def run_pymupdf4llm(pdf_path, out_dir):
    """Run pymupdf4llm; extract images into figures/ subdir.
    For large PDFs (>100 pages), process in chunks to avoid OOM."""
    raw_figs = out_dir / "_raw_figs"
    raw_figs.mkdir(exist_ok=True)
    
    # Count pages first
    doc = fitz.open(pdf_path)
    n_pages = len(doc)
    doc.close()
    
    if n_pages <= 100:
        # Single-shot for small/medium PDFs
        chunks = pymupdf4llm.to_markdown(
            str(pdf_path),
            page_chunks=True,
            write_images=True,
            image_path=str(raw_figs),
            image_format="png",
            dpi=144,
            table_strategy="lines_strict",
            margins=0,
        )
        return chunks, raw_figs
    
    # Chunked mode for large PDFs (memory safety)
    print(f"      (chunked: {n_pages} pages in batches of 50)")
    all_chunks = []
    chunk_size = 50
    for start in range(0, n_pages, chunk_size):
        end = min(start + chunk_size, n_pages)
        pages_list = list(range(start, end))
        try:
            chs = pymupdf4llm.to_markdown(
                str(pdf_path),
                pages=pages_list,
                page_chunks=True,
                write_images=True,
                image_path=str(raw_figs),
                image_format="png",
                dpi=120,   # lower DPI for big docs
                table_strategy="lines_strict",
                margins=0,
            )
            all_chunks.extend(chs)
        except Exception as e:
            print(f"      chunk {start+1}-{end} failed: {e}")
    return all_chunks, raw_figs


def reorganize_figures(raw_figs, out_dir, chunks):
    """Rename and filter figures. Returns: original_filename -> new_relative_path or None."""
    figs_dir = out_dir / "figures"
    figs_dir.mkdir(exist_ok=True)
    
    mapping = {}
    figs_meta = []
    fig_idx = 0
    seen = set()
    
    for ch in chunks:
        pno = ch.get("metadata", {}).get("page", 0)
        for m in IMG_RE.finditer(ch.get("text", "")):
            fname = Path(m.group(1)).name
            if fname in seen:
                continue
            seen.add(fname)
            raw = raw_figs / fname
            if not raw.exists():
                mapping[fname] = None
                continue
            # Filter tiny images (icons, decorations)
            if raw.stat().st_size < 5 * 1024:
                mapping[fname] = None
                continue
            fig_idx += 1
            new_name = f"fig-{fig_idx:03d}.png"
            shutil.copy2(raw, figs_dir / new_name)
            mapping[fname] = f"figures/{new_name}"
            figs_meta.append({
                "id": f"fig-{fig_idx:03d}",
                "file": mapping[fname],
                "page": pno,
            })
    shutil.rmtree(raw_figs, ignore_errors=True)
    return mapping, figs_meta


_END_OF_ABSTRACT_RE = re.compile(
    r"(?im)^#{1,3}\s*\**\s*"
    r"(?:\d+\s*[\.\|]?\s*\**\s*)?"
    r"(?:Introduction|Background|Overview|"
    r"Materials\s+and\s+Methods|Methods|Results|Methodology|"
    r"Origin\s+and\s+Evolution|Literature\s+Review|"
    r"K\s*E\s*Y\s*W\s*O\s*R\s*D\s*S|KEYWORDS?|Keywords?|"
    r"Introducción)\b"
)


def _preserve_page_markers(stripped_region: str) -> str:
    """Extract any PAGE_BOUNDARY tokens from a region we're about to delete
    and return them as a string we can re-insert, so that pages don't
    disappear from the final marker stream."""
    markers = PAGE_MARKER_RE.findall(stripped_region)
    if not markers:
        return ""
    pieces = [f"{PAGE_MARKER_OPEN}{n}/{t}{PAGE_MARKER_CLOSE}" for n, t in markers]
    return "\n\n" + "\n\n".join(pieces) + "\n\n"


def _strip_garbled_abstract(body: str, clean_abstract: str) -> str:
    """
    Remove a garbled-by-pymupdf4llm abstract from the body when we already
    have a clean layout-extracted replacement.
    
    Strategy:
      1. If body contains a ``## Abstract`` heading (from a previous reshape
         or pymupdf4llm), drop everything from that heading down to the
         next section heading (Introduction, KEYWORDS, etc.).
      2. Else find the FIRST short prefix (first 6 words) of the clean
         abstract somewhere in the body; if it's within the first ~6000
         chars, walk forward to the next section heading and remove the
         whole range.

    In both cases we preserve any PDF page-boundary markers that fell
    inside the stripped region so the page count remains complete.
    """
    import re as _re
    # Case 1: existing body has an Abstract section we should drop.
    # Match both:
    #   `## Abstract`        (real heading)
    #   `**Abstract**`       (Springer chapter bold marker — usually on
    #                         its own line OR followed by the first
    #                         sentence on the same line)
    m = _re.search(
        r"(?im)^(?:##\s*\**\s*Abstract\**\s*$|\*\*\s*Abstract\s*\*\*)",
        body,
    )
    if m:
        rest = body[m.end():]
        end_m = _END_OF_ABSTRACT_RE.search(rest)
        if end_m:
            cut_to = m.end() + end_m.start()
            # If the boundary itself is a KEYWORDS-style heading, also
            # consume the keywords block (the heading + the next short
            # paragraph) since we've already injected our own clean
            # **Keywords:** line.
            boundary_line = body[m.end() + end_m.start(): m.end() + end_m.end()]
            if _re.search(r"(?i)K\s*E\s*Y\s*W\s*O\s*R\s*D\s*S|KEYWORDS?|Keywords?",
                          boundary_line):
                # Skip the heading line and the next paragraph
                after_heading = body[m.end() + end_m.end():]
                # Find end of next short paragraph (≤ 5 lines or first blank
                # line followed by another section heading / blank line)
                paragraph_end = 0
                lines = after_heading.split("\n")
                # Consume up to 6 non-empty lines until a blank or heading
                consumed = 0
                idx = 0
                non_empty = 0
                while idx < len(lines) and non_empty < 4:
                    line = lines[idx]
                    idx += 1
                    if line.strip() == "":
                        if non_empty > 0:
                            break
                        continue
                    if _re.match(r"^#{1,3}\s", line):
                        idx -= 1
                        break
                    non_empty += 1
                paragraph_end = sum(len(l) + 1 for l in lines[:idx])
                cut_to = m.end() + end_m.end() + paragraph_end
            preserved = _preserve_page_markers(body[m.start():cut_to])
            return (body[:m.start()] + preserved + body[cut_to:]).strip("\n") + "\n"
        # Fallback: drop just the heading line
        split = rest.split("\n", 1)
        tail = split[1] if len(split) > 1 else ""
        preserved = _preserve_page_markers(body[m.start():m.end()])
        return (body[:m.start()] + preserved + tail).strip("\n") + "\n"

    # Case 2: find clean-abstract prefix
    words = clean_abstract.split()
    if len(words) < 6:
        return body
    prefix = " ".join(w.strip(",.;:") for w in words[:6])
    pat = _re.compile(
        r"\b" + r"\W{1,4}".join(_re.escape(w) for w in prefix.split()) + r"\b",
        _re.IGNORECASE | _re.DOTALL,
    )
    m = pat.search(body[:8000])
    if not m:
        return body
    start = m.start()
    line_start = body.rfind("\n", 0, start)
    line_start = 0 if line_start == -1 else line_start + 1
    rest = body[m.end():]
    end_m = _END_OF_ABSTRACT_RE.search(rest)
    if not end_m:
        return body
    cut_to = m.end() + end_m.start()
    preserved = _preserve_page_markers(body[line_start:cut_to])
    return (body[:line_start] + preserved + body[cut_to:]).strip("\n") + "\n"


def _strip_redundant_keywords_line(body: str) -> str:
    """After we've injected our own clean Keywords list, drop any leftover
    body lines that are obviously a keywords echo, such as:
      - `**Keywords** kw1 · kw2 · kw3` (Springer)
      - `**Keywords:** kw1, kw2` (some MDPI)
      - `## **KEYWORDS**` followed by a short paragraph
    Only strip when the match is within the first ~6 000 chars (early in the
    document) to avoid touching real prose later.
    """
    import re as _re
    out = body
    # Pattern A: bold "Keywords" line in body (within first 6k)
    pat = _re.compile(
        r"(?im)^\s*\*\*\s*Keywords?\s*\*\*\s*[:\u2002\s]*[^\n]{0,400}\s*$"
    )
    def _strip_a(m):
        if m.start() < 6000:
            return ""
        return m.group(0)
    out = pat.sub(_strip_a, out, count=2)
    # Pattern B: '## **KEYWORDS**' followed by a short paragraph
    pat2 = _re.compile(
        r"(?im)^##\s*\**\s*K\s*E\s*Y\s*W\s*O\s*R\s*D\s*S\s*\**\s*$\n+([^\n]{1,400})\n",
    )
    def _strip_b(m):
        if m.start() < 6000:
            return ""
        return m.group(0)
    out = pat2.sub(_strip_b, out, count=2)
    return out


VISIBLE_PAGE_RE = re.compile(r"(?m)^\*— Page (\d+) of (\d+) —\*\s*$")
HEADING_RE = re.compile(r"(?m)^(#{1,6})\s+(.+?)\s*$")
REF_ANCHOR_RE = re.compile(r"<a id=\"([^\"]+)\"></a>")
MD_LINK_CITATION_RE = re.compile(r"\[([^\]]*?(?:19|20)\d{2}[a-z]?[^\]]*?)\]\(#([^)]+)\)", re.I)
IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def sha256_file(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8', errors='ignore')).hexdigest()


def role_for_heading(heading: str) -> str:
    h=heading.lower()
    if 'abstract' in h: return 'abstract'
    if 'reference' in h or 'bibliograph' in h: return 'references'
    if any(k in h for k in ['method', 'materials', 'data', 'model', 'approach']): return 'methods'
    if any(k in h for k in ['result', 'finding', 'analysis']): return 'results'
    if any(k in h for k in ['discussion', 'implication', 'conclusion', 'policy']): return 'discussion'
    if any(k in h for k in ['limitation', 'future research']): return 'limitations'
    if any(k in h for k in ['theory', 'framework', 'concept', 'classification']): return 'theory'
    if any(k in h for k in ['introduction', 'background', 'context', 'literature review']): return 'background'
    return 'body'


def page_for_offset(pages, pos):
    for pg in pages:
        if pg['char_start'] <= pos < pg['char_end']:
            return str(pg['page'])
    return ''


def build_parse_map(final_md: str, pdf_path: Path, slug: str, meta: dict, references: list, figs_meta: list, n_pages: int) -> dict:
    """Build a harness-compatible sidecar map over final Markdown offsets.

    This deliberately maps offsets into paper.md, not the raw PDF text. The Academic-Database
    harness can ingest this through `rh2.py ingest --parse-map paper.parse.json`.
    """
    # Pages from visible page markers. Page span starts at marker and ends before next marker.
    markers=list(VISIBLE_PAGE_RE.finditer(final_md))
    pages=[]
    if markers:
        for i,m in enumerate(markers):
            start=m.start(); end=markers[i+1].start() if i+1 < len(markers) else len(final_md)
            pages.append({'page': m.group(1), 'char_start': start, 'char_end': end})
    else:
        pages=[{'page': str(i+1), 'char_start': 0 if i==0 else None, 'char_end': len(final_md) if i==0 else None} for i in range(max(1,n_pages))]
        pages=[p for p in pages if p['char_start'] is not None]

    # Sections from markdown headings.
    hmatches=list(HEADING_RE.finditer(final_md))
    sections=[]
    for i,m in enumerate(hmatches):
        level=len(m.group(1)); heading=m.group(2).strip().strip('*').strip()
        start=m.start(); end=hmatches[i+1].start() if i+1 < len(hmatches) else len(final_md)
        sections.append({
            'section_id': f'sec-{i+1:04d}', 'heading': heading, 'level': level,
            'section_role': role_for_heading(heading),
            'char_start': start, 'char_end': end,
            'page_start': page_for_offset(pages, start), 'page_end': page_for_offset(pages, max(start,end-1)),
        })

    def section_id_for(pos):
        best=''
        for sec in sections:
            if sec['char_start'] <= pos < sec['char_end']:
                best=sec['section_id']; break
        return best

    # Paragraph blocks.
    paragraphs=[]
    for i,m in enumerate(re.finditer(r"\S[\s\S]*?(?=\n\s*\n|\Z)", final_md), 1):
        block=m.group(0).strip()
        if not block or HEADING_RE.match(block) or VISIBLE_PAGE_RE.match(block):
            continue
        paragraphs.append({
            'paragraph_id': f'p-{len(paragraphs)+1:05d}', 'section_id': section_id_for(m.start()),
            'char_start': m.start(), 'char_end': m.end(),
            'page_start': page_for_offset(pages, m.start()), 'page_end': page_for_offset(pages, max(m.start(),m.end()-1)),
        })

    # References: locate rendered anchors.
    ref_by_id={r.get('id'):r for r in references}
    ref_items=[]
    anchors=list(REF_ANCHOR_RE.finditer(final_md))
    for i,m in enumerate(anchors):
        rid=m.group(1); line_start=final_md.rfind('\n',0,m.start())+1
        next_line=final_md.find('\n', m.end())
        end=next_line if next_line >= 0 else (anchors[i+1].start() if i+1 < len(anchors) else len(final_md))
        r=ref_by_id.get(rid,{})
        ref_items.append({
            'reference_id': rid, 'raw_text': r.get('text') or final_md[line_start:end].strip(),
            'doi': r.get('doi',''), 'authors': r.get('authors',[]), 'year': r.get('year',''), 'title': r.get('title',''),
            'char_start': line_start, 'char_end': end,
        })

    # Linked citations.
    citations=[]
    for i,m in enumerate(MD_LINK_CITATION_RE.finditer(final_md),1):
        start=m.start(); end=m.end(); ctx_start=final_md.rfind('.',0,start)
        ctx_start=final_md.rfind('\n',0,start) if ctx_start < 0 else ctx_start+1
        ctx_end=final_md.find('.', end)
        if ctx_end < 0: ctx_end=final_md.find('\n', end)
        if ctx_end < 0: ctx_end=end
        else: ctx_end+=1
        citations.append({
            'citation_id': f'cit-{i:05d}', 'reference_id': m.group(2), 'raw_text': m.group(0),
            'label': m.group(1), 'char_start': start, 'char_end': end,
            'context_char_start': max(0,ctx_start), 'context_char_end': min(len(final_md),ctx_end),
            'section_id': section_id_for(start), 'page_start': page_for_offset(pages,start), 'page_end': page_for_offset(pages,max(start,end-1)),
        })

    # Figures from markdown image refs + metadata.
    figures=[]
    fig_meta_by_file={str(f.get('file','')):f for f in figs_meta}
    for i,m in enumerate(IMAGE_RE.finditer(final_md),1):
        file=m.group(2).lstrip('./')
        meta_f=fig_meta_by_file.get(file,{})
        figures.append({
            'figure_id': meta_f.get('id') or f'fig-{i:03d}', 'file': file, 'caption': m.group(1),
            'char_start': m.start(), 'char_end': m.end(), 'page_start': page_for_offset(pages,m.start()), 'page_end': page_for_offset(pages,m.end()-1)
        })

    # Tables: contiguous markdown pipe-table blocks.
    tables=[]
    table_re=re.compile(r"(?ms)(?:^\|.*\|\s*$\n?){2,}")
    for i,m in enumerate(table_re.finditer(final_md),1):
        tables.append({'table_id': f'table-{i:03d}', 'char_start': m.start(), 'char_end': m.end(), 'page_start': page_for_offset(pages,m.start()), 'page_end': page_for_offset(pages,m.end()-1), 'caption': ''})

    return {
        'parser': {'name': 'PDF-to-MD pipeline_v2', 'version': '2', 'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())},
        'source': {'pdf': pdf_path.name, 'pdf_sha256': sha256_file(pdf_path), 'markdown_sha256': sha256_text(final_md), 'slug': slug, **(meta or {})},
        'pages': pages, 'sections': sections, 'paragraphs': paragraphs,
        'tables': tables, 'figures': figures, 'references': ref_items, 'citations': citations,
    }


def cleanup_page_markdown(md, figs_remap):
    """Per-page cleanup: soft-hyphens, unicode punct normalization, image-path rewriting.

    Image references in ``md`` use pymupdf4llm's original filenames. We
    translate them via ``figs_remap``, whose values may be:
      - ``None``   → the image was filtered out as junk; drop the ref
      - ``"path"`` → just the new path (legacy, no alt-text)
      - ``"path|alt text"`` → new path + alt-text (from `figures.py`)
    """
    md = clean_soft_hyphens(md)
    md = normalize_unicode_punct(md)
    # IMPORTANT: rewrite image refs BEFORE fix_pdf_artifacts. The artifact
    # fixer's italic-touch regex inserts spaces inside `_` characters
    # ("Frontiers_in_Soil_Science" → "Frontiers_in _Soil _Science"), which
    # mangles raw image paths and then the IMG_RE substitution misses
    # them. By doing the substitution first we lock in clean paths.
    def repl(m):
        fname = Path(m.group(1)).name
        new = figs_remap.get(fname)
        if not new:
            return ""
        if "|" in new:
            path, alt = new.split("|", 1)
            return f"![{alt}](./{path})"
        return f"![](./{new})"
    md = IMG_RE.sub(repl, md)
    # Apply PDF artifact fixes (ligature drops, italic-run mergers, known
    # diacritic words) at the per-page level so that the subsequent
    # header/footer dedup sees consistent running headers across pages.
    md = fix_pdf_artifacts(md)
    return md


def convert_pdf(pdf_path, out_root, force=False):
    slug = slugify(pdf_path.stem)
    out_dir = out_root / slug
    marker = out_dir / "_done"
    if marker.exists() and not force:
        return {"slug": slug, "skipped": True}
    
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    
    t0 = time.time()
    print(f"   -> {pdf_path.name[:75]}")
    
    # Raw text baseline for coverage
    doc = fitz.open(pdf_path)
    n_pages = len(doc)
    raw_text = "\n\n".join(doc[p].get_text("text") for p in range(n_pages))
    (out_dir / "raw_text.txt").write_text(raw_text, encoding="utf-8")
    doc.close()
    
    # Stage 1: pymupdf4llm
    chunks, raw_figs = run_pymupdf4llm(pdf_path, out_dir)
    
    # Stage 2: figures
    figs_remap, figs_meta = reorganize_and_describe_figures(raw_figs, out_dir, chunks)
    
    # Stage 3: per-page cleanup
    per_page = []
    for i, ch in enumerate(chunks):
        # pymupdf4llm emits `page_number` (1-indexed) in the chunk metadata;
        # fall back to the chunk index.
        md = ch.get("metadata", {})
        pno = md.get("page_number") or md.get("page") or (i + 1)
        cleaned = cleanup_page_markdown(ch.get("text", ""), figs_remap)
        per_page.append({"page": pno, "text": cleaned})
    
    # Stage 4: dedupe headers/footers across pages
    page_texts = [p["text"] for p in per_page]
    page_texts = dedupe_page_headers_footers(page_texts)
    for i, p in enumerate(per_page):
        p["text"] = page_texts[i]
    
    # Stage 5: join + global postprocess (picture blocks, BR-tables, blank lines).
    # We inject invisible PAGE_MARKER tokens BEFORE each page so they survive
    # all subsequent transformations; they are rendered as visible
    # `*— Page N of T —*` separators at the very end. We emit one marker per
    # PDF page (including page 1), and the leading-marker strip later removes
    # the page-1 marker if it lands right under the front matter.
    n_pdf_pages = n_pages
    page_join_parts = []
    for i, p in enumerate(per_page):
        pno = p["page"]
        page_join_parts.append(
            f"\n\n{PAGE_MARKER_OPEN}{pno}/{n_pdf_pages}{PAGE_MARKER_CLOSE}\n\n"
        )
        page_join_parts.append(p["text"])
    full = "".join(page_join_parts)
    full = reformat_picture_text_blocks(full)
    # Reshape Elsevier-style "ARTICLE INFO | ABSTRACT" 2-col table BEFORE
    # the generic BR-table reshaper would mangle it. This emits clean
    # `## Abstract` heading + keywords list.
    full = reshape_article_info_abstract_table(full)
    full = find_and_reshape_br_tables(full)
    full = strip_stray_br(full)
    # Unwrap reference tables BEFORE junk-table removal — otherwise the
    # heuristic kills the bibliography (it contains DOIs/URLs/MDPI strings).
    full = unwrap_reference_tables(full)
    full = remove_junk_tables(full)
    full = fix_pdf_artifacts(full)
    # NOTE: We do NOT call convert_superscripts here because it would mangle
    # citation patterns like (OECD, 2017[25]) -> (OECD, 2017²⁵), which then
    # don't match our citation regex. Superscripts are converted AFTER linking.
    # keep banner content for metadata extraction
    full_for_meta = full
    full = strip_boilerplate(full)
    full = collapse_blank_lines(full)
    
    # Stage 6: split body/refs
    body, ref_md = split_references_section(full)
    # We KEEP the page markers in ref_md so that
    # `render_references_section` can interleave them between
    # bibliography entries — that way page boundaries are visible across
    # the whole document, not just the body.
    # Parsing ignores them naturally (no marker matches our reference regex).
    references = parse_references(ref_md)
    
    # Stage 7: extract metadata from the UNCLEANED full (banner info intact)
    meta = extract_metadata(full_for_meta)
    
    # Stage 7b: strip redundant front matter (banner, dup title/authors, Received line)
    body = strip_front_matter(body, known_title=meta.get("title"))
    
    # Stage 7c: layout-aware abstract extraction (page 1 only).
    # Pymupdf4llm's flatten of 2-column ARTICLE INFO / ABSTRACT panels
    # interleaves keywords inside the abstract, drops ligatures, and
    # generally makes the abstract unreadable. We re-extract from the PDF
    # using pymupdf's layout-preserving block mode, get a clean abstract +
    # keywords list, and splice them in. The garbled original is removed
    # from `body` by `_strip_garbled_abstract`.
    abstract_block = extract_first_page_abstract(str(pdf_path))
    if abstract_block and abstract_block.abstract:
        body = _strip_garbled_abstract(body, abstract_block.abstract)
        if abstract_block.keywords:
            body = _strip_redundant_keywords_line(body)
    
    # Stage 8: link citations in cleaned body
    body_linked = link_citations(body, references)
    # NOW convert math superscripts (after citation linking to avoid mangling cites)
    body_linked = convert_superscripts(body_linked)
    # Lift the page-1 marker out of the body so we can render it at the
    # very top of the document, above the title. Page 1 of the PDF is the
    # title page, so it makes sense for the "Page 1 of N" indicator to
    # sit above the title — symmetric with the other page markers, which
    # all introduce the page that follows.
    page1_marker = ""
    pages_total = n_pages
    m_p1 = re.search(re.escape(PAGE_MARKER_OPEN) + r"1/\d+" + re.escape(PAGE_MARKER_CLOSE),
                     body_linked)
    if m_p1:
        page1_marker = body_linked[m_p1.start():m_p1.end()]
        body_linked = (body_linked[:m_p1.start()] + body_linked[m_p1.end():]).lstrip()
    else:
        # If no page-1 marker survived in body_linked, synthesise one so the
        # title still gets a "Page 1 of N" header.
        page1_marker = f"{PAGE_MARKER_OPEN}1/{pages_total}{PAGE_MARKER_CLOSE}"
    # Render visible page separators for the body (page 2..N)
    body_linked = _render_page_markers(body_linked)
    
    # Assemble final markdown with a clean title at top + metadata block
    md_parts = []
    # Page-1 marker goes at the very top — above the title. We render it
    # without the leading `---` rule since there's nothing above it.
    m_pg1 = PAGE_MARKER_RE.search(page1_marker)
    if m_pg1:
        md_parts.append(f"*— Page {m_pg1.group(1)} of {m_pg1.group(2)} —*\n")
    if meta["title"]:
        md_parts.append(f"# {meta['title']}\n")
    if meta["authors"]:
        md_parts.append(f"*{', '.join(meta['authors'])}*\n")
    info_bits = []
    if meta["year"]:
        info_bits.append(str(meta["year"]))
    if meta["journal"]:
        info_bits.append(meta["journal"])
    if meta["doi"]:
        info_bits.append(f"DOI: [{meta['doi']}](https://doi.org/{meta['doi']})")
    if info_bits:
        md_parts.append(" · ".join(info_bits) + "\n\n---\n")
    if abstract_block and abstract_block.abstract:
        md_parts.append("## Abstract\n")
        md_parts.append(abstract_block.abstract + "\n")
        if abstract_block.keywords:
            md_parts.append("**Keywords:** " + ", ".join(abstract_block.keywords) + "\n")
    md_parts.append(body_linked)
    # Render the bibliography, interleaving page-boundary markers at the
    # positions where they fell in the raw ref_md.
    refs_rendered = render_references_section(
        references, ref_md=ref_md, page_marker_re=PAGE_MARKER_RE
    )
    # Convert the invisible page tokens in the rendered refs to the same
    # visible separators used in the body.
    refs_rendered = _render_page_markers(refs_rendered)
    md_parts.append(refs_rendered)
    final_md = "\n".join(md_parts)
    final_md = collapse_blank_lines(final_md)
    # Squash adjacent horizontal rules — happens when the metadata block's
    # `---` divider is followed immediately by a page-1 marker that opens
    # with its own `---`.
    final_md = re.sub(r"(?m)^---\s*\n+\s*---", "---", final_md)
    # Defensive: strip any unrendered page-marker tokens (e.g. leaked into
    # references metadata, where we don't want them shown raw).
    final_md = _strip_page_markers(final_md)
    
    # Write outputs
    (out_dir / "paper.md").write_text(final_md, encoding="utf-8")
    parse_map = build_parse_map(final_md, pdf_path, slug, meta, references, figs_meta, n_pages)
    (out_dir / "paper.parse.json").write_text(json.dumps(parse_map, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "paper.json").write_text(json.dumps({
        "source_pdf": pdf_path.name,
        "slug": slug,
        "metadata": meta,
        "n_pages": n_pages,
        "references": references,
        "figures": figs_meta,
        "per_page_text": per_page,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "references.json").write_text(
        json.dumps(references, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "provenance.json").write_text(json.dumps({
        "source_pdf": pdf_path.name,
        "n_pages": n_pages,
        "pages": [{"page": p["page"], "chars": len(p["text"])} for p in per_page],
    }, indent=2), encoding="utf-8")
    
    # Stats + confidence
    md_words = len(final_md.split())
    raw_words = len(raw_text.split())
    coverage = round(md_words / max(raw_words, 1), 3)
    structure = sum([
        bool(meta["title"]),
        bool(meta["doi"]),
        bool(references),
        bool(figs_meta),
        bool(meta["authors"]),
    ])
    # Linked-citation count
    n_linked = len(re.findall(r"\]\(#ref-\d+\)", body_linked))
    
    # Confidence: structure-first, coverage as tiebreaker.
    # Many papers have multi-language abstracts/translations that legitimately
    # depress coverage but the English markdown is fine.
    if structure >= 4 and 0.6 <= coverage <= 1.3:
        confidence = "high"
    elif structure >= 3 and 0.5 <= coverage <= 1.5:
        confidence = "medium"
    elif structure >= 2:
        confidence = "medium"
    else:
        confidence = "low"
    
    stats = {
        "n_pages": n_pages,
        "conversion_seconds": round(time.time() - t0, 1),
        "raw_word_count": raw_words,
        "md_word_count": md_words,
        "coverage_ratio": coverage,
        "title": meta["title"],
        "year": meta["year"],
        "doi": meta["doi"],
        "journal": meta["journal"],
        "n_authors": len(meta["authors"]),
        "n_figures": len(figs_meta),
        "n_references": len(references),
        "n_citations_linked": n_linked,
        "n_parse_sections": len(parse_map.get("sections", [])),
        "n_parse_paragraphs": len(parse_map.get("paragraphs", [])),
        "n_parse_tables": len(parse_map.get("tables", [])),
        "structure_score": structure,
        "confidence": confidence,
    }
    (out_dir / "stats.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
    marker.write_text("done\n")
    
    print(f"      ok {n_pages}p, {len(figs_meta)} figs, {len(references)} refs, "
          f"{n_linked} cites linked, cov={coverage}, conf={confidence} "
          f"({stats['conversion_seconds']}s)")
    return {"slug": slug, "stats": stats, "skipped": False}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+", help="PDF paths")
    ap.add_argument("--out", default="/home/user/output_v2")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    out_root = Path(args.out)
    out_root.mkdir(exist_ok=True)
    for p in args.pdfs:
        try:
            convert_pdf(Path(p), out_root, force=args.force)
        except Exception as e:
            import traceback
            print(f"   FAIL on {p}: {e}")
            traceback.print_exc()


if __name__ == "__main__":
    main()
