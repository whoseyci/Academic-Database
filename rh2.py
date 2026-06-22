#!/usr/bin/env python3
"""Research Harness V2: SQLite-first, source-span-first claim ledger.

V2's design bias:
- canonical source text is stored once as compressed gzip blobs;
- pages/paragraphs/evidence are spans over that canonical text;
- claims may be exact source sentences (`claim_representation=source_quote`);
- LLM-facing tools retrieve compact claim cards first, then expand source context on demand;
- chapter briefs assemble upfront evidence packets for writing.
"""
from __future__ import annotations

import argparse, collections, csv, gzip, hashlib, json, re, shutil, sqlite3, sys, textwrap, uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "harness_v2.db"
BLOBS = ROOT / "blobs"
EXPORTS = ROOT / "exports"
REPORTS = ROOT / "reports"
CHAPTER_PROFILES = ROOT / "config" / "chapter_profiles"
PAPER_INDEX = ROOT / "paper_index.pkl"

CLAIM_TYPES = {
    "empirical finding", "theoretical claim", "methodological claim", "definition",
    "policy implication", "limitation", "background", "contradiction", "unknown"
}
STATUSES = {"verified", "rejected", "candidate_needs_review", "needs_page_check", "needs_source_check", "superseded"}
EVIDENCE_GRADES = {"A", "B", "C", "D", "X"}
RELATION_TYPES = {"supports", "contradicts", "qualifies", "same_concept", "methodologically_incompatible", "stronger_than", "weaker_than", "supersedes"}
CARD_ROLES = {"result_claim", "interpretive_claim", "policy_design_card", "method_card", "definition_card", "theory_card", "background_card", "limitation_card", "contradiction_card", "unknown_card"}

PAGE_MARKER_RE = re.compile(
    r"(?im)^[ \t]*(?:"
    r"<!--[ \t]*page[ \t]*[:=#-]?[ \t]*(\d{1,5})[ \t]*-->"
    r"|#{1,6}[ \t]*page[ \t]*[:=#-]?[ \t]*(\d{1,5})[ \t]*$"
    r"|\[+[ \t]*page[ \t]*[:=#-]?[ \t]*(\d{1,5})[ \t]*\]+[ \t]*$"
    r"|---[ \t]*page[ \t]*[:=#-]?[ \t]*(\d{1,5})[ \t]*---[ \t]*$"
    r"|page[ \t]*[:=#-]?[ \t]*(\d{1,5})[ \t]*$"
    r"|[ \t*_—–-]*page[ \t]*[:=#-]?[ \t]*(\d{1,5})(?:[ \t]*of[ \t]*\d{1,5})?[ \t*_—–-]*$"
    r")[ \t]*$"
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def ensure_dirs() -> None:
    for p in [BLOBS, EXPORTS, REPORTS, CHAPTER_PROFILES]:
        p.mkdir(parents=True, exist_ok=True)


def db() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA mmap_size=268435456")
    except sqlite3.OperationalError:
        pass
    return conn


def ensure_column(cur: sqlite3.Cursor, table: str, column: str, definition: str) -> None:
    cols = {r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def short(s: Any, n: int = 260) -> str:
    return textwrap.shorten(str(s or "").replace("\n", " "), width=n, placeholder=" [...]")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def sha1_short(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", s.strip())
    return s.strip("_")[:100] or str(uuid.uuid4())[:8]


def split_list(s: Any) -> list[str]:
    if s is None:
        return []
    if isinstance(s, list):
        return [str(x).strip() for x in s if str(x).strip()]
    return [x.strip() for x in re.split(r"[;,|]", str(s)) if x.strip()]


def print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def parse_char_range(value: str) -> tuple[int, int]:
    m = re.match(r"^\s*(\d+)\s*[:-]\s*(\d+)\s*$", str(value or ""))
    if not m:
        raise SystemExit("Range must look like START:END or START-END")
    start, end = int(m.group(1)), int(m.group(2))
    if end <= start:
        raise SystemExit("Range end must be greater than start")
    return start, end


def source_range_handle(source_id: str, start: int, end: int) -> str:
    return f"SOURCE_RANGE[{source_id}:{int(start)}-{int(end)}]"


def claim_handle(claim_id: str) -> str:
    return f"CLAIM[{claim_id}]"


def citation_context_handle(context_id: str) -> str:
    return f"CITATION_CONTEXT[{context_id}]"


def line_offsets(text: str) -> list[int]:
    offsets=[]; pos=0
    for line in text.splitlines(True):
        offsets.append(pos); pos += len(line)
    return offsets or [0]


def line_no(offsets: list[int], pos: int) -> int:
    lo, hi = 0, len(offsets)-1
    while lo <= hi:
        mid=(lo+hi)//2
        if offsets[mid] <= pos: lo=mid+1
        else: hi=mid-1
    return max(1, hi+1)


def init_db(quiet: bool=False) -> None:
    conn=db(); cur=conn.cursor()
    # One-time migration: the canonical table is now source_cards.
    # Keep a read-only compatibility view named `claims` for older queries/UI language.
    existing_tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    existing_views = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='view'").fetchall()}
    if "claims" in existing_tables and "source_cards" not in existing_tables:
        cur.execute("ALTER TABLE claims RENAME TO source_cards")
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS sources (
        source_id TEXT PRIMARY KEY,
        title TEXT, authors TEXT, year TEXT, doi TEXT, source_type TEXT,
        disciplines TEXT, geography TEXT, methodology TEXT, theory TEXT,
        quality TEXT, notes TEXT,
        blob_path TEXT, source_hash TEXT,
        char_count INTEGER, line_count INTEGER,
        created_at TEXT, updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS spans (
        span_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        ref_id TEXT,
        char_start INTEGER, char_end INTEGER,
        line_start INTEGER, line_end INTEGER,
        page_start TEXT, page_end TEXT,
        heading TEXT,
        text_hash TEXT,
        token_count INTEGER,
        created_at TEXT,
        FOREIGN KEY(source_id) REFERENCES sources(source_id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS source_cards (
        claim_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        claim TEXT NOT NULL,
        evidence TEXT NOT NULL,
        claim_representation TEXT,
        claim_type TEXT,
        page TEXT,
        page_status TEXT,
        verification_status TEXT,
        confidence TEXT,
        scope_note TEXT,
        source_span_id TEXT,
        char_start INTEGER, char_end INTEGER,
        line_start INTEGER, line_end INTEGER,
        source_hash TEXT,
        extraction_mode TEXT,
        created_at TEXT, updated_at TEXT,
        FOREIGN KEY(source_id) REFERENCES sources(source_id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS claim_tags (
        claim_id TEXT,
        tag_type TEXT,
        tag TEXT,
        PRIMARY KEY(claim_id, tag_type, tag),
        FOREIGN KEY(claim_id) REFERENCES source_cards(claim_id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS claim_relations (
        relation_id TEXT PRIMARY KEY,
        claim_a TEXT, claim_b TEXT,
        relation_type TEXT,
        note TEXT,
        status TEXT,
        created_at TEXT,
        FOREIGN KEY(claim_a) REFERENCES source_cards(claim_id) ON DELETE CASCADE,
        FOREIGN KEY(claim_b) REFERENCES source_cards(claim_id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS source_references (
        reference_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        reference_anchor TEXT,
        raw_text TEXT,
        author_key TEXT,
        year TEXT,
        title TEXT,
        doi TEXT,
        canonical_source_id TEXT,
        matched_source_id TEXT,
        status TEXT,
        created_at TEXT,
        FOREIGN KEY(source_id) REFERENCES sources(source_id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS citation_contexts (
        context_id TEXT PRIMARY KEY,
        citing_source_id TEXT NOT NULL,
        reference_id TEXT,
        reference_anchor TEXT,
        canonical_source_id TEXT,
        cited_author_key TEXT,
        cited_year TEXT,
        citation_text TEXT,
        char_start INTEGER,
        char_end INTEGER,
        line_start INTEGER,
        line_end INTEGER,
        context_text TEXT,
        citation_function TEXT,
        matched_source_id TEXT,
        verification_status TEXT,
        verification_note TEXT,
        created_at TEXT,
        updated_at TEXT,
        FOREIGN KEY(citing_source_id) REFERENCES sources(source_id) ON DELETE CASCADE,
        FOREIGN KEY(reference_id) REFERENCES source_references(reference_id) ON DELETE SET NULL
    );
    CREATE TABLE IF NOT EXISTS review_events (
        event_id TEXT PRIMARY KEY,
        claim_id TEXT,
        from_status TEXT,
        to_status TEXT,
        note TEXT,
        actor TEXT,
        created_at TEXT,
        FOREIGN KEY(claim_id) REFERENCES source_cards(claim_id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS query_cache (
        query_hash TEXT PRIMARY KEY,
        query TEXT,
        filters_json TEXT,
        result_json TEXT,
        created_at TEXT
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS claims_fts USING fts5(claim_id UNINDEXED, claim, evidence, tags);
    CREATE INDEX IF NOT EXISTS idx_claims_source ON source_cards(source_id);
    CREATE INDEX IF NOT EXISTS idx_claims_status ON source_cards(verification_status);
    CREATE INDEX IF NOT EXISTS idx_claims_type ON source_cards(claim_type);
    CREATE INDEX IF NOT EXISTS idx_claims_page ON source_cards(source_id, page);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_source_span_unique ON source_cards(source_id, char_start, char_end);
    CREATE INDEX IF NOT EXISTS idx_spans_source_kind ON spans(source_id, kind);
    CREATE INDEX IF NOT EXISTS idx_spans_offsets ON spans(source_id, char_start, char_end);
    CREATE INDEX IF NOT EXISTS idx_tags_type_tag ON claim_tags(tag_type, tag);
    CREATE INDEX IF NOT EXISTS idx_refs_source ON source_references(source_id);
    CREATE INDEX IF NOT EXISTS idx_refs_key_year ON source_references(author_key, year);
    CREATE INDEX IF NOT EXISTS idx_citctx_source ON citation_contexts(citing_source_id);
    CREATE INDEX IF NOT EXISTS idx_citctx_match ON citation_contexts(matched_source_id);
    CREATE INDEX IF NOT EXISTS idx_citctx_status ON citation_contexts(verification_status);
    """)
    # Lightweight migrations for existing V2 databases.
    ensure_column(cur, "source_references", "reference_anchor", "TEXT")
    ensure_column(cur, "source_references", "canonical_source_id", "TEXT")
    ensure_column(cur, "citation_contexts", "reference_anchor", "TEXT")
    ensure_column(cur, "citation_contexts", "canonical_source_id", "TEXT")
    # Recreate compatibility view if possible. External dashboards can still SELECT FROM claims.
    cur.execute("DROP VIEW IF EXISTS claims")
    if "claims" not in {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}:
        cur.execute("CREATE VIEW IF NOT EXISTS claims AS SELECT * FROM source_cards")
    conn.commit(); conn.close()
    if not quiet:
        print(f"Initialized V2 DB: {DB_PATH}")


def read_source_text(source_id: str) -> str:
    conn=db(); row=conn.execute("SELECT blob_path FROM sources WHERE source_id=?", (source_id,)).fetchone(); conn.close()
    if not row:
        raise SystemExit(f"Unknown source_id: {source_id}")
    path = ROOT / row["blob_path"]
    if not path.exists():
        raise SystemExit(
            f"Source blob is not available locally for {source_id}: {path}. "
            "Full source blobs are intentionally gitignored; re-ingest the source markdown or restore blobs/*.gz."
        )
    with gzip.open(path, "rt", encoding="utf-8", errors="ignore") as f:
        return f.read()


def write_blob(source_id: str, text: str) -> tuple[str, str]:
    source_hash=sha256_text(text)
    path=BLOBS / f"{source_id}_{source_hash[:12]}.md.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(text)
    return str(path.relative_to(ROOT)), source_hash


def marker_page_no(m: re.Match) -> str:
    return next((g for g in m.groups() if g), "")


def add_span(cur: sqlite3.Cursor, *, span_id: str, source_id: str, kind: str, ref_id: str|None,
             char_start: int|None, char_end: int|None, line_start: int|None, line_end: int|None,
             page_start: str|None="", page_end: str|None="", heading: str|None="", text: str="") -> None:
    cur.execute("""INSERT OR REPLACE INTO spans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        span_id, source_id, kind, ref_id, char_start, char_end, line_start, line_end,
        page_start or "", page_end or "", heading or "", sha1_short(text or ""),
        len(re.findall(r"\w+", text or "")), now()
    ))


def page_spans(source_id: str, text: str) -> list[dict[str, Any]]:
    offsets=line_offsets(text); matches=list(PAGE_MARKER_RE.finditer(text or "")); rows=[]
    for i,m in enumerate(matches):
        page=marker_page_no(m); start=m.end(); end=matches[i+1].start() if i+1 < len(matches) else len(text)
        page_text=text[start:end].strip()
        rows.append({"span_id": f"PAGE-{source_id}-{page}", "source_id": source_id, "kind": "page", "ref_id": page,
            "char_start": start, "char_end": end, "line_start": line_no(offsets, start), "line_end": line_no(offsets, max(start,end-1)),
            "page_start": str(page), "page_end": str(page), "heading": "", "text": page_text})
    return rows


def paragraph_spans(source_id: str, text: str) -> list[dict[str, Any]]:
    offsets=line_offsets(text); rows=[]; heading=""
    for m in re.finditer(r"\S[\s\S]*?(?=\n\s*\n|\Z)", text):
        block=m.group(0).strip()
        if not block:
            continue
        if re.match(r"^#{1,6}\s+", block):
            heading=re.sub(r"^#{1,6}\s+", "", block).strip()
            continue
        sid=f"PARA-{source_id}-{len(rows)+1:05d}"
        rows.append({"span_id": sid, "source_id": source_id, "kind": "paragraph", "ref_id": sid,
            "char_start": m.start(), "char_end": m.end(), "line_start": line_no(offsets,m.start()), "line_end": line_no(offsets,m.end()),
            "page_start": "", "page_end": "", "heading": heading, "text": block})
    return rows


def chunk_spans(source_id: str, text: str, max_chars: int=2200, overlap: int=250) -> list[dict[str, Any]]:
    # offset-preserving paragraph packing
    paras=[]
    for m in re.finditer(r"\S[\s\S]*?(?=\n\s*\n|\Z)", text):
        t=m.group(0).strip()
        if t: paras.append((m.start(), m.end(), t))
    offsets=line_offsets(text); rows=[]; idx=1; buf_start=None; buf_end=None; buf_text=""
    for ps,pe,pt in paras:
        if not buf_text:
            buf_start, buf_end, buf_text = ps, pe, text[ps:pe]
        elif len(buf_text)+2+len(pt) <= max_chars:
            buf_end=pe; buf_text=text[buf_start:buf_end]
        else:
            sid=f"CHUNK-{source_id}-{idx:04d}"
            rows.append({"span_id": sid, "source_id": source_id, "kind": "chunk", "ref_id": sid,
                "char_start": buf_start, "char_end": buf_end, "line_start": line_no(offsets,buf_start), "line_end": line_no(offsets,buf_end),
                "page_start": "", "page_end": "", "heading": "", "text": text[buf_start:buf_end]})
            idx += 1
            # keep simple: start new chunk at current paragraph (no offset-ambiguous overlap in V2 spans)
            buf_start, buf_end, buf_text = ps, pe, text[ps:pe]
    if buf_text:
        sid=f"CHUNK-{source_id}-{idx:04d}"
        rows.append({"span_id": sid, "source_id": source_id, "kind": "chunk", "ref_id": sid,
            "char_start": buf_start, "char_end": buf_end, "line_start": line_no(offsets,buf_start), "line_end": line_no(offsets,buf_end),
            "page_start": "", "page_end": "", "heading": "", "text": text[buf_start:buf_end]})
    return rows


def ingest_source(path: Path, source_id: str, meta: dict[str, Any]) -> None:
    init_db(True)
    text=path.read_text(encoding="utf-8", errors="ignore")
    blob_path, source_hash = write_blob(source_id, text)
    conn=db(); cur=conn.cursor()
    cur.execute("""INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        source_id, meta.get("title") or path.stem, meta.get("authors",""), str(meta.get("year", "")), meta.get("doi",""),
        meta.get("source_type","unknown"), meta.get("disciplines",""), meta.get("geography",""), meta.get("methodology",""),
        meta.get("theory",""), meta.get("quality","unrated"), meta.get("notes",""), blob_path, source_hash,
        len(text), text.count("\n")+1, now(), now()
    ))
    # Replace spans for this source.
    cur.execute("DELETE FROM spans WHERE source_id=?", (source_id,))
    for sp in page_spans(source_id, text) + paragraph_spans(source_id, text) + chunk_spans(source_id, text):
        add_span(cur, **sp)
    conn.commit(); conn.close()


def raw_span_from_normalized_offsets(text: str, normalized_start: int, normalized_end: int) -> tuple[int|None, int|None]:
    """Map offsets in whitespace-collapsed lowercase text back to approximate raw offsets."""
    chars=[]; mapping=[]; in_space=True
    for i,ch in enumerate(text):
        if ch.isspace():
            if not in_space:
                chars.append(" "); mapping.append(i); in_space=True
        else:
            chars.append(ch.lower()); mapping.append(i); in_space=False
    # emulate .strip(): remove leading/trailing normalized spaces and shift mapping
    while chars and chars[0] == " ":
        chars.pop(0); mapping.pop(0)
    while chars and chars[-1] == " ":
        chars.pop(); mapping.pop()
    if not mapping or normalized_start < 0 or normalized_start >= len(mapping):
        return None, None
    end_idx=min(max(normalized_end-1, normalized_start), len(mapping)-1)
    return mapping[normalized_start], mapping[end_idx] + 1


def locate_span(text: str, quote: str) -> dict[str, Any]:
    offsets=line_offsets(text)
    quote=quote.strip()
    if not quote:
        return {"found": False, "method": "no_quote"}
    pos=text.find(quote)
    if pos >= 0:
        return {"found": True, "method": "exact_raw", "char_start": pos, "char_end": pos+len(quote), "line_start": line_no(offsets,pos), "line_end": line_no(offsets,pos+len(quote))}
    # segment fallback for excerpts spanning omitted text
    segments=[seg.strip() for seg in re.split(r"\.\.\.|…|\n", quote) if len(seg.strip()) >= 40]
    for seg in sorted(segments, key=len, reverse=True)[:8]:
        pos=text.find(seg)
        if pos >= 0:
            return {"found": True, "method": "segment_raw", "char_start": pos, "char_end": pos+len(seg), "line_start": line_no(offsets,pos), "line_end": line_no(offsets,pos+len(seg)), "matched_text": seg}
    # fuzzy paragraph fallback
    import difflib
    best=(0.0, None, None)
    for m in re.finditer(r"\S[\s\S]*?(?=\n\s*\n|\Z)", text):
        block=m.group(0).strip()
        sc=difflib.SequenceMatcher(None, norm(block)[:2000], norm(quote)[:2000]).ratio()
        if sc > best[0]: best=(sc,m,block)
    if best[1] and best[0] >= 0.45:
        m=best[1]
        return {"found": True, "method": "fuzzy_paragraph", "score": round(best[0],3), "char_start": m.start(), "char_end": m.end(), "line_start": line_no(offsets,m.start()), "line_end": line_no(offsets,m.end()), "matched_text": best[2][:1200]}
    return {"found": False, "method": "not_found", "best_score": round(best[0],3)}


def page_for_char(source_id: str, char_start: int|None) -> tuple[str|None, str|None]:
    """Lazy page lookup from source_id + char offset.

    Page numbers are derived from page spans when needed. They do not need to be
    treated as primary claim data; char_start/char_end are the canonical anchor.
    """
    if char_start is None:
        return None, None
    conn=db()
    row=conn.execute("""SELECT span_id, page_start FROM spans WHERE source_id=? AND kind='page' AND char_start<=? AND char_end>=? ORDER BY char_start LIMIT 1""", (source_id,char_start,char_start)).fetchone()
    conn.close()
    return (row["page_start"], row["span_id"]) if row else (None, None)


def page_for_claim_dict(d: dict[str, Any]) -> str | None:
    """Return stored page if available, else derive it lazily from char_start."""
    stored = d.get("page")
    if stored not in [None, "", "null"]:
        return str(stored)
    page, _span = page_for_char(d.get("source_id"), d.get("char_start"))
    return page


def source_slice(source_id: str, char_start: int | None, char_end: int | None) -> str:
    if char_start is None or char_end is None:
        return ""
    try:
        text = read_source_text(source_id)
    except SystemExit:
        return ""
    try:
        start=max(0, int(char_start)); end=min(len(text), int(char_end))
    except Exception:
        return ""
    if end < start:
        return ""
    return text[start:end].strip()


def claim_exact_source_text(d: dict[str, Any]) -> str:
    return source_slice(d.get("source_id"), d.get("char_start"), d.get("char_end"))


def paragraph_bounds_around(text: str, start: int, end: int) -> tuple[int, int]:
    # Paragraph = nearest blank-line boundaries around the claim.
    left = text.rfind("\n\n", 0, start)
    right = text.find("\n\n", end)
    pstart = 0 if left < 0 else left + 2
    pend = len(text) if right < 0 else right
    return pstart, pend


def sentence_spans(text: str, base_offset: int = 0) -> list[tuple[int, int, str]]:
    """Simple offset-preserving sentence splitter for already-bounded text."""
    spans=[]
    # Split after sentence punctuation OR keep bullet-ish fragments. Good enough for context windows.
    start=0
    for m in re.finditer(r"(?<=[.!?])\s+(?=[A-Z0-9\"“])|\n(?=\s*[-*]\s+)", text):
        end=m.start()
        sent=text[start:end].strip()
        if sent:
            raw_start=base_offset + start + (len(text[start:end]) - len(text[start:end].lstrip()))
            raw_end=base_offset + end
            spans.append((raw_start, raw_end, sent))
        start=m.end()
    tail=text[start:].strip()
    if tail:
        raw_start=base_offset + start + (len(text[start:]) - len(text[start:].lstrip()))
        raw_end=base_offset + len(text)
        spans.append((raw_start, raw_end, tail))
    return spans


def sentence_aware_context(source_id: str, char_start: int, char_end: int, radius: int = 1, outside_paragraph: bool = False) -> dict[str, Any]:
    """Return context by sentence radius, normally bounded to the claim paragraph.

    radius=1 returns the claim sentence plus one sentence before and after, within
    the same paragraph. Increase radius stepwise for more local context.
    """
    text=read_source_text(source_id)
    start=int(char_start); end=int(char_end)
    pstart, pend = paragraph_bounds_around(text, start, end)
    if outside_paragraph:
        # Allow neighbouring paragraphs, but still sentence-based.
        prev = text.rfind("\n\n", 0, max(0, pstart-2))
        nxt = text.find("\n\n", pend+2)
        pstart = 0 if prev < 0 else prev + 2
        pend = len(text) if nxt < 0 else nxt
    para=text[pstart:pend]
    sents=sentence_spans(para, pstart)
    if not sents:
        return {"mode":"sentence", "context_start":pstart, "context_end":pend, "context":para.strip(), "claim_sentence_index":None, "sentence_radius":radius, "outside_paragraph":outside_paragraph}
    idx=None
    for i,(ss,se,_sent) in enumerate(sents):
        if ss <= start <= se or (start <= ss and end >= ss):
            idx=i; break
    if idx is None:
        idx=min(range(len(sents)), key=lambda i: abs(sents[i][0]-start))
    lo=max(0, idx-radius); hi=min(len(sents), idx+radius+1)
    context_start=sents[lo][0]; context_end=sents[hi-1][1]
    return {
        "mode":"sentence",
        "context_start": context_start,
        "context_end": context_end,
        "context": text[context_start:context_end].strip(),
        "paragraph_start": pstart,
        "paragraph_end": pend,
        "claim_sentence_index": idx,
        "sentence_radius": radius,
        "outside_paragraph": outside_paragraph,
        "sentences_returned": hi-lo,
    }


def chunk_for_char(source_id: str, char_start: int|None) -> str|None:
    if char_start is None:
        return None
    conn=db()
    row=conn.execute("""SELECT span_id FROM spans WHERE source_id=? AND kind='chunk' AND char_start<=? AND char_end>=? ORDER BY char_end-char_start LIMIT 1""", (source_id,char_start,char_start)).fetchone()
    conn.close()
    return row["span_id"] if row else None


def source_ids() -> list[str]:
    conn=db(); rows=conn.execute("SELECT source_id FROM sources ORDER BY source_id").fetchall(); conn.close()
    return [r["source_id"] for r in rows]


def next_claim_id(source_id: str) -> str:
    conn=db(); rows=conn.execute("SELECT claim_id FROM source_cards WHERE source_id=?", (source_id,)).fetchall(); conn.close()
    nums=[]; pat=re.compile(rf"^CLM-{re.escape(source_id)}-(\d+)$")
    for r in rows:
        m=pat.match(r["claim_id"] or "")
        if m: nums.append(int(m.group(1)))
    return f"CLM-{source_id}-{(max(nums) if nums else 0)+1:04d}"


def infer_claim_type(text: str) -> str:
    l=norm(text)
    if any(k in l for k in ["we found", "results", "significant", "increased", "decreased", "associated", "effect"]): return "empirical finding"
    if any(k in l for k in ["method", "model", "regression", "simulation", "data"]): return "methodological claim"
    if any(k in l for k in ["limitation", "could not", "not considered", "underestimated"]): return "limitation"
    if any(k in l for k in ["should", "policy", "recommend", "design"]): return "policy implication"
    if any(k in l for k in ["defined as", "refers to"]): return "definition"
    return "background"


def strip_annotation_noise(text: str) -> str:
    """Remove parser/Obsidian annotation noise while preserving the scientific quote."""
    t = str(text or "")
    t = t.replace("==", "")
    t = re.sub(r"\[PAGE UNVERIFIED\]", " ", t, flags=re.I)
    # Remove bracketed curator notes, but not markdown links with parentheses immediately after.
    t = re.sub(r"\[(?![^\]]+\]\()[^\]]{8,220}\]", " ", t)
    # Remove hashtag metadata such as #MA/RQ2.
    t = re.sub(r"#[A-Za-z0-9_./§-]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def evidence_sentences(text: str) -> list[str]:
    text = strip_annotation_noise(text)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9_\"“])", text)
    return [p.strip() for p in parts if len(p.strip()) >= 25]


def refine_evidence_quote(evidence: str, claim: str = "", max_chars: int = 700) -> str:
    """Keep evidence tight: normally one sentence or a short two-sentence quote.

    Evidence is not context. Context is expanded separately by `context` / the static export.
    """
    evidence = str(evidence or "").strip()
    if not evidence:
        return evidence
    cleaned = strip_annotation_noise(evidence)
    if len(cleaned) <= max_chars and "==" not in evidence and "[PAGE" not in evidence and "#MA/" not in evidence:
        return cleaned
    sents = evidence_sentences(evidence)
    if not sents:
        return short(cleaned, max_chars)
    qwords = set(w for w in re.findall(r"\w+", norm(claim)) if len(w) > 3)
    scored=[]
    for i,sent in enumerate(sents):
        words=set(w for w in re.findall(r"\w+", norm(sent)) if len(w) > 3)
        score=len(qwords & words) / max(1, len(qwords))
        # Prefer sentences with numbers/effect language when the claim is empirical.
        if re.search(r"\b\d+\s*%|significant|increase|decrease|positive|negative|models?\b", sent, re.I):
            score += 0.15
        scored.append((score,i,sent))
    scored.sort(reverse=True)
    best_i=scored[0][1]
    chosen=[sents[best_i]]
    # Add a neighbouring sentence only if the first is too cryptic and total remains bounded.
    for j in [best_i+1, best_i-1]:
        if 0 <= j < len(sents) and len(" ".join(chosen+[sents[j]])) <= max_chars:
            # Only include if it shares at least one meaningful claim word.
            words=set(w for w in re.findall(r"\w+", norm(sents[j])) if len(w) > 3)
            if qwords & words:
                chosen.append(sents[j])
                break
    out=" ".join(chosen)
    return short(out, max_chars)


def tags_for_claim(claim_id: str) -> list[str]:
    conn=db(); rows=conn.execute("SELECT tag_type, tag FROM claim_tags WHERE claim_id=?", (claim_id,)).fetchall(); conn.close()
    return [f"{r['tag_type']}:{r['tag']}" for r in rows]


def evidence_grade(row: sqlite3.Row|dict[str, Any]) -> str:
    """Conservative writing-time grade derived from status, page/offset anchoring and representation."""
    d=dict(row)
    status=d.get("verification_status") or ""
    if status in {"rejected", "superseded"}:
        return "X"
    rep=d.get("claim_representation") or ""
    page_status=d.get("page_status") or ""
    has_offsets=d.get("char_start") is not None and d.get("char_end") is not None
    page_ok=bool(d.get("page")) and page_status not in {"needs_page_check", ""}
    if status == "verified" and page_ok and rep in {"source_quote", "lightly_normalized_source", "source_range"}:
        return "A"
    if status == "verified" or (has_offsets and page_ok and rep in {"source_quote", "lightly_normalized_source", "source_range"}):
        return "B"
    if has_offsets or d.get("page") or rep == "paraphrase":
        return "C"
    return "D"


def card_role(row: sqlite3.Row|dict[str, Any]) -> str:
    """Classify how a source-range card should be used in writing.

    This deliberately separates literal result claims from context/background/method cards.
    The database table is still named `claims` for backwards compatibility, but the UI/LLM
    should treat `card_role` as the writing-time semantic role.
    """
    d=dict(row)
    ct=(d.get("claim_type") or "unknown").lower()
    if ct == "empirical finding":
        return "result_claim"
    if ct == "methodological claim":
        return "method_card"
    if ct == "definition":
        return "definition_card"
    if ct == "theoretical claim":
        return "theory_card"
    if ct == "policy implication":
        return "policy_design_card"
    if ct == "limitation":
        return "limitation_card"
    if ct == "background":
        return "background_card"
    if ct == "contradiction":
        return "contradiction_card"
    return "unknown_card"


def claim_relations_for(claim_id: str) -> list[dict[str, Any]]:
    conn=db()
    rows=conn.execute("""
        SELECT * FROM claim_relations
        WHERE claim_a=? OR claim_b=?
        ORDER BY created_at DESC
    """, (claim_id, claim_id)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def upsert_claim(claim: dict[str, Any], tags: dict[str, list[str]]|None=None) -> None:
    init_db(True)
    claim_id=claim["claim_id"]; source_id=claim["source_id"]
    conn=db(); cur=conn.cursor()
    source_hash_row=cur.execute("SELECT source_hash FROM sources WHERE source_id=?", (source_id,)).fetchone()
    if not source_hash_row:
        raise SystemExit(f"Unknown source_id: {source_id}")
    source_hash=source_hash_row["source_hash"]
    cur.execute("DELETE FROM claim_tags WHERE claim_id=?", (claim_id,))
    cur.execute("DELETE FROM claims_fts WHERE claim_id=?", (claim_id,))
    cur.execute("DELETE FROM spans WHERE ref_id=? AND kind='evidence'", (claim_id,))
    span_id=claim.get("source_span_id") or f"EVID-{claim_id}"
    ev=claim.get("evidence", "")
    add_span(cur, span_id=span_id, source_id=source_id, kind="evidence", ref_id=claim_id,
             char_start=claim.get("char_start"), char_end=claim.get("char_end"), line_start=claim.get("line_start"), line_end=claim.get("line_end"),
             page_start=claim.get("page") or "", page_end=claim.get("page") or "", heading="", text=ev)
    cur.execute("""INSERT OR REPLACE INTO source_cards VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        claim_id, source_id, claim.get("claim", ""), ev, claim.get("claim_representation", "source_quote"), claim.get("claim_type", "unknown"),
        str(claim.get("page") or ""), claim.get("page_status", ""), claim.get("verification_status", "candidate_needs_review"),
        claim.get("confidence", "medium"), claim.get("scope_note", ""), span_id,
        claim.get("char_start"), claim.get("char_end"), claim.get("line_start"), claim.get("line_end"), source_hash,
        claim.get("extraction_mode", "unknown"), claim.get("created_at") or now(), now()
    ))
    all_tags=[]
    for tag_type, values in (tags or {}).items():
        for tag in split_list(values):
            cur.execute("INSERT OR IGNORE INTO claim_tags VALUES (?,?,?)", (claim_id, tag_type, tag))
            all_tags.append(f"{tag_type}:{tag}")
    cur.execute("INSERT INTO claims_fts(claim_id, claim, evidence, tags) VALUES (?,?,?,?)", (claim_id, claim.get("claim", ""), ev, " ".join(all_tags)))
    conn.commit(); conn.close()


def claim_card(row: sqlite3.Row|dict[str, Any], fields: str="minimal") -> dict[str, Any]:
    d=dict(row)
    conn=db(); src=conn.execute("SELECT authors, year, title, doi FROM sources WHERE source_id=?", (d.get("source_id"),)).fetchone(); conn.close()
    citation=f"{src['authors']} ({src['year']})" if src else d.get("source_id")
    representation = d.get("claim_representation")
    exact_text = claim_exact_source_text(d) if representation == "source_range" else ""
    display_claim = exact_text or d.get("claim")
    card={
        "claim_id": d.get("claim_id"), "score": round(float(d.get("score", 0) or 0),4),
        "source_id": d.get("source_id"), "citation_hint": citation, "page": page_for_claim_dict(d),
        "status": d.get("verification_status"), "evidence_grade": evidence_grade(d), "claim_type": d.get("claim_type"), "card_role": card_role(d),
        "claim_representation": representation, "claim": display_claim,
    }
    if d.get("why_retrieved"): card["why_retrieved"] = d.get("why_retrieved")
    if fields in ["standard", "full"]:
        card.update({"evidence": exact_text or d.get("evidence"), "scope_note": d.get("scope_note"), "line_start": d.get("line_start"), "line_end": d.get("line_end")})
    if fields == "full":
        card.update(d)
        card["evidence_grade"] = evidence_grade(d)
        card["tags"] = tags_for_claim(d.get("claim_id"))
        card["relations"] = claim_relations_for(d.get("claim_id"))
    return card


def find_source_for_quote(quote: str, source_id: str|None=None) -> tuple[str, str, dict[str, Any]]:
    ids=[source_id] if source_id else source_ids()
    best=(0.0, None, "", {})
    for sid in ids:
        if not sid: continue
        text=read_source_text(sid)
        loc=locate_span(text, quote)
        if loc.get("found"):
            score=1.0 if loc.get("method") in ["exact_raw", "segment_raw"] else float(loc.get("score", 0.7))
            if score > best[0]: best=(score, sid, text, loc)
    if not best[1]:
        raise SystemExit("Could not locate quote in source(s). Pass --source-id and use a longer exact sentence/excerpt.")
    return best[1], best[2], best[3]


def cmd_init(args):
    init_db(False)


def cmd_ingest(args):
    meta={k:getattr(args,k) for k in ["title","authors","year","doi","source_type","disciplines","geography","methodology","theory","quality","notes"]}
    sid = args.source_id or (canonical_source_id_from_doi(args.doi) if args.doi else slug(args.title or Path(args.path).stem))
    ingest_source(Path(args.path), sid, meta)
    print(f"Ingested {sid}")


def cmd_mark_claim(args):
    quote=args.quote or args.text or ""
    if not quote and not sys.stdin.isatty(): quote=sys.stdin.read()
    quote=quote.strip()
    if not quote: raise SystemExit("Provide quote/excerpt as argument, --text, or stdin.")
    sid,text,loc=find_source_for_quote(quote, args.source_id)
    evidence=text[int(loc["char_start"]):int(loc["char_end"])].strip() if loc.get("char_start") is not None else quote
    page,page_span=page_for_char(sid, loc.get("char_start")); chunk=chunk_for_char(sid, loc.get("char_start"))
    claim_text=args.claim.strip() if args.claim else evidence
    rep="paraphrase" if args.claim else (args.claim_representation or "source_quote")
    claim={"claim_id": args.claim_id or next_claim_id(sid), "source_id": sid, "claim": claim_text, "evidence": evidence,
        "claim_representation": rep, "claim_type": args.claim_type or infer_claim_type(evidence), "page": page,
        "page_status": "page_matched_from_source_span" if page else "needs_page_check", "verification_status": args.status,
        "confidence": args.confidence, "scope_note": args.scope_note or "Marked directly from source text.",
        "char_start": loc.get("char_start"), "char_end": loc.get("char_end"), "line_start": loc.get("line_start"), "line_end": loc.get("line_end"),
        "extraction_mode": "mark_claim_v2"}
    tags={"construct": split_list(args.constructs), "rq": split_list(args.rq_tags), "discipline": split_list(args.discipline), "geography": split_list(args.geography), "methodology": split_list(args.methodology)}
    # duplicate guard by identical source span
    conn=db(); dup=conn.execute("SELECT * FROM source_cards WHERE source_id=? AND char_start=? AND char_end=?", (sid, claim["char_start"], claim["char_end"])).fetchall(); conn.close()
    if dup and not args.allow_duplicate:
        payload={"created": False, "reason": "matching source span exists", "existing": [claim_card(r, args.fields) for r in dup], "proposed": claim}
        print_json(payload) if args.json else print_json(payload)
        return
    upsert_claim(claim,tags)
    conn=db(); row=conn.execute("SELECT * FROM source_cards WHERE claim_id=?", (claim["claim_id"],)).fetchone(); conn.close()
    payload={"created": True, "claim": claim_card(row,args.fields), "chunk_span_id": chunk, "page_span_id": page_span}
    print_json(payload) if args.json else print(f"Created {claim['claim_id']} | {sid} | page {page or '?'}")


def cmd_mark_span(args):
    """Create a pure source-range claim from char_start/char_end."""
    init_db(True)
    text=read_source_text(args.source_id)
    start=max(0, int(args.char_start)); end=min(len(text), int(args.char_end))
    if end <= start:
        raise SystemExit("char_end must be greater than char_start")
    quote=text[start:end].strip()
    if not quote:
        raise SystemExit("Selected span is empty")
    offsets=line_offsets(text)
    page,_page_span=page_for_char(args.source_id, start)
    claim={
        "claim_id": args.claim_id or next_claim_id(args.source_id),
        "source_id": args.source_id,
        "claim": quote,
        "evidence": quote,
        "claim_representation": "source_range",
        "claim_type": args.claim_type or infer_claim_type(quote),
        "page": page,
        "page_status": "page_matched_from_source_span" if page else "needs_page_check",
        "verification_status": args.status,
        "confidence": args.confidence,
        "scope_note": args.scope_note or "Claim is represented canonically as a source character range.",
        "char_start": start,
        "char_end": end,
        "line_start": line_no(offsets, start),
        "line_end": line_no(offsets, end),
        "extraction_mode": "mark_span_source_range",
    }
    tags={"construct": split_list(args.constructs), "rq": split_list(args.rq_tags), "discipline": split_list(args.discipline), "geography": split_list(args.geography), "methodology": split_list(args.methodology)}
    conn=db(); dup=conn.execute("SELECT * FROM source_cards WHERE source_id=? AND char_start=? AND char_end=?", (args.source_id, start, end)).fetchall(); conn.close()
    if dup and not args.allow_duplicate:
        payload={"created": False, "reason": "matching source range exists", "existing": [claim_card(r,args.fields) for r in dup], "proposed": claim}
        print_json(payload) if args.json else print_json(payload)
        return
    upsert_claim(claim,tags)
    conn=db(); row=conn.execute("SELECT * FROM source_cards WHERE claim_id=?", (claim["claim_id"],)).fetchone(); conn.close()
    payload={"created": True, "claim": claim_card(row,args.fields)}
    print_json(payload) if args.json else print(f"Created {claim['claim_id']} | {args.source_id} | chars {start}-{end}")


def sanitize_fts(q: str) -> str:
    return " ".join(re.findall(r"\w+", q or "", flags=re.UNICODE)) or q


def token_overlap_score(query: str, text: str) -> float:
    q=set(w for w in re.findall(r"\w+", norm(query)) if len(w)>3)
    t=set(w for w in re.findall(r"\w+", norm(text)) if len(w)>3)
    return len(q & t) / max(1, len(q))


def retrieve_claims(query: str, limit: int=8, fields: str="minimal", filters: dict[str, Any]|None=None) -> list[dict[str, Any]]:
    filters=filters or {}
    conn=db(); cur=conn.cursor()
    bm25={}; why={}
    try:
        rows=cur.execute("SELECT claim_id, bm25(claims_fts) AS rank FROM claims_fts WHERE claims_fts MATCH ? ORDER BY rank LIMIT ?", (sanitize_fts(query), max(50, limit*10))).fetchall()
        for i,r in enumerate(rows):
            bm25[r["claim_id"]]=i+1; why.setdefault(r["claim_id"],[]).append(f"bm25#{i+1}")
    except sqlite3.OperationalError:
        pass
    all_rows=cur.execute("SELECT * FROM source_cards WHERE verification_status!='rejected'").fetchall()
    candidates=[]
    for r in all_rows:
        d=dict(r)
        if filters.get("source_id") and d["source_id"] != filters["source_id"]: continue
        if filters.get("source_ids") and d["source_id"] not in filters["source_ids"]: continue
        if filters.get("verified_only") and d["verification_status"] != "verified": continue
        if filters.get("statuses") and d["verification_status"] not in filters["statuses"]: continue
        if filters.get("claim_types") and d["claim_type"] not in filters["claim_types"]: continue
        if filters.get("card_roles") and card_role(d) not in filters["card_roles"]: continue
        tags=" ".join(tags_for_claim(d["claim_id"]))
        overlap=token_overlap_score(query, " ".join([d.get("claim",""), d.get("evidence",""), tags]))
        rank=bm25.get(d["claim_id"])
        score=(1/(60+rank) if rank else 0) * 100 + overlap
        if score <= 0 and query: continue
        d["score"]=score; d["why_retrieved"]=why.get(d["claim_id"], []) + (["token_overlap"] if overlap else [])
        candidates.append(d)
    conn.close()
    candidates=sorted(candidates, key=lambda x:x.get("score",0), reverse=True)[:limit]
    return [claim_card(c,fields) for c in candidates]


def cmd_retrieve(args):
    filters={"source_id": args.source_id, "verified_only": args.verified_only, "statuses": split_list(args.status), "claim_types": split_list(args.claim_type), "card_roles": split_list(getattr(args, "card_role", None))}
    results=retrieve_claims(args.query,args.limit,args.fields,filters)
    payload={"query": args.query, "count": len(results), "results": results}
    if args.json: print_json(payload)
    else:
        print(f"# retrieve: {args.query}\n")
        for r in results:
            print(f"- **{r['claim_id']}** [{r['score']}] grade:{r.get('evidence_grade')} · role:{r.get('card_role')} · {r['citation_hint']} p.{r.get('page') or '?'} · {r['status']} · {r['claim_type']}")
            print(f"  {r['claim']}")
            if args.fields in ["standard","full"] and r.get("evidence"): print(f"  evidence: {short(r['evidence'])}")
            if r.get("why_retrieved"): print(f"  why: {', '.join(r['why_retrieved'])}")


def cmd_context(args):
    conn=db(); row=conn.execute("SELECT * FROM source_cards WHERE claim_id=?", (args.claim_id,)).fetchone(); conn.close()
    if not row: raise SystemExit(f"Unknown claim_id: {args.claim_id}")
    d=dict(row); text=read_source_text(d["source_id"])
    if args.mode == "full":
        payload={"claim": claim_card(d,args.fields), "relations": claim_relations_for(args.claim_id), "mode":"full", "source_id": d["source_id"], "context_start":0, "context_end":len(text), "context": text}
    elif args.mode == "char":
        start=max(0, int(d["char_start"] or 0)-args.window); end=min(len(text), int(d["char_end"] or d["char_start"] or 0)+args.window)
        payload={"claim": claim_card(d,args.fields), "relations": claim_relations_for(args.claim_id), "mode":"char", "context_start": start, "context_end": end, "context": text[start:end]}
    else:
        ctx=sentence_aware_context(d["source_id"], int(d["char_start"] or 0), int(d["char_end"] or d["char_start"] or 0), radius=args.sentence_radius, outside_paragraph=args.outside_paragraph)
        payload={"claim": claim_card(d,args.fields), "relations": claim_relations_for(args.claim_id), **ctx}
    if args.json: print_json(payload); return
    print(f"{d['claim_id']} | {d['source_id']} | p.{claim_card(d).get('page') or '?'} | lines {d.get('line_start')}-{d.get('line_end')} | mode={payload.get('mode')}\n")
    print(textwrap.fill(payload["claim"].get("claim", ""), width=100)); print("\n--- context ---"); print(payload["context"])


def cmd_source_text(args):
    text=read_source_text(args.source_id)
    range_start=0; range_end=len(text); range_handle=None
    if getattr(args, "range", None):
        range_start, range_end = parse_char_range(args.range)
        range_start=max(0, range_start); range_end=min(len(text), range_end)
        range_handle=source_range_handle(args.source_id, range_start, range_end)
        out=text[range_start:range_end]
        truncated=False
    elif args.max_chars and len(text) > args.max_chars:
        out=text[:args.max_chars]
        range_end=len(out)
        truncated=True
    else:
        out=text; truncated=False
    payload={"source_id": args.source_id, "char_count": len(text), "range_start": range_start, "range_end": range_end, "range_handle": range_handle or source_range_handle(args.source_id, range_start, range_end), "returned_chars": len(out), "truncated": truncated, "text": out}
    if args.json:
        print_json(payload)
    else:
        print(out)


def cmd_resolve_handle(args):
    h=str(args.handle).strip()
    m=re.match(r"^SOURCE_RANGE\[([^:\]]+):(\d+)-(\d+)\]$", h)
    if m:
        source_id=m.group(1); start=int(m.group(2)); end=int(m.group(3)); text=read_source_text(source_id); out=text[max(0,start):min(len(text),end)]
        payload={"handle":h,"kind":"source_range","source_id":source_id,"char_start":start,"char_end":end,"text":out}
        print_json(payload) if args.json else print(out)
        return
    m=re.match(r"^CLAIM\[([^\]]+)\]$", h)
    if m:
        cid=m.group(1); conn=db(); row=conn.execute("SELECT * FROM source_cards WHERE claim_id=?", (cid,)).fetchone(); conn.close()
        if not row: raise SystemExit(f"Unknown claim handle: {cid}")
        d=dict(row); ctx=sentence_aware_context(d["source_id"], int(d["char_start"] or 0), int(d["char_end"] or d["char_start"] or 0), radius=args.sentence_radius, outside_paragraph=args.outside_paragraph)
        payload={"handle":h,"kind":"claim","claim":claim_card(d,"standard"),"source_range":source_range_handle(d["source_id"], d["char_start"], d["char_end"]), **ctx}
        print_json(payload) if args.json else print(f"{cid}\n\n{payload['claim']['claim']}\n\n--- context ---\n{payload['context']}")
        return
    m=re.match(r"^CITATION_CONTEXT\[([^\]]+)\]$", h)
    if m:
        context_id=m.group(1); conn=db(); row=conn.execute("SELECT * FROM citation_contexts WHERE context_id=?", (context_id,)).fetchone(); conn.close()
        if not row: raise SystemExit(f"Unknown citation context handle: {context_id}")
        r=dict(row); payload={"handle":h,"kind":"citation_context","context":r,"source_range":source_range_handle(r["citing_source_id"], r["char_start"], r["char_end"])}
        print_json(payload) if args.json else print(f"{context_id} | {r['citing_source_id']} cites {r['cited_author_key']} {r['cited_year']}\n\n{r['context_text']}")
        return
    m=re.match(r"^PAPER\[([^\]]+)\]$", h)
    if m:
        source_id=m.group(1); cmd_source_text(argparse.Namespace(source_id=source_id, range=None, max_chars=args.max_chars, json=args.json)); return
    raise SystemExit("Unknown handle. Supported: SOURCE_RANGE[source:start-end], CLAIM[id], CITATION_CONTEXT[id], PAPER[source_id]")




def section_map_for_source(source_id: str, max_claims_per_section: int = 5) -> dict[str, Any]:
    text=read_source_text(source_id)
    conn=db(); src=conn.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
    claims=[dict(r) for r in conn.execute("SELECT * FROM source_cards WHERE source_id=? AND verification_status!='rejected' ORDER BY char_start, claim_id", (source_id,)).fetchall()]
    cites=[dict(r) for r in conn.execute("SELECT * FROM citation_contexts WHERE citing_source_id=? ORDER BY char_start", (source_id,)).fetchall()]
    conn.close()
    sections=heading_spans(text)
    if not sections:
        sections=[{"heading":"full_text","char_start":0,"char_end":len(text)}]
    out_sections=[]
    for sec in sections:
        start,end=sec["char_start"],sec["char_end"]
        sec_claims=[c for c in claims if c.get("char_start") is not None and start <= int(c["char_start"]) < end]
        sec_cites=[c for c in cites if c.get("char_start") is not None and start <= int(c["char_start"]) < end]
        page_start,_=page_for_char(source_id,start)
        page_end,_=page_for_char(source_id,max(start,end-1))
        claim_cards=[claim_card(c,"minimal") for c in sec_claims[:max_claims_per_section]]
        out_sections.append({
            "heading": sec["heading"],
            "handle": source_range_handle(source_id,start,end),
            "char_start": start,
            "char_end": end,
            "page_start": page_start,
            "page_end": page_end,
            "estimated_tokens": estimate_tokens(text[start:end]),
            "claim_count": len(sec_claims),
            "claims_preview": claim_cards,
            "citation_context_count": len(sec_cites),
            "citation_context_handles_preview": [citation_context_handle(c["context_id"]) for c in sec_cites[:max_claims_per_section]],
        })
    return {"source": dict(src) if src else {"source_id":source_id}, "source_handle": f"PAPER[{source_id}]", "char_count": len(text), "section_count": len(out_sections), "claim_count": len(claims), "citation_context_count": len(cites), "sections": out_sections}


def render_source_map_md(m: dict[str, Any]) -> str:
    src=m.get("source",{})
    lines=[f"# Source Map: {src.get('source_id')}", "", f"Title: {src.get('title','')}", f"Authors/year: {src.get('authors','')} ({src.get('year','')})", f"Source handle: `{m.get('source_handle')}`", f"Characters: {m.get('char_count')} | Sections: {m.get('section_count')} | Claims: {m.get('claim_count')} | Citation contexts: {m.get('citation_context_count')}", "", "## Sections", ""]
    for sec in m.get("sections",[]):
        page = f"p.{sec.get('page_start') or '?'}" if sec.get('page_start')==sec.get('page_end') else f"p.{sec.get('page_start') or '?'}–{sec.get('page_end') or '?'}"
        lines.append(f"### {sec.get('heading')}")
        lines.append(f"- Range: `{sec.get('handle')}` | {page} | ~{sec.get('estimated_tokens')} tokens")
        lines.append(f"- Claims: {sec.get('claim_count')} | Citation contexts: {sec.get('citation_context_count')}")
        if sec.get("claims_preview"):
            lines.append("- Claim preview:")
            for c in sec["claims_preview"]:
                lines.append(f"  - `{claim_handle(c['claim_id'])}` {c.get('claim')}")
        if sec.get("citation_context_handles_preview"):
            lines.append("- Citation context handles: " + ", ".join(f"`{h}`" for h in sec["citation_context_handles_preview"]))
        lines.append("")
    return "\n".join(lines)


def cmd_source_map(args):
    m=section_map_for_source(args.source_id, args.max_claims_per_section)
    if args.json:
        print_json(m)
    else:
        print(render_source_map_md(m))


def cmd_import_v1(args):
    init_db(True)
    v1=Path(args.v1_path)
    reg=v1/"source_registry.csv"; ledger=v1/"claim_ledger.jsonl"
    if not reg.exists() or not ledger.exists(): raise SystemExit("V1 path must contain source_registry.csv and claim_ledger.jsonl")
    imported_sources=0
    with reg.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            sid=r.get("source_id"); p=v1/r.get("path","")
            if sid and p.exists():
                ingest_source(p, sid, r); imported_sources += 1
    imported_claims=0
    for line in ledger.read_text(encoding="utf-8").splitlines():
        if not line.strip(): continue
        c=json.loads(line); sid=c.get("source_id")
        if not sid: continue
        try: text=read_source_text(sid)
        except SystemExit: continue
        loc=c.get("source_span") or {}
        if loc.get("found") and loc.get("char_start") is None and loc.get("normalized_start") is not None:
            rs, re_ = raw_span_from_normalized_offsets(text, int(loc.get("normalized_start")), int(loc.get("normalized_end", loc.get("normalized_start"))))
            if rs is not None:
                offsets=line_offsets(text)
                loc={**loc, "char_start": rs, "char_end": re_, "line_start": line_no(offsets, rs), "line_end": line_no(offsets, re_)}
        if not loc.get("found") or loc.get("char_start") is None:
            loc=locate_span(text, c.get("evidence", ""))
        if not loc.get("found") or loc.get("char_start") is None:
            continue
        # Preserve the curated evidence quote from V1. Do not replace it with a whole fallback chunk.
        raw_evidence = c.get("evidence") or text[int(loc["char_start"]):int(loc["char_end"])].strip()
        evidence = refine_evidence_quote(raw_evidence, c.get("claim", ""), max_chars=700)
        page=c.get("page") or page_for_char(sid, loc.get("char_start"))[0]
        rep=c.get("claim_representation") or ("source_quote" if norm(c.get("claim")) == norm(evidence) else "paraphrase")
        claim={"claim_id": c.get("claim_id") or next_claim_id(sid), "source_id": sid, "claim": c.get("claim") or evidence,
            "evidence": evidence, "claim_representation": rep, "claim_type": c.get("claim_type") or "unknown", "page": page,
            "page_status": c.get("page_status") or ("page_matched_from_source_span" if page else "needs_page_check"),
            "verification_status": c.get("verification_status") or "candidate_needs_review", "confidence": c.get("confidence") or "medium",
            "scope_note": c.get("scope_note") or "", "char_start": loc.get("char_start"), "char_end": loc.get("char_end"),
            "line_start": loc.get("line_start"), "line_end": loc.get("line_end"), "extraction_mode": c.get("extraction_mode") or "import_v1"}
        tags={"construct": c.get("constructs") or [], "rq": c.get("rq_tags") or [], "discipline": c.get("discipline") or [], "geography": c.get("geography") or [], "methodology": c.get("methodology") or []}
        upsert_claim(claim,tags); imported_claims += 1
    print(f"Imported V1 → V2: {imported_sources} sources, {imported_claims} claims")


def validate_chapter_profile(prof: dict[str, Any]) -> list[str]:
    warnings=[]
    if not prof.get("chapter_id") and not prof.get("chapter_title"):
        warnings.append("Profile should define chapter_id or chapter_title.")
    if not prof.get("purpose"):
        warnings.append("Profile has no purpose.")
    allowed=set(prof.get("allowed_statuses", []))
    unknown_statuses=allowed - STATUSES
    if unknown_statuses:
        warnings.append(f"Unknown allowed_statuses: {sorted(unknown_statuses)}")
    if not isinstance(prof.get("sections", []), list) or not prof.get("sections"):
        warnings.append("Profile has no sections.")
    for sec in prof.get("sections", []):
        if not (sec.get("query") or sec.get("queries")):
            warnings.append(f"Section {sec.get('section_id') or sec.get('heading') or '?'} has no query/queries.")
    return warnings


def load_chapter_profile(path_or_id: str) -> dict[str, Any]:
    p=Path(path_or_id)
    if not p.exists(): p=CHAPTER_PROFILES/f"{path_or_id}.json"
    if not p.exists(): raise SystemExit(f"Chapter profile not found: {path_or_id}")
    prof=json.loads(p.read_text(encoding="utf-8"))
    warnings=validate_chapter_profile(prof)
    if warnings:
        prof.setdefault("profile_warnings", []).extend(warnings)
    return prof


def chapter_sections_with_cards(prof: dict[str, Any], default_limit: int = 10, fields: str = "standard") -> tuple[str, list[dict[str, Any]], set[str]]:
    """Resolve a chapter profile into retrieved section cards.

    Used by chapter briefs and chapter-aware citation backtracking so both tools
    operate from the same chapter evidence contract.
    """
    chapter_id=prof.get("chapter_id") or slug(prof.get("chapter_title","chapter"))
    global_filters=dict(prof.get("global_filters", {}))
    if prof.get("allowed_statuses") and not global_filters.get("statuses"):
        global_filters["statuses"] = prof.get("allowed_statuses")
    sections=[]; used=set()
    for sec in prof.get("sections", []):
        q=" ".join(sec.get("queries", []) or [sec.get("query", "")])
        filters=dict(global_filters)
        if sec.get("source_ids"): filters["source_ids"]=sec.get("source_ids")
        if sec.get("claim_types"): filters["claim_types"]=sec.get("claim_types")
        if sec.get("card_roles"): filters["card_roles"]=sec.get("card_roles")
        if sec.get("statuses"): filters["statuses"]=sec.get("statuses")
        cards=retrieve_claims(q, sec.get("limit", default_limit), fields, filters)
        for card in cards: used.add(card["claim_id"])
        sections.append({
            "section_id": sec.get("section_id"),
            "heading": sec.get("heading"),
            "section_type": sec.get("section_type") or sec.get("type"),
            "citation_role": sec.get("citation_role"),
            "writing_goal": sec.get("writing_goal", ""),
            "query": q,
            "source_cards": cards,
            "claims": cards,  # compatibility alias
        })
    return chapter_id, sections, used


def chapter_sections_with_cards(prof: dict[str, Any], default_limit: int = 10, fields: str = "standard") -> tuple[str, list[dict[str, Any]], set[str]]:
    """Resolve a chapter profile into retrieved section cards.

    Used by chapter briefs and chapter-aware citation backtracking so both tools
    operate from the same chapter evidence contract.
    """
    chapter_id=prof.get("chapter_id") or slug(prof.get("chapter_title","chapter"))
    global_filters=dict(prof.get("global_filters", {}))
    if prof.get("allowed_statuses") and not global_filters.get("statuses"):
        global_filters["statuses"] = prof.get("allowed_statuses")
    sections=[]; used=set()
    for sec in prof.get("sections", []):
        q=" ".join(sec.get("queries", []) or [sec.get("query", "")])
        filters=dict(global_filters)
        if sec.get("source_ids"): filters["source_ids"]=sec.get("source_ids")
        if sec.get("claim_types"): filters["claim_types"]=sec.get("claim_types")
        if sec.get("card_roles"): filters["card_roles"]=sec.get("card_roles")
        if sec.get("statuses"): filters["statuses"]=sec.get("statuses")
        cards=retrieve_claims(q, sec.get("limit", default_limit), fields, filters)
        for card in cards: used.add(card["claim_id"])
        sections.append({
            "section_id": sec.get("section_id"),
            "heading": sec.get("heading"),
            "section_type": sec.get("section_type") or sec.get("type"),
            "citation_role": sec.get("citation_role"),
            "writing_goal": sec.get("writing_goal", ""),
            "query": q,
            "source_cards": cards,
            "claims": cards,  # compatibility alias
        })
    return chapter_id, sections, used


def cmd_chapter_brief(args):
    prof=load_chapter_profile(args.profile)
    chapter_id, sections, used = chapter_sections_with_cards(prof, args.limit, "standard")
    conn=db()
    status_counts={r["verification_status"]: r["n"] for r in conn.execute(f"SELECT verification_status, COUNT(*) n FROM source_cards WHERE claim_id IN ({','.join(['?']*len(used)) or "''"}) GROUP BY verification_status", list(used)).fetchall()} if used else {}
    source_counts={r["source_id"]: r["n"] for r in conn.execute(f"SELECT source_id, COUNT(*) n FROM source_cards WHERE claim_id IN ({','.join(['?']*len(used)) or "''"}) GROUP BY source_id", list(used)).fetchall()} if used else {}
    conn.close()
    warnings=list(prof.get("profile_warnings", []))
    if status_counts.get("verified",0)==0 and used: warnings.append("No verified claims in this chapter brief; use candidate claims for drafting only after review.")
    if len(source_counts) < prof.get("minimum_source_diversity", 1): warnings.append("Source diversity is below profile target.")
    required_topics=set(prof.get("required_topics", []))
    if required_topics and used:
        conn2=db()
        placeholders=','.join(['?']*len(used))
        tag_rows=conn2.execute(f"SELECT DISTINCT tag FROM claim_tags WHERE claim_id IN ({placeholders})", list(used)).fetchall()
        conn2.close()
        present={r['tag'] for r in tag_rows}
        missing=sorted(required_topics - present)
        if missing:
            warnings.append(f"Required topics not represented in retrieved claims: {missing}")
    packet={"chapter_id": chapter_id, "chapter_title": prof.get("chapter_title"), "purpose": prof.get("purpose"), "research_questions": prof.get("research_questions", []), "required_topics": prof.get("required_topics", []), "avoid": prof.get("avoid", []), "allowed_statuses": prof.get("allowed_statuses", []), "writing_contract": prof.get("writing_contract", default_writing_contract()), "claim_count": len(used), "source_counts": source_counts, "status_counts": status_counts, "warnings": warnings, "sections": sections}
    out_json=EXPORTS/f"chapter_brief_{chapter_id}.json"; out_md=EXPORTS/f"chapter_brief_{chapter_id}.md"
    out_json.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    md=render_chapter_brief_md(packet); out_md.write_text(md, encoding="utf-8")
    if args.json: print_json(packet)
    else:
        print(out_json.relative_to(ROOT)); print(out_md.relative_to(ROOT)); print(f"claims: {len(used)} | sources: {len(source_counts)} | warnings: {len(warnings)}")


def default_writing_contract() -> list[str]:
    return [
        "Use only claims from this packet unless you explicitly retrieve more evidence.",
        "Every substantive statement should map to at least one claim_id.",
        "Candidate claims must be treated as provisional until reviewed.",
        "If a claim matters for the argument, call context on its claim_id before drafting.",
        "Do not upgrade scope: preserve geography, method, species/case and uncertainty limits.",
        "Report contradictions, limitations and weak evidence rather than smoothing them away."
    ]


def render_chapter_brief_md(packet: dict[str, Any]) -> str:
    lines=[f"# Chapter Brief: {packet.get('chapter_title') or packet.get('chapter_id')}", "", f"Purpose: {packet.get('purpose','')}", "", "## Writing contract", ""]
    for rule in packet.get("writing_contract", []): lines.append(f"- {rule}")
    lines += ["", "## Coverage", "", f"- Claims: {packet.get('claim_count')}", f"- Sources: {packet.get('source_counts')}", f"- Statuses: {packet.get('status_counts')}"]
    if packet.get("warnings"):
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in packet["warnings"]]
    for sec in packet.get("sections", []):
        lines += ["", f"## {sec.get('heading') or sec.get('section_id')}", "", f"Writing goal: {sec.get('writing_goal','')}", "", f"Query: `{sec.get('query','')}`", ""]
        for c in sec.get("source_cards") or sec.get("claims", []):
            lines.append(f"- **{c['claim_id']}** [{c['score']}] grade:{c.get('evidence_grade')} · {c['citation_hint']} p.{c.get('page') or '?'} · {c['status']} · {c.get('card_role')} · {c['claim_type']} · `{c.get('claim_representation')}`")
            lines.append(f"  - Claim: {c['claim']}")
            lines.append(f"  - Evidence: {short(c.get('evidence',''), 300)}")
            lines.append(f"  - Deep dive: `python rh2.py context {c['claim_id']} --window 500`")
            lines.append("")
    return "\n".join(lines)


# ---------- source-range candidate extraction (no LLM rewriting) ----------

SKIP_SECTION_RE = re.compile(r"(?i)^(references|bibliography|acknowledg|orcid|palabras clave|oportunidades y retos|resumen|����)")
LOW_VALUE_RE = re.compile(r"(?i)^(figure|fig\.|table|keywords?|copyright|©|received|accepted|published|author contributions?|data availability|conflict of interest)")


def heading_spans(text: str) -> list[dict[str, Any]]:
    matches=list(re.finditer(r"(?m)^#{1,6}\s+(.+?)\s*$", text))
    spans=[]
    for i,m in enumerate(matches):
        raw=m.group(1).strip()
        title=re.sub(r"[*_`]+", "", raw).strip()
        spans.append({"heading": title, "char_start": m.end(), "char_end": matches[i+1].start() if i+1 < len(matches) else len(text)})
    return spans


def heading_for_char(text: str, pos: int) -> str:
    current="front_matter"
    for h in heading_spans(text):
        if h["char_start"] <= pos < h["char_end"]:
            return h["heading"]
        if h["char_start"] <= pos:
            current=h["heading"]
    return current


def candidate_sentence_spans(text: str) -> list[tuple[int,int,str,str]]:
    """Return sentence-ish spans with section heading.

    Claim candidates remain pure char ranges; this function only proposes ranges.
    """
    spans=[]
    for hs in heading_spans(text):
        heading=hs["heading"]
        if SKIP_SECTION_RE.search(heading):
            continue
        body=text[hs["char_start"]:hs["char_end"]]
        # Remove figure/image markdown lines from consideration but keep original offsets by splitting paragraphs.
        for pm in re.finditer(r"\S[\s\S]*?(?=\n\s*\n|\Z)", body):
            pstart=hs["char_start"]+pm.start(); pend=hs["char_start"]+pm.end()
            para=text[pstart:pend]
            stripped=para.strip()
            if not stripped or stripped.startswith("![") or LOW_VALUE_RE.match(stripped):
                continue
            for ss,se,sent in sentence_spans(para, pstart):
                clean=sent.strip()
                if len(clean) < 70 or len(clean) > 850:
                    continue
                if clean.startswith("![") or LOW_VALUE_RE.match(clean):
                    continue
                if re.search(r"(?i)^[-*]?\s*(select all|deselect all|filter)$", clean):
                    continue
                # Skip mostly Spanish translated abstract/body fragments: crude but avoids duplicate Spanish abstract.
                spanish_hits=sum(w in norm(clean) for w in ["política", "agrícola", "común", "objetivos", "medioambientales", "biodiversidad", "sostenible"])
                if spanish_hits >= 3 and heading.lower().startswith(("oportunidades", "resumen")):
                    continue
                spans.append((ss,se,clean,heading))
    return spans


def infer_constructs_from_text(text: str, heading: str = "") -> list[str]:
    l=norm(" ".join([text, heading]))
    rules=[
        ("CAP", ["common agricultural policy", "cap ", "cap's", "cap reform"]),
        ("European Green Deal", ["green deal", "egd"]),
        ("biodiversity", ["biodiversity", "species", "ecosystem", "landscape features", "homogenization"]),
        ("climate mitigation", ["climate", "greenhouse", "ghg", "emissions", "carbon"]),
        ("natural resources", ["pesticide", "fertilizer", "organic", "soil", "water", "synthetic input"]),
        ("funding allocation", ["fund", "payment", "subsid", "budget", "allocation"]),
        ("monitoring and indicators", ["monitor", "indicator", "target", "quantitative", "assessment"]),
        ("eco-schemes", ["ecoscheme", "eco-scheme", "eco schemes"]),
        ("AECM", ["aecm", "agri-environment"]),
        ("policy design", ["policy", "strategic plan", "intervention", "conditionality"]),
    ]
    out=[]
    for label,keys in rules:
        if any(k in l for k in keys): out.append(label)
    return out


def score_candidate_sentence(sent: str, heading: str) -> tuple[float, str]:
    l=norm(sent)
    score=0.0
    # High-value argumentative/result/policy cues.
    cues=[
        "we recommend", "should", "must", "need", "requires", "lack", "lacks", "missing", "unclear", "not addressed",
        "raises concerns", "may hinder", "risks", "threaten", "in contrast", "however", "although", "despite",
        "evidence-based", "quantitative target", "monitoring", "funding allocation", "subsid", "no baseline",
        "not tailored", "not yet", "remain unchanged", "is likely", "will likely", "can increase", "decrease",
        "potential", "mismatch", "weakening", "doubts", "incompatible", "ineffective", "performance",
    ]
    for c in cues:
        if c in l: score += 1.0
    # Paper-specific relevance for thesis/harness policy use.
    for c in ["cap", "green deal", "egd", "biodiversity", "climate", "pesticide", "organic", "ecoscheme", "aecm"]:
        if c in l: score += 0.55
    h=norm(heading)
    if any(x in h for x in ["protection of biodiversity", "climate", "sustainable management", "opportunities", "environmental commitments"]):
        score += 0.6
    if "abstract" in h:
        score += 0.3
    # Penalize citation-dense literature laundry lists and pure background facts.
    if len(re.findall(r"\([^)]+\d{4}[^)]*\)", sent)) >= 3:
        score -= 0.8
    if l.startswith(("in 2019", "established in", "within the egd")):
        score -= 0.3
    claim_type=infer_claim_type(sent)
    if any(k in l for k in ["should", "recommend", "target", "monitoring", "intervention", "strategic plan"]):
        claim_type="policy implication"
    if any(k in l for k in ["lack", "missing", "unclear", "not addressed", "doubts", "concerns", "hinder", "risk"]):
        if claim_type == "background": claim_type="limitation"
    return score, claim_type


def extract_source_range_candidates(source_id: str, min_score: float = 2.0, limit: int = 40) -> list[dict[str, Any]]:
    text=read_source_text(source_id)
    rows=[]
    for ss,se,sent,heading in candidate_sentence_spans(text):
        score, ctype=score_candidate_sentence(sent, heading)
        if score < min_score:
            continue
        rows.append({"source_id":source_id,"char_start":ss,"char_end":se,"text":sent,"heading":heading,"score":round(score,3),"claim_type":ctype,"constructs":infer_constructs_from_text(sent,heading)})
    # Prefer higher score and avoid near-duplicates by text hash.
    seen=set(); out=[]
    for r in sorted(rows, key=lambda x:(x["score"], len(x["text"])), reverse=True):
        h=sha1_short(norm(r["text"]))
        if h in seen: continue
        seen.add(h); out.append(r)
        if limit and len(out) >= limit: break
    # Return in source order for readability after top selection.
    return sorted(out, key=lambda x:x["char_start"])


def cmd_extract_source_ranges(args):
    init_db(True)
    cands=extract_source_range_candidates(args.source_id, args.min_score, args.limit)
    if args.dry_run:
        payload={"source_id":args.source_id,"count":len(cands),"candidates":cands}
        print_json(payload) if args.json else [print(f"{i+1}. [{c['score']}] {c['heading']} {c['char_start']}-{c['char_end']} {c['claim_type']} :: {short(c['text'],220)}") for i,c in enumerate(cands)]
        return
    created=[]; skipped=[]
    conn=db()
    existing={(r[0],r[1],r[2]) for r in conn.execute("SELECT source_id,char_start,char_end FROM source_cards WHERE source_id=?", (args.source_id,)).fetchall()}
    conn.close()
    for c in cands:
        key=(args.source_id,c["char_start"],c["char_end"])
        if key in existing and not args.allow_duplicate:
            skipped.append(c); continue
        claim={
            "claim_id": next_claim_id(args.source_id),
            "source_id": args.source_id,
            "claim": c["text"],
            "evidence": c["text"],
            "claim_representation": "source_range",
            "claim_type": c["claim_type"],
            "page": page_for_char(args.source_id,c["char_start"])[0],
            "page_status": "page_matched_from_source_span" if page_for_char(args.source_id,c["char_start"])[0] else "needs_page_check",
            "verification_status": args.status,
            "confidence": "medium",
            "scope_note": f"Auto-selected source-range candidate from section: {c['heading']}. Requires review.",
            "char_start": c["char_start"],
            "char_end": c["char_end"],
            "line_start": line_no(line_offsets(read_source_text(args.source_id)), c["char_start"]),
            "line_end": line_no(line_offsets(read_source_text(args.source_id)), c["char_end"]),
            "extraction_mode": "heuristic_source_range_v0",
        }
        tags={"construct": c["constructs"], "rq": split_list(args.rq_tags), "discipline": [], "geography": [], "methodology": []}
        upsert_claim(claim,tags); created.append(claim)
    payload={"source_id":args.source_id,"created":len(created),"skipped_existing":len(skipped)}
    print_json(payload) if args.json else print(f"Created {len(created)} source-range claims for {args.source_id}; skipped existing {len(skipped)}")


# ---------- paper-level retrieval / vector index ----------

def source_metadata_text(row: sqlite3.Row | dict[str, Any]) -> str:
    d=dict(row)
    return " ".join(str(d.get(k) or "") for k in ["source_id","title","authors","year","source_type","disciplines","geography","methodology","theory","quality","notes"])


def build_paper_index(max_features: int = 30000) -> dict[str, Any]:
    import pickle
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.preprocessing import normalize
    except Exception as e:
        raise SystemExit("scikit-learn is required for paper-level TF-IDF index") from e
    conn=db(); sources=[dict(r) for r in conn.execute("SELECT * FROM sources ORDER BY source_id").fetchall()]; conn.close()
    texts=[]; items=[]
    for src in sources:
        body=read_source_text(src["source_id"])
        # One vector per paper. Use metadata + full text. Stored payload does not keep full text.
        texts.append(source_metadata_text(src) + "\n\n" + body)
        items.append({k:src.get(k) for k in ["source_id","title","authors","year","doi","source_type","disciplines","geography","methodology","quality"]})
    vectorizer=TfidfVectorizer(stop_words="english", ngram_range=(1,2), min_df=1, max_features=max_features)
    X=normalize(vectorizer.fit_transform(texts)) if texts else None
    payload={"backend":"tfidf", "level":"paper", "items":items, "vectorizer":vectorizer, "matrix":X, "created_at":now()}
    with PAPER_INDEX.open("wb") as f:
        pickle.dump(payload,f)
    return payload


def load_or_build_paper_index() -> dict[str, Any]:
    import pickle
    if PAPER_INDEX.exists():
        with PAPER_INDEX.open("rb") as f:
            return pickle.load(f)
    return build_paper_index()


def rank_papers(query: str, limit: int = 5) -> list[dict[str, Any]]:
    idx=load_or_build_paper_index()
    if not idx.get("items"):
        return []
    try:
        from sklearn.preprocessing import normalize
        q=normalize(idx["vectorizer"].transform([query]))
        sims=(idx["matrix"] @ q.T).toarray().ravel()
        order=sims.argsort()[::-1][:limit]
        out=[]
        for i in order:
            item=dict(idx["items"][int(i)])
            item["score"]=round(float(sims[i]),4)
            out.append(item)
        return out
    except Exception:
        return []


def cmd_build_paper_index(args):
    idx=build_paper_index(args.max_features)
    print(f"Built paper-level {idx['backend']} index with {len(idx['items'])} papers -> {PAPER_INDEX.relative_to(ROOT)}")


def cmd_paper_search(args):
    rows=rank_papers(args.query,args.limit)
    if args.json:
        print_json({"query":args.query,"count":len(rows),"results":rows}); return
    print(f"# Paper search: {args.query}\n")
    for r in rows:
        print(f"- [{r['score']}] {r['source_id']} | {r.get('authors')} ({r.get('year')}) | {r.get('title')}")


def all_claim_cards_for_source(source_id: str, fields: str = "standard", status: str | None = None, verified_only: bool=False, claim_type: str | None=None, card_role_filter: str | None=None) -> list[dict[str, Any]]:
    conn=db(); rows=[dict(r) for r in conn.execute("SELECT * FROM source_cards WHERE source_id=? AND verification_status!='rejected' ORDER BY char_start, claim_id", (source_id,)).fetchall()]; conn.close()
    allowed_status=split_list(status)
    allowed_type=split_list(claim_type)
    allowed_role=split_list(card_role_filter)
    cards=[]
    for r in rows:
        if verified_only and r.get("verification_status") != "verified": continue
        if allowed_status and r.get("verification_status") not in allowed_status: continue
        if allowed_type and r.get("claim_type") not in allowed_type: continue
        if allowed_role and card_role(r) not in allowed_role: continue
        cards.append(claim_card(r, fields))
    return cards


def cmd_paper_brief(args):
    papers=rank_papers(args.query,args.paper_limit)
    sections=[]; total_tokens=0
    for p in papers:
        cards=all_claim_cards_for_source(p["source_id"], "standard", status=args.status, verified_only=args.verified_only, claim_type=args.claim_type, card_role_filter=args.card_role)
        # User preference: if a paper is deemed useful, provide all claims of that paper.
        # Still allow global token budget as a safety valve.
        kept=[]
        for c in cards:
            cost=estimate_tokens(card_to_brief_text(c))
            if args.token_budget and kept and total_tokens+cost > args.token_budget:
                continue
            kept.append(c); total_tokens += cost
        sections.append({"paper":p, "source_cards":kept, "claims":kept, "claim_count":len(kept)})
    packet={"brief_type":"paper_brief", "query":args.query, "paper_count":len(papers), "estimated_tokens":total_tokens, "papers":sections, "writing_contract":[
        "Paper selection is based on one vector per paper, not one vector per claim.",
        "If a paper is selected, use its claim cards as the relevant evidence inventory.",
        "Do not ask for source context unless a specific claim is central or ambiguous.",
        "Use `context CLAIM_ID --sentence-radius N` to expand locally within the claim paragraph.",
        "Use `source-text SOURCE_ID` only if full-paper reading is explicitly needed."
    ]}
    if args.json:
        print_json(packet); return
    lines=[f"# Paper Brief: {args.query}", "", f"Estimated tokens: {total_tokens}", "", "## Writing contract"]
    for rule in packet["writing_contract"]: lines.append(f"- {rule}")
    for sec in sections:
        p=sec["paper"]
        lines += ["", f"## [{p['score']}] {p['source_id']} — {p.get('title')}", f"Authors/year: {p.get('authors')} ({p.get('year')})", f"Claims: {sec['claim_count']}", ""]
        for c in sec.get("source_cards") or sec.get("claims", []):
            lines.append(f"- **{c['claim_id']}** p.{c.get('page') or '?'} · {c.get('status')} · {c.get('card_role')} · {c.get('claim_type')}: {c.get('claim')}")
            if c.get("evidence"):
                lines.append(f"  - Evidence: {c.get('evidence')}")
        lines.append("")
    print("\n".join(lines))


SECTION_TYPE_CLAIM_TYPES = {
    "background": ["background", "definition", "theoretical claim", "empirical finding"],
    "introduction": ["background", "definition", "theoretical claim", "empirical finding", "limitation"],
    "literature": ["empirical finding", "theoretical claim", "definition", "limitation", "contradiction"],
    "theory": ["theoretical claim", "definition", "methodological claim"],
    "methods": ["methodological claim", "definition", "limitation"],
    "results": ["empirical finding", "methodological claim", "limitation"],
    "discussion": ["empirical finding", "theoretical claim", "policy implication", "limitation", "contradiction"],
    "policy": ["policy implication", "empirical finding", "limitation", "theoretical claim"],
}


def estimate_tokens(text: str) -> int:
    # Simple robust heuristic; good enough for context budgeting.
    return max(1, round(len(text or "") / 4))


def card_to_brief_text(card: dict[str, Any], include_scope: bool = True) -> str:
    parts = [
        f"{card.get('claim_id')} | {card.get('citation_hint')} | p.{card.get('page') or '?'} | {card.get('status')} | {card.get('claim_type')}",
        f"Claim: {card.get('claim')}",
        f"Evidence: {card.get('evidence') or ''}",
    ]
    if include_scope and card.get("scope_note"):
        parts.append(f"Scope: {card.get('scope_note')}")
    return "\n".join(parts)


def build_writing_brief(query: str, *, section_type: str | None = None, limit: int = 12,
                        token_budget: int = 1800, source_id: str | None = None,
                        verified_only: bool = False, status: str | None = None,
                        claim_type: str | None = None, card_role_filter: str | None = None, max_per_source: int = 0) -> dict[str, Any]:
    inferred_types = SECTION_TYPE_CLAIM_TYPES.get(norm(section_type), []) if section_type else []
    requested_types = split_list(claim_type) or inferred_types
    filters = {
        "source_id": source_id,
        "verified_only": verified_only,
        "statuses": split_list(status),
        "claim_types": requested_types,
        "card_roles": split_list(card_role_filter),
    }
    # Retrieve a wider pool, then enforce budget and source diversity.
    pool = retrieve_claims(query, max(limit * 4, 30), "standard", filters)
    selected=[]; used_tokens=0; by_source={}
    for card in pool:
        src=card.get("source_id")
        if max_per_source and by_source.get(src, 0) >= max_per_source:
            continue
        txt=card_to_brief_text(card)
        cost=estimate_tokens(txt)
        if selected and used_tokens + cost > token_budget:
            continue
        selected.append(card)
        used_tokens += cost
        by_source[src]=by_source.get(src,0)+1
        if len(selected) >= limit:
            break
    status_counts={}
    type_counts={}
    source_counts={}
    for c in selected:
        status_counts[c.get("status")]=status_counts.get(c.get("status"),0)+1
        type_counts[c.get("claim_type")]=type_counts.get(c.get("claim_type"),0)+1
        source_counts[c.get("source_id")]=source_counts.get(c.get("source_id"),0)+1
    warnings=[]
    if selected and not status_counts.get("verified"):
        warnings.append("No verified claims in selected packet; treat as drafting evidence until reviewed.")
    if len(selected) < min(limit, 5):
        warnings.append("Few claims selected; retrieve more or broaden the query before drafting a major section.")
    if any(c.get("page") in [None, "", "?"] for c in selected):
        warnings.append("Some claims lack page numbers; char spans remain canonical but page checks may be needed.")
    return {
        "brief_type": "writing_brief",
        "query": query,
        "section_type": section_type,
        "claim_type_filter": requested_types, "card_role_filter": split_list(card_role_filter),
        "token_budget": token_budget,
        "estimated_tokens": used_tokens,
        "selected_count": len(selected),
        "source_counts": source_counts,
        "status_counts": status_counts,
        "claim_type_counts": type_counts,
        "warnings": warnings,
        "writing_contract": [
            "Use only these claim cards unless you explicitly retrieve more evidence.",
            "Every substantive sentence in the draft should map to at least one claim_id.",
            "Do not paste source context into the draft unless needed; use claim evidence first, context only for central/ambiguous claims.",
            "Preserve source scope: geography, method, population/species/case, and uncertainty.",
            "If a claim is central to the paragraph, inspect it with `python rh2.py context CLAIM_ID --window 500` before final use.",
            "Candidate claims are not publication-safe until reviewed."
        ],
        "source_cards": selected, "claims": selected,
    }


def render_writing_brief_md(packet: dict[str, Any]) -> str:
    lines=[
        f"# Writing Brief: {packet.get('query')}", "",
        f"Section type: {packet.get('section_type') or 'unspecified'}", 
        f"Estimated tokens: {packet.get('estimated_tokens')} / {packet.get('token_budget')}",
        f"Claims selected: {packet.get('selected_count')}", "",
        "## Writing contract", ""
    ]
    for rule in packet.get("writing_contract", []):
        lines.append(f"- {rule}")
    if packet.get("warnings"):
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in packet["warnings"]]
    lines += ["", "## Evidence cards", ""]
    for c in packet.get("source_cards") or packet.get("claims", []):
        lines.append(f"### {c.get('claim_id')} — {c.get('citation_hint')} p.{c.get('page') or '?'}")
        lines.append(f"- Status/role/type/grade: `{c.get('status')}` / `{c.get('card_role')}` / `{c.get('claim_type')}` / `{c.get('evidence_grade')}`")
        lines.append(f"- Claim: {c.get('claim')}")
        lines.append(f"- Evidence: {c.get('evidence')}")
        if c.get("scope_note"):
            lines.append(f"- Scope: {c.get('scope_note')}")
        lines.append(f"- Deep dive: `python rh2.py context {c.get('claim_id')} --window 500`")
        lines.append("")
    return "\n".join(lines)


def cmd_writing_brief(args):
    packet=build_writing_brief(
        args.query,
        section_type=args.section_type,
        limit=args.limit,
        token_budget=args.token_budget,
        source_id=args.source_id,
        verified_only=args.verified_only,
        status=args.status,
        claim_type=args.claim_type,
        card_role_filter=args.card_role,
        max_per_source=args.max_per_source,
    )
    if args.out:
        out=Path(args.out)
        out.write_text(json.dumps(packet, ensure_ascii=False, indent=2) if out.suffix.lower()==".json" else render_writing_brief_md(packet), encoding="utf-8")
        print(out)
        return
    if args.json:
        print_json(packet)
    else:
        print(render_writing_brief_md(packet))


# ---------- citation / reference backtracking V0 ----------

def normalize_key(s: str) -> str:
    s = str(s or "").replace("ø", "o").replace("Ø", "O").lower()
    import unicodedata
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))
    s = s.replace("’", "'")
    s = re.sub(r"\bet\s+al\.?", "", s)
    s = re.sub(r"\b(see|cf|e\.g|eg|also|in|by)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s


def compact_key(s: str) -> str:
    return re.sub(r"\s+", "_", normalize_key(s)).strip("_")


def normalize_doi(doi: str | None) -> str:
    if not doi:
        return ""
    d=str(doi).strip()
    d=re.sub(r"^https?://(dx\.)?doi\.org/", "", d, flags=re.I)
    d=re.sub(r"^doi:\s*", "", d, flags=re.I)
    d=d.strip().rstrip(".,);]")
    return d.lower()


def extract_doi_from_text(raw: str) -> str:
    """Extract DOI robustly from reference text with common PDF line-break/OCR spaces."""
    if not raw:
        return ""
    t=str(raw)
    # Repair common broken DOI spacing: "10.1016/j. biocon..." and "10.1186/ s...".
    t=re.sub(r"(10\.\d{4,9}/)\s+", r"\1", t, flags=re.I)
    t=re.sub(r"(10\.1016/j\.)\s+", r"\1", t, flags=re.I)
    t=re.sub(r"(10\.\d{4,9}/[A-Za-z0-9_.-]*\.)\s+(?=[A-Za-z0-9_.-]*\d)", r"\1", t, flags=re.I)
    # Prefer explicit DOI URL or DOI-looking token.
    m=re.search(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", t, flags=re.I)
    if not m:
        return ""
    doi=normalize_doi(m.group(0))
    # Reject obvious partials such as 10.1016/j created by line breaks.
    if len(doi) < 12 or re.fullmatch(r"10\.\d{4,9}/j", doi):
        return ""
    return doi


def canonical_source_id_from_doi(doi: str | None) -> str:
    d=normalize_doi(doi)
    if not d:
        return ""
    return "doi_" + re.sub(r"[^a-z0-9]+", "_", d.lower()).strip("_")


def first_content_word(title: str) -> str:
    stop={"the","a","an","of","on","in","for","to","and","or","with","from","by","at","into","toward","towards"}
    for w in re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9]+", str(title or "")):
        k=compact_key(w)
        if k and k not in stop and not k.isdigit():
            return k
    return "untitled"


def deterministic_source_id(author_key: str, year: str, title: str, doi: str = "") -> str:
    doi_id=canonical_source_id_from_doi(doi)
    if doi_id:
        return doi_id
    year_part=re.match(r"(19|20)\d{2}[a-z]?", str(year or ""))
    year_part=year_part.group(0).lower() if year_part else "nd"
    author=compact_key(author_key) or "unknown"
    word=first_content_word(title)
    return f"ref_{author}_{word}_{year_part}"


def year_token_from_text(s: str) -> str:
    m=re.search(r"\b((?:19|20)\d{2}[a-z]?)\b", str(s or ""), flags=re.I)
    return m.group(1).lower() if m else ""


def year_base(year: str) -> str:
    m=re.match(r"((?:19|20)\d{2})", str(year or ""))
    return m.group(1) if m else str(year or "")[:4]


def author_key_from_author_part(author_part: str) -> str:
    """Stable key for first author / organization.

    Personal refs: "Emmerson, M." -> emmerson.
    Institutional refs/citations: "European Commission" -> european_commission.
    "Pe'er et al." -> pe_er.
    "Official Journal of the European Union" -> official_journal_of_the_european_union.
    """
    s=str(author_part or "").strip()
    s=re.sub(r"<a\s+id=['\"][^'\"]+['\"]\s*>\s*</a>", " ", s, flags=re.I)
    s=re.sub(r"^[\[({\s-]+", "", s)
    s=re.sub(r"\bet\s+al\.?", "", s, flags=re.I)
    s=re.sub(r"\b((?:19|20)\d{2}[a-z]?)\b.*", "", s, flags=re.I).strip(" ,.;()")
    # Split multi-citation connectors but preserve organization names containing 'of'.
    if ";" in s:
        s=s.split(";",1)[0]
    # Personal author references usually have surname comma initials.
    if "," in s:
        first=s.split(",",1)[0].strip()
        # If first segment looks like an organization phrase, keep it; otherwise use surname.
        if len(first.split()) <= 3:
            s=first
    # Remove trailing connectors from in-text fragments.
    s=re.split(r"\s+(?:and|&)\s+", s, flags=re.I)[0].strip() if not re.search(r"\b(of|for|on)\b", s, flags=re.I) else s
    return compact_key(s)


def first_author_key_from_text(s: str) -> str:
    return author_key_from_author_part(s)


def source_author_keys(authors: str) -> set[str]:
    keys=set()
    # semicolon-separated author metadata in our registry is safest; comma splitting is only fallback.
    parts = re.split(r"[;|]", str(authors or "")) if (";" in str(authors or "") or "|" in str(authors or "")) else [str(authors or "")]
    for part in parts:
        k=author_key_from_author_part(part)
        if k:
            keys.add(k)
    return keys


def source_canonical_id(row: sqlite3.Row | dict[str, Any]) -> str:
    d=dict(row)
    doi_id=canonical_source_id_from_doi(d.get("doi"))
    if doi_id:
        return doi_id
    return deterministic_source_id(author_key_from_author_part(d.get("authors","")), str(d.get("year") or ""), d.get("title", ""))


def match_local_source(author_key: str, year: str, excluding_source_id: str | None = None, doi: str | None = None, canonical_source_id: str | None = None) -> str | None:
    conn=db()
    rows=conn.execute("SELECT source_id, authors, year, title, doi FROM sources").fetchall()
    conn.close()
    doi_norm=normalize_doi(doi)
    if doi_norm:
        for r in rows:
            if excluding_source_id and r["source_id"] == excluding_source_id:
                continue
            if normalize_doi(r["doi"]) == doi_norm:
                return r["source_id"]
    if canonical_source_id:
        for r in rows:
            if excluding_source_id and r["source_id"] == excluding_source_id:
                continue
            if r["source_id"] == canonical_source_id or source_canonical_id(r) == canonical_source_id:
                return r["source_id"]
    ybase=year_base(year)
    for r in rows:
        if excluding_source_id and r["source_id"] == excluding_source_id:
            continue
        if ybase and str(r["year"] or "")[:4] != ybase:
            continue
        if author_key and author_key in source_author_keys(r["authors"]):
            return r["source_id"]
        hay=compact_key(" ".join([r["authors"] or "", r["title"] or ""]))
        if author_key and author_key in hay:
            return r["source_id"]
    return None


def references_section(text: str) -> str:
    m = re.search(r"(?im)^#{1,6}\s*(references|bibliography)\s*$", text)
    if not m:
        m = re.search(r"(?im)^\s*(references|bibliography)\s*$", text)
    return text[m.end():] if m else ""


def body_without_references(text: str) -> str:
    """Return source body before References/Bibliography to avoid parsing reference-list entries as narrative citations."""
    m = re.search(r"(?im)^#{1,6}\s*(references|bibliography)\s*$", text)
    if not m:
        m = re.search(r"(?im)^\s*(references|bibliography)\s*$", text)
    return text[:m.start()] if m else text


def clean_reference_raw(raw: str) -> str:
    raw=re.sub(r"<a\s+id=['\"][^'\"]+['\"]\s*>\s*</a>", " ", raw, flags=re.I)
    raw=re.sub(r"\*—\s*Page\s+\d+\s+of\s+\d+\s+—\*", " ", raw)
    raw=re.sub(r"---", " ", raw)
    raw=re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", raw)
    raw=re.sub(r"\s+", " ", raw).strip(" -\t\n")
    return raw


def parse_reference_author_year_title(raw: str) -> tuple[str, str, str, str]:
    year_m=re.search(r"\b((?:19|20)\d{2}[a-z]?)\b", raw, flags=re.I)
    year=year_m.group(1).lower() if year_m else ""
    author_part=raw[:year_m.start()] if year_m else raw
    author_key=author_key_from_author_part(author_part)
    title=""
    if year_m:
        rest=raw[year_m.end():].strip(" .,)–-")
        # APA: title follows year. Elsevier-ish: title follows year after comma/period.
        title=re.sub(r"[*_`]+", "", rest.split(".",1)[0]).strip(" .,-_")[:300]
    return author_key, year, title, author_part.strip()


def parse_reference_lines(text: str, source_id: str) -> list[dict[str, Any]]:
    refs=references_section(text)
    if not refs:
        return []
    rows=[]; seen=set()
    # Prefer markdown references with explicit anchors. These anchors are the only reliable
    # way to couple markdown-linked in-text citations to reference-list entries.
    anchor_pat=re.compile(r"(?ms)^\s*-\s*(?:<a\s+id=['\"]([^'\"]+)['\"]\s*>\s*</a>)?\s*(.*?)(?=^\s*-\s*(?:<a\s+id=|[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+,)|\Z)")
    anchored_blocks=[]; bullet_blocks=[]
    for m in anchor_pat.finditer(refs):
        anchor=m.group(1) or ""
        raw=clean_reference_raw(m.group(2))
        if len(raw) > 20 and re.search(r"\b(?:19|20)\d{2}[a-z]?\b", raw, flags=re.I):
            if anchor:
                anchored_blocks.append((anchor,raw))
            elif len(raw) < 2500:
                bullet_blocks.append(("",raw))
    blocks = anchored_blocks or bullet_blocks
    # Fallback for reference lists without anchors/bullets.
    if not blocks:
        for block in re.split(r"\n\s*\n|\n(?=\s*\[?\d+\]?\s+|\s*[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+,)", refs):
            raw=clean_reference_raw(block)
            if len(raw) > 20 and re.search(r"\b(?:19|20)\d{2}[a-z]?\b", raw, flags=re.I):
                blocks.append(("",raw))
    for anchor, raw in blocks:
        author_key, year, title, author_part = parse_reference_author_year_title(raw)
        doi=extract_doi_from_text(raw)
        canonical=deterministic_source_id(author_key, year, title, doi)
        # Reference instance is stable within the citing source; canonical_source_id is stable globally.
        reference_id=f"REF-{source_id}-{anchor or canonical}"
        key=f"{reference_id}:{canonical}"
        if key in seen:
            continue
        seen.add(key)
        matched=match_local_source(author_key, year, source_id, doi=doi, canonical_source_id=canonical)
        rows.append({"reference_id": reference_id, "source_id": source_id, "reference_anchor": anchor, "raw_text": raw, "author_key": author_key, "year": year, "title": title, "doi": doi, "canonical_source_id": canonical, "matched_source_id": matched, "status": "matched_local" if matched else "missing_source", "created_at": now()})
    return rows


def sentence_context_for_span(text: str, start: int, end: int, max_chars: int = 700) -> tuple[int, int, str]:
    left_candidates=[text.rfind(". ", 0, start), text.rfind("\n\n", 0, start), text.rfind("! ",0,start), text.rfind("? ",0,start)]
    left=max(left_candidates)
    left = 0 if left < 0 else left + 1
    right_candidates=[x for x in [text.find(". ", end), text.find("\n\n", end), text.find("! ",end), text.find("? ",end)] if x != -1]
    right=min(right_candidates)+1 if right_candidates else min(len(text), end+max_chars//2)
    if right-left > max_chars:
        left=max(0, start - max_chars//2)
        right=min(len(text), end + max_chars//2)
    ctx=text[left:right].strip()
    return left, right, ctx


def classify_citation_function(context: str) -> str:
    l=norm(context)
    if any(k in l for k in ["contrary", "in contrast", "however", "whereas", "mixed evidence", "inconsistent"]):
        return "contradiction_or_qualification"
    if any(k in l for k in ["method", "model", "using", "following", "adapted", "framework", "approach"]):
        return "method_or_framework"
    if any(k in l for k in ["consistent with", "in line with", "similarly", "also found", "confirm", "support"]):
        return "supporting_evidence"
    if any(k in l for k in ["review", "literature", "studies", "shown", "found", "reported"]):
        return "background_or_evidence"
    return "background"


def parse_citation_parts(citation_text: str) -> list[tuple[str, str, str]]:
    """Return list of (author_key, year_token, raw_part) from citation label/string."""
    out=[]
    raw=str(citation_text or "").strip()
    inner=raw[1:-1] if raw.startswith("(") and raw.endswith(")") else raw
    parts=re.split(r";", inner)
    for part in parts:
        part=re.sub(r"^\s*(?:i\.e\.|e\.g\.|eg|see|cf\.?)\s*,?\s*", "", part, flags=re.I)
        years=list(re.finditer(r"\b((?:19|20)\d{2}[a-z]?)\b", part, flags=re.I))
        if not years:
            continue
        # Use text before first year for all years in same part, e.g. Cullen et al., 2020, 2021.
        author_part=part[:years[0].start()].strip(" ,;()")
        # Reject numeric/statistical or prose parentheses such as "(34.5% of the total EU annual budget in 2020)"
        # or "(As in the previous study ... 2022b)".
        if re.search(r"[0-9%]", author_part):
            continue
        first_token = re.match(r"\s*([A-Za-zÀ-ÖØ-öø-ÿ]+)", author_part or "")
        if first_token and first_token.group(1)[0].islower() and first_token.group(1).lower() not in {"van", "von", "de", "del", "da", "der"}:
            continue
        if re.match(r"(?i)^(as in|where|when|while|because|although|if)\b", author_part.strip()):
            continue
        if len(re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", author_part)) > 8 and not re.search(r"(?i)(commission|council|parliament|agency|organization|organisation|environment|observatory|journal|union)", author_part):
            continue
        author_key=author_key_from_author_part(author_part)
        if not author_key and out:
            author_key=out[-1][0]
        if not author_key:
            continue
        for ym in years:
            out.append((author_key, ym.group(1).lower(), part.strip()))
    return out


def extract_intext_citations(text: str, source_id: str) -> list[dict[str, Any]]:
    text = body_without_references(text)
    offsets=line_offsets(text)
    candidates=[]
    # Markdown-linked citations: [Author et al., 2020](#ref-001).
    link_re=re.compile(r"\[([^\]]*?\b(?:19|20)\d{2}[a-z]?[^\]]*?)\]\((#[^)]+)\)", flags=re.I)
    for m in link_re.finditer(text):
        label=m.group(1); anchor=m.group(2).lstrip("#")
        parts=parse_citation_parts(label)
        # Usually one reference per markdown link. Keep all parsed years just in case.
        for author_key, year, raw_part in parts:
            cs,ce,ctx=sentence_context_for_span(text, m.start(), m.end())
            candidates.append((m.start(), m.end(), label, author_key, year, anchor, ctx, cs, ce))
    # Parenthetical citations: (Author et al., 2020; Smith, 2021)
    for m in re.finditer(r"\((?:[^()\n]{0,260}?\b(?:19|20)\d{2}[a-z]?[^()\n]{0,180}?)\)", text, flags=re.I):
        citation=m.group(0)
        if not re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]", citation):
            continue
        # Avoid double counting citations already captured as markdown links by checking if span is inside link label? Simple overlap guard later.
        for author_key, year, raw_part in parse_citation_parts(citation):
            cs,ce,ctx=sentence_context_for_span(text, m.start(), m.end())
            candidates.append((m.start(), m.end(), citation, author_key, year, "", ctx, cs, ce))
    # Narrative citations: Smith et al. (2020), Knowler and Bradshaw (2007).
    # Case-sensitive on the leading author to avoid matching fragments like "al. (2021)".
    narrative_re = re.compile(r"\b([A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+(?:\s+(?:and|&)\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+)?(?:\s+et\s+al\.)?)\s*\(\s*((?:19|20)\d{2}[a-z]?)\s*\)")
    for m in narrative_re.finditer(text):
        author_key=author_key_from_author_part(m.group(1))
        year=m.group(2).lower()
        cs,ce,ctx=sentence_context_for_span(text, m.start(), m.end())
        candidates.append((m.start(), m.end(), m.group(0), author_key, year, "", ctx, cs, ce))
    rows=[]; seen=set(); occupied=[]
    for start,end,citation,author_key,year,anchor,ctx,cs,ce in sorted(candidates, key=lambda x:(x[0],x[3],x[4],x[5])):
        # If a plain citation span is fully inside an already captured markdown-link citation, skip it.
        if not anchor and any(os <= start and end <= oe for os,oe in occupied):
            continue
        key=(start,end,author_key,year,anchor)
        if key in seen:
            continue
        seen.add(key)
        if anchor:
            occupied.append((start,end))
        canonical=deterministic_source_id(author_key, year, "")
        matched=match_local_source(author_key, year, source_id, canonical_source_id=canonical)
        cid=f"CITCTX-{source_id}-{len(rows)+1:05d}"
        rows.append({
            "context_id": cid,
            "citing_source_id": source_id,
            "reference_id": None,
            "reference_anchor": anchor,
            "canonical_source_id": canonical,
            "cited_author_key": author_key,
            "cited_year": year,
            "citation_text": citation,
            "char_start": cs,
            "char_end": ce,
            "line_start": line_no(offsets, cs),
            "line_end": line_no(offsets, ce),
            "context_text": ctx,
            "citation_function": classify_citation_function(ctx),
            "matched_source_id": matched,
            "verification_status": "needs_verification" if matched else "missing_source",
            "verification_note": "",
            "created_at": now(),
            "updated_at": now(),
        })
    return rows


def cmd_extract_citations(args):
    init_db(True)
    text=read_source_text(args.source_id)
    refs=parse_reference_lines(text, args.source_id)
    ctxs=extract_intext_citations(text, args.source_id)
    conn=db(); cur=conn.cursor()
    if args.clear:
        cur.execute("DELETE FROM citation_contexts WHERE citing_source_id=?", (args.source_id,))
        cur.execute("DELETE FROM source_references WHERE source_id=?", (args.source_id,))
    # Insert refs and create robust lookups for citation -> reference coupling.
    ref_by_anchor={}
    ref_by_author_year={}
    ref_by_author_baseyear={}
    ref_by_canonical={}
    for r in refs:
        cur.execute("""INSERT OR REPLACE INTO source_references
            (reference_id, source_id, reference_anchor, raw_text, author_key, year, title, doi, canonical_source_id, matched_source_id, status, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r["reference_id"], r["source_id"], r.get("reference_anchor"), r["raw_text"], r["author_key"], r["year"], r["title"], r["doi"], r.get("canonical_source_id"), r["matched_source_id"], r["status"], r["created_at"]))
        if r.get("reference_anchor"):
            ref_by_anchor[r["reference_anchor"]]=r
        ref_by_author_year[(r["author_key"], r["year"])]=r
        ref_by_author_baseyear.setdefault((r["author_key"], year_base(r["year"])), r)
        if r.get("canonical_source_id"):
            ref_by_canonical[r["canonical_source_id"]]=r
    for c in ctxs:
        ref=None
        if c.get("reference_anchor"):
            ref=ref_by_anchor.get(c["reference_anchor"])
        if not ref:
            ref=ref_by_author_year.get((c["cited_author_key"], c["cited_year"]))
        if not ref:
            ref=ref_by_author_baseyear.get((c["cited_author_key"], year_base(c["cited_year"])))
        if ref:
            c["reference_id"]=ref["reference_id"]
            c["canonical_source_id"]=ref.get("canonical_source_id") or c.get("canonical_source_id")
            c["matched_source_id"]=ref.get("matched_source_id") or c.get("matched_source_id")
            c["verification_status"] = "needs_verification" if c.get("matched_source_id") else "missing_source"
        cur.execute("""INSERT OR REPLACE INTO citation_contexts
            (context_id, citing_source_id, reference_id, reference_anchor, canonical_source_id, cited_author_key, cited_year, citation_text, char_start, char_end, line_start, line_end, context_text, citation_function, matched_source_id, verification_status, verification_note, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (c["context_id"], c["citing_source_id"], c.get("reference_id"), c.get("reference_anchor"), c.get("canonical_source_id"), c["cited_author_key"], c["cited_year"], c["citation_text"], c["char_start"], c["char_end"], c["line_start"], c["line_end"], c["context_text"], c["citation_function"], c["matched_source_id"], c["verification_status"], c["verification_note"], c["created_at"], c["updated_at"]))
    conn.commit(); conn.close()
    payload={"source_id": args.source_id, "source_references": len(refs), "citation_contexts": len(ctxs), "matched_contexts": sum(1 for c in ctxs if c["matched_source_id"]), "missing_source_contexts": sum(1 for c in ctxs if not c["matched_source_id"])}
    print_json(payload) if args.json else print(f"Extracted {len(refs)} references and {len(ctxs)} citation contexts for {args.source_id} ({payload['matched_contexts']} matched local sources).")


def cmd_reference_report(args):
    init_db(True)
    conn=db(); cur=conn.cursor()
    where=[]; params=[]
    if args.source_id:
        where.append("source_id=?"); params.append(args.source_id)
    if args.status:
        where.append("status=?"); params.append(args.status)
    sql="SELECT * FROM source_references" + (" WHERE "+" AND ".join(where) if where else "") + " ORDER BY source_id, reference_anchor, reference_id"
    rows=[dict(r) for r in cur.execute(sql, params).fetchall()]
    conn.close()
    if args.json:
        print_json({"count":len(rows),"references":rows[:args.limit]}); return
    print(f"References: {len(rows)}")
    for r in rows[:args.limit]:
        print(f"\n{r['reference_id']} | anchor={r.get('reference_anchor') or '-'} | canonical={r.get('canonical_source_id')} | status={r.get('status')}")
        print(f"author_key={r.get('author_key')} year={r.get('year')} doi={r.get('doi') or '-'} matched={r.get('matched_source_id') or '-'}")
        print(short(r.get('title') or r.get('raw_text'), 260))


def build_citation_summary(source_id: str | None = None, limit: int = 20) -> dict[str, Any]:
    init_db(True)
    conn=db(); cur=conn.cursor()
    where="WHERE citing_source_id=?" if source_id else ""
    params=[source_id] if source_id else []
    rows=[dict(r) for r in cur.execute("SELECT * FROM citation_contexts " + where + " ORDER BY citing_source_id, context_id", params).fetchall()]
    refs=[dict(r) for r in cur.execute("SELECT * FROM source_references " + ("WHERE source_id=?" if source_id else ""), params).fetchall()]
    conn.close()
    status_counts={}; function_counts={}; missing={}; matched={}
    for r in rows:
        status_counts[r["verification_status"]]=status_counts.get(r["verification_status"],0)+1
        function_counts[r["citation_function"]]=function_counts.get(r["citation_function"],0)+1
        key=r.get('canonical_source_id') or f"{r.get('cited_author_key')} {r.get('cited_year')}"
        if r.get("matched_source_id"):
            matched[r["matched_source_id"]]=matched.get(r["matched_source_id"],0)+1
        else:
            missing[key]=missing.get(key,0)+1
    top_missing=sorted(missing.items(), key=lambda x:x[1], reverse=True)[:limit]
    priority=[]
    for r in rows:
        if len(priority) >= limit: break
        if r.get("verification_status") in ["missing_source","needs_source","needs_verification"] or r.get("citation_function") in ["supporting_evidence","contradiction_or_qualification","method_or_framework"]:
            priority.append({"handle": citation_context_handle(r["context_id"]), "context_id": r["context_id"], "citing_source_id": r["citing_source_id"], "cited": r.get("canonical_source_id") or f"{r.get('cited_author_key')} {r.get('cited_year')}", "matched_source_id": r.get("matched_source_id"), "function": r.get("citation_function"), "status": r.get("verification_status"), "context_preview": short(r.get("context_text"), 260)})
    return {"source_id": source_id, "reference_count": len(refs), "citation_context_count": len(rows), "status_counts": status_counts, "function_counts": function_counts, "matched_source_counts": matched, "top_missing_cited_sources": [{"cited":k,"count":v} for k,v in top_missing], "priority_contexts": priority}


def render_citation_summary_md(summary: dict[str, Any]) -> str:
    lines=[f"# Citation Summary: {summary.get('source_id') or 'all sources'}", "", f"References: {summary.get('reference_count')} | Citation contexts: {summary.get('citation_context_count')}", "", "## Status counts", ""]
    for k,v in sorted(summary.get("status_counts",{}).items()): lines.append(f"- {k}: {v}")
    lines += ["", "## Citation function counts", ""]
    for k,v in sorted(summary.get("function_counts",{}).items()): lines.append(f"- {k}: {v}")
    if summary.get("top_missing_cited_sources"):
        lines += ["", "## Top missing cited sources", ""]
        for x in summary["top_missing_cited_sources"]: lines.append(f"- {x['cited']}: {x['count']} context(s)")
    if summary.get("priority_contexts"):
        lines += ["", "## Priority contexts", ""]
        for c in summary["priority_contexts"]:
            lines.append(f"- `{c['handle']}` {c['citing_source_id']} -> {c['cited']} ({c.get('matched_source_id') or 'MISSING'}) | {c['function']} | {c['status']}")
            lines.append(f"  - {c['context_preview']}")
    return "\n".join(lines)


def cmd_citation_summary(args):
    summary=build_citation_summary(args.source_id,args.limit)
    if args.json:
        print_json(summary)
    else:
        print(render_citation_summary_md(summary))


def citation_contexts_for_card(card: dict[str, Any], scope: str = "paragraph") -> list[dict[str, Any]]:
    """Find citation contexts relevant to a source card.

    Default scope is the source card's paragraph. This captures the usual case
    where a sentence/card depends on an in-text citation elsewhere in the same
    paragraph, without dumping the whole paper.
    """
    source_id=card.get("source_id")
    if not source_id or card.get("char_start") is None:
        return []
    start=int(card.get("char_start") or 0); end=int(card.get("char_end") or start)
    if scope == "overlap":
        lo,hi=start,end
    elif scope == "section":
        text=read_source_text(source_id)
        heading=heading_for_char(text,start)
        hs=next((h for h in heading_spans(text) if h["heading"] == heading and h["char_start"] <= start < h["char_end"]), None)
        lo,hi=(hs["char_start"],hs["char_end"]) if hs else paragraph_bounds_around(text,start,end)
    else:
        text=read_source_text(source_id)
        lo,hi=paragraph_bounds_around(text,start,end)
    conn=db()
    rows=[dict(r) for r in conn.execute("""
        SELECT * FROM citation_contexts
        WHERE citing_source_id=?
          AND char_start <= ?
          AND char_end >= ?
        ORDER BY char_start, context_id
    """, (source_id, hi, lo)).fetchall()]
    conn.close()
    return rows


def infer_chapter_citation_role(section: dict[str, Any], card: dict[str, Any], ctx: dict[str, Any]) -> str:
    explicit=section.get("citation_role")
    if explicit:
        return explicit
    text=norm(" ".join([section.get("section_id") or "", section.get("heading") or "", section.get("section_type") or "", section.get("writing_goal") or ""]))
    role=card.get("card_role") or ""
    func=ctx.get("citation_function") or ""
    if "method" in text or role == "method_card" or func == "method_or_framework":
        return "methods_source"
    if any(k in text for k in ["theor", "framework", "concept", "definition"]) or role in ["theory_card", "definition_card"]:
        return "theory_source"
    if any(k in text for k in ["result", "finding", "empirical"]) or role == "result_claim":
        return "empirical_support_source"
    if any(k in text for k in ["policy", "design", "recommend", "discussion"]) or role == "policy_design_card":
        return "policy_design_source"
    if "gap" in text or role == "limitation_card":
        return "limitation_or_gap_source"
    if func == "contradiction_or_qualification" or role == "contradiction_card":
        return "contradiction_or_qualification_source"
    return "background_source"


def build_chapter_citation_backtracking(profile: str, default_limit: int = 10, context_scope: str = "paragraph") -> dict[str, Any]:
    prof=load_chapter_profile(profile)
    chapter_id, sections, used = chapter_sections_with_cards(prof, default_limit, "full")
    upstream={}
    section_outputs=[]
    for sec in sections:
        sec_contexts=[]
        sec_seen=set()
        for card in sec.get("source_cards", []):
            ctxs=citation_contexts_for_card(card, context_scope)
            for ctx in ctxs:
                canonical=ctx.get("canonical_source_id") or f"{ctx.get('cited_author_key')} {ctx.get('cited_year')}"
                chap_role=infer_chapter_citation_role(sec, card, ctx)
                dedup_key=(sec.get("section_id") or sec.get("heading"), ctx.get("context_id"), canonical, chap_role)
                if dedup_key in sec_seen:
                    continue
                sec_seen.add(dedup_key)
                rec={
                    "chapter_section_id": sec.get("section_id"),
                    "chapter_heading": sec.get("heading"),
                    "chapter_section_type": sec.get("section_type"),
                    "chapter_citation_role": chap_role,
                    "source_card_id": card.get("claim_id"),
                    "source_card_role": card.get("card_role"),
                    "source_card_text": short(card.get("claim"), 220),
                    "citing_source_id": ctx.get("citing_source_id"),
                    "citation_context_id": ctx.get("context_id"),
                    "citation_context_handle": citation_context_handle(ctx.get("context_id")),
                    "reference_id": ctx.get("reference_id"),
                    "reference_anchor": ctx.get("reference_anchor"),
                    "canonical_source_id": canonical,
                    "matched_source_id": ctx.get("matched_source_id"),
                    "cited_author_key": ctx.get("cited_author_key"),
                    "cited_year": ctx.get("cited_year"),
                    "citation_function": ctx.get("citation_function"),
                    "verification_status": ctx.get("verification_status"),
                    "context_preview": short(ctx.get("context_text"), 320),
                }
                sec_contexts.append(rec)
                agg=upstream.setdefault(canonical, {"canonical_source_id": canonical, "matched_source_id": ctx.get("matched_source_id"), "count":0, "chapter_roles": {}, "sections": {}, "source_cards": {}, "citation_functions": {}, "verification_statuses": {}, "contexts": []})
                agg["count"] += 1
                agg["chapter_roles"][chap_role]=agg["chapter_roles"].get(chap_role,0)+1
                sid=sec.get("section_id") or sec.get("heading") or "unknown_section"
                agg["sections"][sid]=agg["sections"].get(sid,0)+1
                agg["source_cards"][card.get("claim_id")]=agg["source_cards"].get(card.get("claim_id"),0)+1
                agg["citation_functions"][ctx.get("citation_function")]=agg["citation_functions"].get(ctx.get("citation_function"),0)+1
                agg["verification_statuses"][ctx.get("verification_status")]=agg["verification_statuses"].get(ctx.get("verification_status"),0)+1
                if len(agg["contexts"]) < 8:
                    agg["contexts"].append(rec)
        section_outputs.append({"section_id": sec.get("section_id"), "heading": sec.get("heading"), "section_type": sec.get("section_type"), "query": sec.get("query"), "source_card_count": len(sec.get("source_cards", [])), "citation_context_count": len(sec_contexts), "citation_contexts": sec_contexts})
    top_upstream=sorted(upstream.values(), key=lambda x:x["count"], reverse=True)
    warnings=[]
    if not top_upstream:
        warnings.append("No citation contexts found around chapter-selected source cards. Use --context-scope section or extract citations for the involved papers.")
    missing=sum(1 for u in top_upstream if not u.get("matched_source_id"))
    if missing:
        warnings.append(f"{missing} upstream cited sources are not available locally; citation verification will require importing or resolving those sources.")
    return {"chapter_id": chapter_id, "chapter_title": prof.get("chapter_title"), "profile": profile, "context_scope": context_scope, "selected_source_card_count": len(used), "upstream_source_count": len(top_upstream), "warnings": warnings, "upstream_sources": top_upstream, "sections": section_outputs}


def render_chapter_citations_md(packet: dict[str, Any]) -> str:
    lines=[f"# Chapter Citation Backtracking: {packet.get('chapter_title') or packet.get('chapter_id')}", "", f"Context scope: `{packet.get('context_scope')}`", f"Selected source cards: {packet.get('selected_source_card_count')}", f"Upstream cited sources: {packet.get('upstream_source_count')}", ""]
    if packet.get("warnings"):
        lines += ["## Warnings", ""] + [f"- {w}" for w in packet["warnings"]] + [""]
    lines += ["## Upstream sources by chapter role", ""]
    for u in packet.get("upstream_sources", []):
        role_str=", ".join(f"{k}:{v}" for k,v in sorted(u.get("chapter_roles",{}).items(), key=lambda x:-x[1]))
        sec_str=", ".join(f"{k}:{v}" for k,v in sorted(u.get("sections",{}).items(), key=lambda x:-x[1]))
        status_str=", ".join(f"{k}:{v}" for k,v in sorted(u.get("verification_statuses",{}).items(), key=lambda x:-x[1]))
        lines.append(f"### {u['canonical_source_id']} ({u.get('matched_source_id') or 'MISSING'})")
        lines.append(f"- Count: {u['count']}")
        lines.append(f"- Chapter roles: {role_str}")
        lines.append(f"- Sections: {sec_str}")
        lines.append(f"- Verification: {status_str}")
        if u.get("contexts"):
            lines.append("- Example contexts:")
            for c in u["contexts"][:3]:
                lines.append(f"  - `{c['citation_context_handle']}` via `{c['source_card_id']}` in {c.get('chapter_section_id')}: {c['context_preview']}")
        lines.append("")
    lines += ["## Section-level citation contexts", ""]
    for sec in packet.get("sections", []):
        lines.append(f"### {sec.get('heading') or sec.get('section_id')}")
        lines.append(f"- Source cards: {sec.get('source_card_count')} | Citation contexts: {sec.get('citation_context_count')}")
        for c in sec.get("citation_contexts", [])[:10]:
            lines.append(f"  - `{c['citation_context_handle']}` {c['canonical_source_id']} → role `{c['chapter_citation_role']}` via `{c['source_card_id']}`")
        lines.append("")
    return "\n".join(lines)


def cmd_chapter_citations(args):
    packet=build_chapter_citation_backtracking(args.profile, args.limit, args.context_scope)
    if args.out:
        out=Path(args.out)
        out.write_text(json.dumps(packet, ensure_ascii=False, indent=2) if out.suffix.lower()==".json" else render_chapter_citations_md(packet), encoding="utf-8")
        print(out); return
    if args.json:
        print_json(packet)
    else:
        print(render_chapter_citations_md(packet))


def cmd_citation_report(args):
    init_db(True)
    conn=db(); cur=conn.cursor()
    where=[]; params=[]
    if args.source_id:
        where.append("citing_source_id=?"); params.append(args.source_id)
    if args.status:
        where.append("verification_status=?"); params.append(args.status)
    sql="SELECT * FROM citation_contexts" + (" WHERE "+" AND ".join(where) if where else "") + " ORDER BY citing_source_id, context_id"
    rows=[dict(r) for r in cur.execute(sql, params).fetchall()]
    conn.close()
    counts={}
    functions={}
    for r in rows:
        counts[r["verification_status"]]=counts.get(r["verification_status"],0)+1
        functions[r["citation_function"]]=functions.get(r["citation_function"],0)+1
    if args.json:
        print_json({"count": len(rows), "status_counts": counts, "function_counts": functions, "contexts": rows[:args.limit]})
        return
    print(f"Citation contexts: {len(rows)}")
    print("Status:", counts)
    print("Functions:", functions)
    for r in rows[:args.limit]:
        target=r.get("matched_source_id") or "MISSING"
        cited = r.get('canonical_source_id') or f"{r['cited_author_key']} {r['cited_year']}"
        print(f"\n{r['context_id']} | {r['citing_source_id']} -> {cited} ({target}) | {r['citation_function']} | {r['verification_status']}")
        print(short(r['context_text'], 320))


def cmd_citation_context(args):
    init_db(True)
    conn=db(); row=conn.execute("SELECT * FROM citation_contexts WHERE context_id=?", (args.context_id,)).fetchone(); conn.close()
    if not row:
        raise SystemExit(f"Unknown context_id: {args.context_id}")
    r=dict(row)
    payload={"context": r, "target_claim_suggestions": []}
    if r.get("matched_source_id"):
        payload["target_claim_suggestions"] = retrieve_claims(r.get("context_text", ""), args.limit, "standard", {"source_id": r["matched_source_id"]})
    if args.json:
        print_json(payload); return
    cited = r.get('canonical_source_id') or f"{r['cited_author_key']} {r['cited_year']}"
    print(f"{r['context_id']} | {r['citing_source_id']} cites {cited} -> {r.get('matched_source_id') or 'MISSING'}")
    print(f"Function: {r['citation_function']} | Status: {r['verification_status']}")
    print("\n--- citing context ---")
    print(r["context_text"])
    if r.get("matched_source_id"):
        print("\n--- candidate claims from cited source ---")
        for c in payload["target_claim_suggestions"]:
            print(f"- {c['claim_id']} [{c['score']}] {c['claim']}")
            if c.get("evidence"):
                print(f"  evidence: {short(c['evidence'], 220)}")
    else:
        print("\nNo local source available for cited paper. Add/import the cited paper, then rerun extract-citations.")


def cmd_verify_citation(args):
    conn=db(); row=conn.execute("SELECT context_id FROM citation_contexts WHERE context_id=?", (args.context_id,)).fetchone()
    if not row:
        raise SystemExit(f"Unknown context_id: {args.context_id}")
    conn.execute("UPDATE citation_contexts SET verification_status=?, verification_note=?, updated_at=? WHERE context_id=?", (args.status, args.note or "", now(), args.context_id))
    conn.commit(); conn.close()
    print(f"{args.context_id} -> {args.status}")


def update_claim_evidence(cur: sqlite3.Cursor, claim_id: str, evidence: str) -> None:
    row = cur.execute("SELECT claim, scope_note FROM source_cards WHERE claim_id=?", (claim_id,)).fetchone()
    if not row:
        return
    tags = " ".join(f"{r['tag_type']}:{r['tag']}" for r in cur.execute("SELECT tag_type, tag FROM claim_tags WHERE claim_id=?", (claim_id,)).fetchall())
    cur.execute("UPDATE source_cards SET evidence=?, updated_at=? WHERE claim_id=?", (evidence, now(), claim_id))
    cur.execute("DELETE FROM claims_fts WHERE claim_id=?", (claim_id,))
    cur.execute("INSERT INTO claims_fts(claim_id, claim, evidence, tags) VALUES (?,?,?,?)", (claim_id, row["claim"], evidence, tags))


def cmd_repair_evidence(args):
    """Repair overlong legacy evidence quotes.

    Main use-case: V1 fallback anchors sometimes pointed to a whole chunk; early V2 import then
    copied that chunk as evidence. This command restores tight evidence quotes from the V1 ledger
    and/or trims noisy annotated evidence to 1-2 supporting sentences.
    """
    init_db(True)
    legacy = {}
    if args.from_v1:
        ledger = Path(args.from_v1) / "claim_ledger.jsonl"
        if not ledger.exists():
            raise SystemExit(f"No V1 claim_ledger.jsonl found at {ledger}")
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if line.strip():
                c=json.loads(line)
                if c.get("claim_id"):
                    legacy[c["claim_id"]] = c
    conn=db(); cur=conn.cursor(); rows=cur.execute("SELECT claim_id, claim, evidence FROM source_cards ORDER BY claim_id").fetchall()
    changes=[]
    for r in rows:
        cid=r["claim_id"]; old_ev=r["evidence"] or ""; claim=r["claim"] or ""
        source_ev = old_ev
        if cid in legacy and legacy[cid].get("evidence"):
            # Prefer curated V1 evidence whenever current evidence is noisy/longer.
            candidate = refine_evidence_quote(legacy[cid].get("evidence", ""), claim, args.max_chars)
            if len(old_ev) > args.max_chars or "[PAGE" in old_ev or "#MA/" in old_ev or "==" in old_ev or len(candidate) < len(old_ev):
                source_ev = candidate
        else:
            source_ev = refine_evidence_quote(old_ev, claim, args.max_chars)
        if source_ev.strip() != old_ev.strip():
            changes.append({"claim_id": cid, "old_len": len(old_ev), "new_len": len(source_ev), "old_preview": short(old_ev, 120), "new_preview": short(source_ev, 160)})
            if not args.dry_run:
                update_claim_evidence(cur, cid, source_ev)
    if not args.dry_run:
        conn.commit()
    conn.close()
    payload={"dry_run": args.dry_run, "changed": len(changes), "changes": changes[:args.limit]}
    print_json(payload) if args.json else print(f"Evidence repair {'dry-run ' if args.dry_run else ''}changed {len(changes)} claims")


CLAIM_ID_RE = re.compile(r"\bCLM-[A-Za-z0-9_.-]+-\d{4,}\b")


def draft_claim_ids(text: str) -> list[str]:
    seen=[]
    for m in CLAIM_ID_RE.finditer(text):
        cid=m.group(0)
        if cid not in seen:
            seen.append(cid)
    return seen


def substantive_sentences_without_claim_ids(text: str, min_words: int=14) -> list[dict[str, Any]]:
    cleaned=re.sub(r"```[\s\S]*?```", " ", text)
    cleaned=re.sub(r"<!--([\s\S]*?)-->", " ", cleaned)
    sentences=re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"“])", cleaned)
    out=[]
    for idx, sent in enumerate(sentences, 1):
        s=sent.strip()
        if not s or CLAIM_ID_RE.search(s):
            continue
        words=re.findall(r"\b[A-Za-z][A-Za-z-]+\b", s)
        if len(words) >= min_words:
            out.append({"sentence_no": idx, "word_count": len(words), "text": short(s, 320)})
    return out


def cmd_audit_draft(args):
    text=Path(args.path).read_text(encoding="utf-8", errors="ignore")
    ids=draft_claim_ids(text)
    conn=db()
    known=[]; unknown=[]
    for cid in ids:
        row=conn.execute("SELECT * FROM source_cards WHERE claim_id=?", (cid,)).fetchone()
        if row:
            card=claim_card(row, "standard")
            card["relations"] = claim_relations_for(cid)
            known.append(card)
        else:
            unknown.append(cid)
    status_counts=collections.Counter(c["status"] for c in known)
    grade_counts=collections.Counter(c["evidence_grade"] for c in known)
    source_counts=collections.Counter(c["source_id"] for c in known)
    flagged=[]
    for c in known:
        if c["status"] != "verified":
            flagged.append({"claim_id": c["claim_id"], "issue": "not_verified", "status": c["status"], "grade": c["evidence_grade"]})
        if c["evidence_grade"] in {"C", "D", "X"}:
            flagged.append({"claim_id": c["claim_id"], "issue": "weak_evidence_grade", "status": c["status"], "grade": c["evidence_grade"]})
    uncited=substantive_sentences_without_claim_ids(text, args.min_words)
    payload={
        "draft": str(args.path),
        "claim_ids_found": ids,
        "known_claims": len(known),
        "unknown_claim_ids": unknown,
        "status_counts": dict(status_counts),
        "evidence_grade_counts": dict(grade_counts),
        "source_counts": dict(source_counts),
        "flagged_claims": flagged,
        "uncited_substantive_sentences": uncited[:args.show_uncited],
        "uncited_substantive_sentence_count": len(uncited),
        "recommendations": []
    }
    if unknown:
        payload["recommendations"].append("Resolve unknown claim IDs or remove stale references.")
    if status_counts.get("verified",0) < len(known):
        payload["recommendations"].append("Review candidate/page-check/source-check claims before final submission.")
    if len(source_counts) < args.min_sources and known:
        payload["recommendations"].append(f"Increase source diversity: {len(source_counts)} source(s), target {args.min_sources}.")
    if uncited:
        payload["recommendations"].append("Attach claim IDs to substantive draft sentences or mark them as interpretation.")
    conn.close()
    if args.json:
        print_json(payload)
    else:
        print(f"Draft: {args.path}")
        print(f"Known claim IDs: {len(known)} | unknown: {len(unknown)}")
        print(f"Statuses: {dict(status_counts)}")
        print(f"Evidence grades: {dict(grade_counts)}")
        print(f"Sources: {dict(source_counts)}")
        if flagged:
            print("\nFlagged claims:")
            for f in flagged[:40]: print(f"- {f['claim_id']}: {f['issue']} ({f['status']}, grade {f['grade']})")
        if unknown:
            print("\nUnknown claim IDs:"); [print(f"- {cid}") for cid in unknown]
        if uncited:
            print(f"\nUncited substantive sentences: {len(uncited)}")
            for u in uncited[:args.show_uncited]: print(f"- [{u['sentence_no']}] {u['text']}")
        if payload["recommendations"]:
            print("\nRecommendations:"); [print(f"- {x}") for x in payload["recommendations"]]


def cmd_relate(args):
    if args.claim_a == args.claim_b:
        raise SystemExit("Cannot relate a claim to itself.")
    conn=db()
    for cid in [args.claim_a, args.claim_b]:
        if not conn.execute("SELECT 1 FROM source_cards WHERE claim_id=?", (cid,)).fetchone():
            raise SystemExit(f"Unknown claim_id: {cid}")
    rid=args.relation_id or f"REL-{sha1_short(args.claim_a + args.claim_b + args.relation_type)[:10]}"
    conn.execute("""INSERT OR REPLACE INTO claim_relations VALUES (?,?,?,?,?,?,?)""", (
        rid, args.claim_a, args.claim_b, args.relation_type, args.note or "", args.status or "candidate_needs_review", now()
    ))
    conn.commit(); conn.close()
    print(f"{rid}: {args.claim_a} --{args.relation_type}--> {args.claim_b}")


def cmd_relations(args):
    conn=db()
    params=[]; where="1=1"
    if args.claim_id:
        where="(claim_a=? OR claim_b=?)"; params=[args.claim_id,args.claim_id]
    if args.relation_type:
        where += " AND relation_type=?"; params.append(args.relation_type)
    rows=conn.execute(f"SELECT * FROM claim_relations WHERE {where} ORDER BY created_at DESC LIMIT ?", (*params,args.limit)).fetchall()
    payload=[dict(r) for r in rows]
    conn.close()
    if args.json: print_json(payload)
    else:
        for r in payload:
            print(f"{r['relation_id']} | {r['claim_a']} --{r['relation_type']}--> {r['claim_b']} | {r['status']} | {r.get('note','')}")


def cmd_evidence_grades(args):
    conn=db(); rows=conn.execute("SELECT * FROM source_cards ORDER BY source_id, claim_id").fetchall(); conn.close()
    items=[]
    for r in rows:
        g=evidence_grade(r)
        if args.grade and g != args.grade: continue
        items.append({"claim_id": r["claim_id"], "grade": g, "status": r["verification_status"], "page_status": r["page_status"], "claim_representation": r["claim_representation"], "claim": short(r["claim"], 180)})
    if args.json: print_json(items)
    else:
        counts=collections.Counter(x["grade"] for x in items)
        print(f"Evidence grades: {dict(counts)}")
        for x in items[:args.limit]: print(f"{x['claim_id']} grade:{x['grade']} {x['status']} {x['claim']}")


def cmd_stats(args):
    conn=db(); cur=conn.cursor()
    tables=["sources","spans","source_cards","claim_tags","claim_relations","source_references","citation_contexts","review_events"]
    stats={}
    for t in tables:
        try: stats[t]=cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception: stats[t]="missing"
    stats["db_bytes"]=DB_PATH.stat().st_size if DB_PATH.exists() else 0
    stats["blob_bytes"]=sum(p.stat().st_size for p in BLOBS.glob("*.gz"))
    conn.close()
    print_json(stats) if args.json else [print(f"{k}: {v}") for k,v in stats.items()]


def cmd_review(args):
    conn=db(); row=conn.execute("SELECT verification_status FROM source_cards WHERE claim_id=?", (args.claim_id,)).fetchone()
    if not row: raise SystemExit(f"Unknown claim_id: {args.claim_id}")
    old=row["verification_status"]
    conn.execute("UPDATE source_cards SET verification_status=?, updated_at=? WHERE claim_id=?", (args.status, now(), args.claim_id))
    conn.execute("INSERT INTO review_events VALUES (?,?,?,?,?,?,?)", (str(uuid.uuid4()), args.claim_id, old, args.status, args.note or "", args.actor or "human", now()))
    conn.commit(); conn.close(); print(f"{args.claim_id}: {old} -> {args.status}")


def main():
    ensure_dirs()
    p=argparse.ArgumentParser(description="Research Harness V2")
    sub=p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(func=cmd_init)
    ing=sub.add_parser("ingest"); ing.add_argument("path"); ing.add_argument("--source-id"); ing.add_argument("--title"); ing.add_argument("--authors",default=""); ing.add_argument("--year",default=""); ing.add_argument("--doi",default=""); ing.add_argument("--source-type",default="peer-reviewed article"); ing.add_argument("--disciplines",default=""); ing.add_argument("--geography",default=""); ing.add_argument("--methodology",default=""); ing.add_argument("--theory",default=""); ing.add_argument("--quality",default="unrated"); ing.add_argument("--notes",default=""); ing.set_defaults(func=cmd_ingest)
    imp=sub.add_parser("import-v1"); imp.add_argument("v1_path"); imp.set_defaults(func=cmd_import_v1)
    mark=sub.add_parser("mark-claim"); mark.add_argument("quote", nargs="?"); mark.add_argument("--text"); mark.add_argument("--source-id"); mark.add_argument("--claim-id"); mark.add_argument("--claim"); mark.add_argument("--claim-representation", choices=["source_quote","lightly_normalized_source","paraphrase","source_range"]); mark.add_argument("--claim-type", choices=sorted(CLAIM_TYPES)); mark.add_argument("--constructs"); mark.add_argument("--rq-tags"); mark.add_argument("--discipline"); mark.add_argument("--geography"); mark.add_argument("--methodology"); mark.add_argument("--scope-note"); mark.add_argument("--confidence", choices=["low","medium","high"], default="high"); mark.add_argument("--status", choices=sorted(STATUSES), default="candidate_needs_review"); mark.add_argument("--allow-duplicate", action="store_true"); mark.add_argument("--fields", choices=["minimal","standard","full"], default="standard"); mark.add_argument("--json", action="store_true"); mark.set_defaults(func=cmd_mark_claim)
    ret=sub.add_parser("retrieve"); ret.add_argument("query"); ret.add_argument("--limit", type=int, default=8); ret.add_argument("--source-id"); ret.add_argument("--verified-only", action="store_true"); ret.add_argument("--status"); ret.add_argument("--claim-type"); ret.add_argument("--card-role", help="Filter by card role, e.g. result_claim, method_card, background_card"); ret.add_argument("--fields", choices=["minimal","standard","full"], default="minimal"); ret.add_argument("--json", action="store_true"); ret.set_defaults(func=cmd_retrieve)
    ctx=sub.add_parser("context"); ctx.add_argument("claim_id"); ctx.add_argument("--mode", choices=["sentence","char","full"], default="sentence"); ctx.add_argument("--sentence-radius", type=int, default=1, help="Sentence radius around the claim sentence within the claim paragraph"); ctx.add_argument("--outside-paragraph", action="store_true", help="Allow sentence expansion into neighbouring paragraphs"); ctx.add_argument("--window", type=int, default=500, help="Character window for --mode char only"); ctx.add_argument("--fields", choices=["minimal","standard","full"], default="standard"); ctx.add_argument("--json", action="store_true"); ctx.set_defaults(func=cmd_context)
    stxt=sub.add_parser("source-text", help="Read an entire source text, a truncated prefix, or an exact char range")
    stxt.add_argument("source_id"); stxt.add_argument("--range", help="Character range START:END or START-END"); stxt.add_argument("--max-chars", type=int, default=0); stxt.add_argument("--json", action="store_true"); stxt.set_defaults(func=cmd_source_text)
    smap=sub.add_parser("source-map", help="Compressed map of source sections with retrieval handles, claim counts, citation counts")
    smap.add_argument("source_id"); smap.add_argument("--max-claims-per-section", type=int, default=5); smap.add_argument("--json", action="store_true"); smap.set_defaults(func=cmd_source_map)
    rh=sub.add_parser("resolve-handle", help="Resolve SOURCE_RANGE[], CLAIM[], CITATION_CONTEXT[], or PAPER[] handles")
    rh.add_argument("handle"); rh.add_argument("--sentence-radius", type=int, default=1); rh.add_argument("--outside-paragraph", action="store_true"); rh.add_argument("--max-chars", type=int, default=0); rh.add_argument("--json", action="store_true"); rh.set_defaults(func=cmd_resolve_handle)
    mspan=sub.add_parser("mark-span", help="Create a pure source-range claim from source_id and char_start/char_end")
    mspan.add_argument("source_id"); mspan.add_argument("char_start", type=int); mspan.add_argument("char_end", type=int); mspan.add_argument("--claim-id"); mspan.add_argument("--claim-type", choices=sorted(CLAIM_TYPES)); mspan.add_argument("--constructs"); mspan.add_argument("--rq-tags"); mspan.add_argument("--discipline"); mspan.add_argument("--geography"); mspan.add_argument("--methodology"); mspan.add_argument("--scope-note"); mspan.add_argument("--confidence", choices=["low","medium","high"], default="high"); mspan.add_argument("--status", choices=sorted(STATUSES), default="candidate_needs_review"); mspan.add_argument("--allow-duplicate", action="store_true"); mspan.add_argument("--fields", choices=["minimal","standard","full"], default="standard"); mspan.add_argument("--json", action="store_true"); mspan.set_defaults(func=cmd_mark_span)
    esr=sub.add_parser("extract-source-ranges", help="Heuristically extract exact source-range claim candidates; no LLM rewriting")
    esr.add_argument("source_id"); esr.add_argument("--min-score", type=float, default=2.0); esr.add_argument("--limit", type=int, default=40); esr.add_argument("--dry-run", action="store_true"); esr.add_argument("--allow-duplicate", action="store_true"); esr.add_argument("--status", choices=sorted(STATUSES), default="candidate_needs_review"); esr.add_argument("--rq-tags"); esr.add_argument("--json", action="store_true"); esr.set_defaults(func=cmd_extract_source_ranges)
    bpi=sub.add_parser("build-paper-index", help="Build one vector per paper/source")
    bpi.add_argument("--max-features", type=int, default=30000); bpi.set_defaults(func=cmd_build_paper_index)
    ps=sub.add_parser("paper-search", help="Rank papers by one vector per paper")
    ps.add_argument("query"); ps.add_argument("--limit", type=int, default=5); ps.add_argument("--json", action="store_true"); ps.set_defaults(func=cmd_paper_search)
    pb=sub.add_parser("paper-brief", help="Rank papers by paper vector, then provide claim inventory for selected papers")
    pb.add_argument("query"); pb.add_argument("--paper-limit", type=int, default=3); pb.add_argument("--token-budget", type=int, default=6000); pb.add_argument("--verified-only", action="store_true"); pb.add_argument("--status"); pb.add_argument("--claim-type"); pb.add_argument("--card-role", help="Filter by card role, e.g. result_claim, method_card, background_card"); pb.add_argument("--json", action="store_true"); pb.set_defaults(func=cmd_paper_brief)
    chap=sub.add_parser("chapter-brief"); chap.add_argument("profile"); chap.add_argument("--limit", type=int, default=10); chap.add_argument("--json", action="store_true"); chap.set_defaults(func=cmd_chapter_brief)
    wb=sub.add_parser("writing-brief", help="Build a compact, budgeted evidence packet for an LLM/human writing a paragraph/section")
    wb.add_argument("query"); wb.add_argument("--section-type", choices=sorted(SECTION_TYPE_CLAIM_TYPES.keys())); wb.add_argument("--limit", type=int, default=10); wb.add_argument("--token-budget", type=int, default=1800); wb.add_argument("--source-id"); wb.add_argument("--verified-only", action="store_true"); wb.add_argument("--status"); wb.add_argument("--claim-type"); wb.add_argument("--card-role", help="Filter by writing role, e.g. result_claim or policy_design_card"); wb.add_argument("--max-per-source", type=int, default=0); wb.add_argument("--json", action="store_true"); wb.add_argument("--out"); wb.set_defaults(func=cmd_writing_brief)
    rev=sub.add_parser("review"); rev.add_argument("claim_id"); rev.add_argument("status", choices=sorted(STATUSES)); rev.add_argument("--note"); rev.add_argument("--actor"); rev.set_defaults(func=cmd_review)
    rel=sub.add_parser("relate", help="Create or update a relation between two claims")
    rel.add_argument("claim_a"); rel.add_argument("claim_b"); rel.add_argument("relation_type", choices=sorted(RELATION_TYPES)); rel.add_argument("--relation-id"); rel.add_argument("--note"); rel.add_argument("--status", choices=sorted(STATUSES), default="candidate_needs_review"); rel.set_defaults(func=cmd_relate)
    rels=sub.add_parser("relations", help="List claim relations / tension-map edges")
    rels.add_argument("--claim-id"); rels.add_argument("--relation-type", choices=sorted(RELATION_TYPES)); rels.add_argument("--limit", type=int, default=50); rels.add_argument("--json", action="store_true"); rels.set_defaults(func=cmd_relations)
    aud=sub.add_parser("audit-draft", help="Audit a markdown draft for claim-ID traceability and weak evidence")
    aud.add_argument("path"); aud.add_argument("--min-words", type=int, default=14); aud.add_argument("--show-uncited", type=int, default=20); aud.add_argument("--min-sources", type=int, default=3); aud.add_argument("--json", action="store_true"); aud.set_defaults(func=cmd_audit_draft)
    grd=sub.add_parser("evidence-grades", help="List computed claim evidence grades")
    grd.add_argument("--grade", choices=sorted(EVIDENCE_GRADES)); grd.add_argument("--limit", type=int, default=80); grd.add_argument("--json", action="store_true"); grd.set_defaults(func=cmd_evidence_grades)
    ec=sub.add_parser("extract-citations", help="Extract reference-list entries and in-text citation contexts for backtracking")
    ec.add_argument("source_id"); ec.add_argument("--clear", action="store_true"); ec.add_argument("--json", action="store_true"); ec.set_defaults(func=cmd_extract_citations)
    rr=sub.add_parser("reference-report", help="List parsed reference-list entries with stable canonical IDs")
    rr.add_argument("--source-id"); rr.add_argument("--status"); rr.add_argument("--limit", type=int, default=50); rr.add_argument("--json", action="store_true"); rr.set_defaults(func=cmd_reference_report)
    chcit=sub.add_parser("chapter-citations", help="Chapter-aware citation backtracking: classify upstream sources by chapter section/card role")
    chcit.add_argument("profile"); chcit.add_argument("--limit", type=int, default=10); chcit.add_argument("--context-scope", choices=["overlap","paragraph","section"], default="paragraph"); chcit.add_argument("--json", action="store_true"); chcit.add_argument("--out"); chcit.set_defaults(func=cmd_chapter_citations)
    csum=sub.add_parser("citation-summary", help="Compressed citation/backtracking summary with handles")
    csum.add_argument("--source-id"); csum.add_argument("--limit", type=int, default=20); csum.add_argument("--json", action="store_true"); csum.set_defaults(func=cmd_citation_summary)
    cr=sub.add_parser("citation-report", help="Summarize extracted citation contexts")
    cr.add_argument("--source-id"); cr.add_argument("--status"); cr.add_argument("--limit", type=int, default=30); cr.add_argument("--json", action="store_true"); cr.set_defaults(func=cmd_citation_report)
    cc=sub.add_parser("citation-context", help="Show one citation context and suggested claims from the cited source if available")
    cc.add_argument("context_id"); cc.add_argument("--limit", type=int, default=5); cc.add_argument("--json", action="store_true"); cc.set_defaults(func=cmd_citation_context)
    vc=sub.add_parser("verify-citation", help="Mark a citation context as accurate/inaccurate/missing/etc.")
    vc.add_argument("context_id"); vc.add_argument("status", choices=["verified_accurate","verified_inaccurate","misleading","missing_source","needs_source","needs_context","not_relevant"]); vc.add_argument("--note"); vc.set_defaults(func=cmd_verify_citation)
    rep=sub.add_parser("repair-evidence", help="Repair overlong/noisy evidence quotes, optionally using a V1 ledger as source of curated quotes"); rep.add_argument("--from-v1"); rep.add_argument("--max-chars", type=int, default=700); rep.add_argument("--dry-run", action="store_true"); rep.add_argument("--json", action="store_true"); rep.add_argument("--limit", type=int, default=30); rep.set_defaults(func=cmd_repair_evidence)
    st=sub.add_parser("stats"); st.add_argument("--json", action="store_true"); st.set_defaults(func=cmd_stats)
    args=p.parse_args(); args.func(args)

if __name__ == "__main__":
    main()
