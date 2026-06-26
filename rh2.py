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

import argparse, collections, csv, gzip, hashlib, json, re, shutil, sqlite3, subprocess, sys, textwrap, uuid
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
RELATION_TYPES = {"supports", "contradicts", "qualifies", "same_concept", "duplicate_of", "methodologically_incompatible", "stronger_than", "weaker_than", "supersedes"}
REVIEW_LABELS = {"good_claim", "excellent", "too_broad", "too_narrow", "not_substantive", "bad_evidence", "needs_split", "duplicate", "scope_overreach", "method_only", "background_only", "page_verified", "source_verified", "superseded", "revised"}
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
    CREATE TABLE IF NOT EXISTS review_labels (
        event_id TEXT,
        claim_id TEXT,
        label TEXT,
        created_at TEXT,
        PRIMARY KEY(event_id, label),
        FOREIGN KEY(event_id) REFERENCES review_events(event_id) ON DELETE CASCADE,
        FOREIGN KEY(claim_id) REFERENCES source_cards(claim_id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_review_labels_claim ON review_labels(claim_id);
    CREATE INDEX IF NOT EXISTS idx_review_labels_label ON review_labels(label);
    CREATE TABLE IF NOT EXISTS citation_location_suggestions (
        suggestion_id TEXT PRIMARY KEY,
        context_id TEXT NOT NULL,
        matched_source_id TEXT,
        target_type TEXT,
        target_id TEXT,
        source_id TEXT,
        char_start INTEGER,
        char_end INTEGER,
        page_start TEXT,
        page_end TEXT,
        score REAL,
        score_json TEXT,
        query_text TEXT,
        matched_text TEXT,
        suggested_relation TEXT,
        status TEXT,
        created_at TEXT,
        updated_at TEXT,
        FOREIGN KEY(context_id) REFERENCES citation_contexts(context_id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_cls_context ON citation_location_suggestions(context_id);
    CREATE INDEX IF NOT EXISTS idx_cls_target ON citation_location_suggestions(target_type, target_id);
    CREATE INDEX IF NOT EXISTS idx_cls_status ON citation_location_suggestions(status);
    CREATE TABLE IF NOT EXISTS source_card_suggestions (
        suggestion_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        target_type TEXT,
        char_start INTEGER,
        char_end INTEGER,
        page_start TEXT,
        page_end TEXT,
        heading TEXT,
        section_role TEXT,
        suggested_claim_type TEXT,
        suggested_card_role TEXT,
        score REAL,
        score_json TEXT,
        text TEXT,
        status TEXT,
        created_at TEXT,
        updated_at TEXT,
        FOREIGN KEY(source_id) REFERENCES sources(source_id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_scs_source ON source_card_suggestions(source_id);
    CREATE INDEX IF NOT EXISTS idx_scs_status ON source_card_suggestions(status);
    CREATE INDEX IF NOT EXISTS idx_scs_offsets ON source_card_suggestions(source_id, char_start, char_end);
    CREATE TABLE IF NOT EXISTS source_location_signals (
        signal_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        char_start INTEGER,
        char_end INTEGER,
        signal_type TEXT,
        polarity TEXT,
        strength REAL,
        originating_context_id TEXT,
        originating_suggestion_id TEXT,
        citing_source_id TEXT,
        citation_function TEXT,
        note TEXT,
        created_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_sls_source_offsets ON source_location_signals(source_id, char_start, char_end);
    CREATE INDEX IF NOT EXISTS idx_sls_type ON source_location_signals(signal_type, polarity);
    CREATE TABLE IF NOT EXISTS synthesis_cards (
        synthesis_id TEXT PRIMARY KEY,
        title TEXT,
        synthesis TEXT NOT NULL,
        status TEXT,
        topic TEXT,
        scope_note TEXT,
        created_at TEXT,
        updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS synthesis_claims (
        synthesis_id TEXT,
        claim_id TEXT,
        role TEXT,
        note TEXT,
        PRIMARY KEY(synthesis_id, claim_id),
        FOREIGN KEY(synthesis_id) REFERENCES synthesis_cards(synthesis_id) ON DELETE CASCADE,
        FOREIGN KEY(claim_id) REFERENCES source_cards(claim_id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_synthesis_topic ON synthesis_cards(topic);
    CREATE TABLE IF NOT EXISTS local_embeddings (
        item_id TEXT PRIMARY KEY,
        item_type TEXT,
        source_id TEXT,
        text_hash TEXT,
        dim INTEGER,
        vector_json TEXT,
        created_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_local_embeddings_type ON local_embeddings(item_type, source_id);
    CREATE TABLE IF NOT EXISTS graph_edges_cache (
        edge_id TEXT PRIMARY KEY,
        source TEXT,
        target TEXT,
        kind TEXT,
        weight REAL,
        meta_json TEXT,
        updated_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_graph_edges_source ON graph_edges_cache(source);
    CREATE INDEX IF NOT EXISTS idx_graph_edges_target ON graph_edges_cache(target);
    CREATE INDEX IF NOT EXISTS idx_graph_edges_kind ON graph_edges_cache(kind);
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


def clean_paper_markup(text: str) -> str:
    """Remove harness/Obsidian annotation noise from markdown before canonical ingest.

    This is intentionally conservative: it removes highlight markers and known MA/page
    notes while preserving actual paper prose and citation text.
    """
    t=str(text or "")
    # Drop YAML frontmatter from annotated Obsidian notes; source metadata is stored separately.
    t=re.sub(r"\A---\s*\n[\s\S]*?\n---\s*\n", "", t, count=1)
    # Keep highlighted text but remove markers.
    t=t.replace("==", "")
    # Remove known page/verifier annotations and MA tags inserted during thesis work.
    t=re.sub(r"\[PAGE UNVERIFIED\]", " ", t, flags=re.I)
    t=re.sub(r"\[(?:Abstract-level benchmarks|Explicit TPB definition|Environmental attitudes|Farmer typology|Core quantitative finding|Past participation|Consistent positive|Contract design|Mixed evidence|[^\]]{0,40}RQ[123][^\]]{0,180})\]", " ", t)
    # Remove remaining curator notes in square brackets while preserving markdown links.
    t=re.sub(r"\[(?![^\]]+\]\()[^\]]{8,260}\]", " ", t)
    t=re.sub(r"#[A-Za-z0-9_./§-]+", " ", t)
    # Remove leftover repeated blank spacing without destroying paragraph boundaries.
    t=re.sub(r"[ \t]+", " ", t)
    t=re.sub(r"\n{4,}", "\n\n\n", t)
    return t.strip() + "\n"


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


def parse_map_spans(source_id: str, text: str, parse_map: dict[str, Any]|None) -> list[dict[str, Any]]:
    """Convert a parser sidecar map into offset-preserving spans.

    Expected optional keys: pages[{page,char_start,char_end}], sections[{heading,level,char_start,char_end}],
    tables/figures with similar offsets. This is intentionally permissive so your MD parser can evolve.
    """
    if not parse_map:
        return []
    offsets=line_offsets(text); rows=[]
    def safe_int(x, default=0):
        try: return int(x)
        except Exception: return default
    for i,pag in enumerate(parse_map.get("pages", []) or [], 1):
        start=max(0, safe_int(pag.get("char_start"), 0)); end=min(len(text), safe_int(pag.get("char_end"), len(text)))
        if end <= start: continue
        page=str(pag.get("page") or pag.get("page_number") or i)
        rows.append({"span_id": f"PAGE-{source_id}-{page}", "source_id": source_id, "kind": "page", "ref_id": page,
            "char_start": start, "char_end": end, "line_start": line_no(offsets,start), "line_end": line_no(offsets,max(start,end-1)),
            "page_start": page, "page_end": page, "heading": "", "text": text[start:end]})
    for i,sec in enumerate(parse_map.get("sections", []) or [], 1):
        start=max(0, safe_int(sec.get("char_start"), 0)); end=min(len(text), safe_int(sec.get("char_end"), len(text)))
        if end <= start: continue
        heading=str(sec.get("heading") or sec.get("title") or f"section {i}").strip()
        level=str(sec.get("level") or "")
        sid=f"SECTION-{source_id}-{i:04d}"
        page_start,_=page_for_char(source_id, start)
        page_end,_=page_for_char(source_id, max(start,end-1))
        rows.append({"span_id": sid, "source_id": source_id, "kind": "section", "ref_id": sid,
            "char_start": start, "char_end": end, "line_start": line_no(offsets,start), "line_end": line_no(offsets,max(start,end-1)),
            "page_start": page_start or "", "page_end": page_end or "", "heading": heading if not level else f"L{level}: {heading}", "text": text[start:end]})
    for kind in ["tables", "figures"]:
        for i,obj in enumerate(parse_map.get(kind, []) or [], 1):
            start=max(0, safe_int(obj.get("char_start"), 0)); end=min(len(text), safe_int(obj.get("char_end"), len(text)))
            if end <= start: continue
            sid=f"{kind[:-1].upper()}-{source_id}-{i:04d}"
            page_start,_=page_for_char(source_id, start)
            page_end,_=page_for_char(source_id, max(start,end-1))
            rows.append({"span_id": sid, "source_id": source_id, "kind": kind[:-1], "ref_id": str(obj.get("id") or sid),
                "char_start": start, "char_end": end, "line_start": line_no(offsets,start), "line_end": line_no(offsets,max(start,end-1)),
                "page_start": page_start or "", "page_end": page_end or "", "heading": str(obj.get("caption") or obj.get("heading") or ""), "text": text[start:end]})
    return rows


def ingest_source(path: Path, source_id: str, meta: dict[str, Any], parse_map_path: Path|None=None) -> None:
    init_db(True)
    text=path.read_text(encoding="utf-8", errors="ignore")
    blob_path, source_hash = write_blob(source_id, text)
    parse_map = None
    if parse_map_path:
        parse_map = json.loads(Path(parse_map_path).read_text(encoding="utf-8"))
    conn=db(); cur=conn.cursor()
    cur.execute("""INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        source_id, meta.get("title") or path.stem, meta.get("authors",""), str(meta.get("year", "")), meta.get("doi",""),
        meta.get("source_type","unknown"), meta.get("disciplines",""), meta.get("geography",""), meta.get("methodology",""),
        meta.get("theory",""), meta.get("quality","unrated"), meta.get("notes",""), blob_path, source_hash,
        len(text), text.count("\n")+1, now(), now()
    ))
    # Replace spans for this source.
    cur.execute("DELETE FROM spans WHERE source_id=?", (source_id,))
    parser_spans=parse_map_spans(source_id, text, parse_map)
    parser_pages=[sp for sp in parser_spans if sp.get("kind") == "page"]
    auto_pages=[] if parser_pages else page_spans(source_id, text)
    for sp in parser_spans + auto_pages + paragraph_spans(source_id, text) + chunk_spans(source_id, text):
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
    if getattr(args, "clean_markup", False):
        src=Path(args.path)
        cleaned=clean_paper_markup(src.read_text(encoding="utf-8", errors="ignore"))
        tmp=ROOT / "_tmp_clean_ingest.md"
        tmp.write_text(cleaned, encoding="utf-8")
        try:
            ingest_source(tmp, sid, meta, Path(args.parse_map) if args.parse_map else None)
        finally:
            try: tmp.unlink()
            except Exception: pass
        imported=import_parse_map_bibliography(sid, Path(args.parse_map) if args.parse_map else None, clear_existing=True)
        suffix=f" refs={imported.get('references',0)} cites={imported.get('citation_contexts',0)}" if imported.get('references') or imported.get('citation_contexts') else ""
        print(f"Ingested {sid} (cleaned markup){suffix}")
    else:
        ingest_source(Path(args.path), sid, meta, Path(args.parse_map) if args.parse_map else None)
        imported=import_parse_map_bibliography(sid, Path(args.parse_map) if args.parse_map else None, clear_existing=True)
        suffix=f" refs={imported.get('references',0)} cites={imported.get('citation_contexts',0)}" if imported.get('references') or imported.get('citation_contexts') else ""
        print(f"Ingested {sid}{suffix}")


def metadata_from_converted_dir(converted_dir: Path) -> tuple[Path, Path|None, dict[str, Any]]:
    """Read paper.md + optional paper.parse.json/paper.json from a parser output directory."""
    md_path=converted_dir / "paper.md"
    if not md_path.exists():
        raise SystemExit(f"Converted directory must contain paper.md: {converted_dir}")
    parse_path=converted_dir / "paper.parse.json"
    paper_json=converted_dir / "paper.json"
    stats_json=converted_dir / "stats.json"
    meta={"title":"", "authors":"", "year":"", "doi":"", "source_type":"peer-reviewed article", "disciplines":"", "geography":"", "methodology":"", "theory":"", "quality":"unrated", "notes":""}
    if paper_json.exists():
        try:
            pj=json.loads(paper_json.read_text(encoding="utf-8"))
            m=pj.get("metadata") or {}
            meta.update({
                "title": m.get("title") or meta["title"],
                "authors": "; ".join(m.get("authors") or []) if isinstance(m.get("authors"), list) else (m.get("authors") or meta["authors"]),
                "year": str(m.get("year") or meta["year"]),
                "doi": m.get("doi") or meta["doi"],
                "notes": f"Converted from {pj.get('source_pdf','')} via pdf_pipeline.".strip(),
            })
            if m.get("journal"):
                meta["notes"] = (meta.get("notes","") + f" Journal: {m.get('journal')}.").strip()
        except Exception as e:
            meta["notes"] = f"paper.json present but could not be parsed: {e}"
    if stats_json.exists():
        try:
            st=json.loads(stats_json.read_text(encoding="utf-8"))
            conf=st.get("confidence")
            if conf and meta["quality"] == "unrated":
                meta["quality"] = str(conf)
            meta["notes"] = (meta.get("notes","") + f" Parser stats: pages={st.get('n_pages')}, refs={st.get('n_references')}, linked_cites={st.get('n_citations_linked')}, coverage={st.get('coverage_ratio')}, confidence={st.get('confidence')}.").strip()
        except Exception:
            pass
    return md_path, (parse_path if parse_path.exists() else None), meta


def cmd_ingest_converted(args):
    converted_dir=Path(args.converted_dir)
    md_path, parse_path, meta=metadata_from_converted_dir(converted_dir)
    # CLI overrides parser metadata.
    for k in ["title","authors","year","doi","source_type","disciplines","geography","methodology","theory","quality","notes"]:
        v=getattr(args,k,None)
        if v:
            meta[k]=v
    sid=args.source_id or (canonical_source_id_from_doi(meta.get("doi")) if meta.get("doi") else slug(meta.get("title") or converted_dir.name))
    if args.clean_markup:
        cleaned=clean_paper_markup(md_path.read_text(encoding="utf-8", errors="ignore"))
        tmp=ROOT / "_tmp_clean_ingest.md"
        tmp.write_text(cleaned, encoding="utf-8")
        try:
            ingest_source(tmp, sid, meta, parse_path)
        finally:
            try: tmp.unlink()
            except Exception: pass
    else:
        ingest_source(md_path, sid, meta, parse_path)
    imported=import_parse_map_bibliography(sid, parse_path, clear_existing=True)
    payload={"source_id": sid, "paper_md": str(md_path), "parse_map": str(parse_path) if parse_path else "", "metadata": meta, "imported_parse_map": imported}
    if args.json: print_json(payload)
    else:
        suffix=f" refs={imported.get('references',0)} cites={imported.get('citation_contexts',0)}" if imported.get('references') or imported.get('citation_contexts') else ""
        print(f"Ingested converted paper {sid} from {converted_dir}" + (" with parse map" if parse_path else "") + suffix)


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
    if used:
        placeholders = ','.join(['?'] * len(used))
        used_list = list(used)
        status_counts={r["verification_status"]: r["n"] for r in conn.execute(f"SELECT verification_status, COUNT(*) n FROM source_cards WHERE claim_id IN ({placeholders}) GROUP BY verification_status", used_list).fetchall()}
        source_counts={r["source_id"]: r["n"] for r in conn.execute(f"SELECT source_id, COUNT(*) n FROM source_cards WHERE claim_id IN ({placeholders}) GROUP BY source_id", used_list).fetchall()}
    else:
        status_counts={}
        source_counts={}
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
                # Avoid page-break fragments / table remnants rather than complete sentences.
                if not re.search(r'[.!?][)\'"”\]]?$', clean):
                    continue
                if re.match(r"^\d+[.;:]\s", clean):
                    continue
                if re.match(r"^(and|or|but|while|whereas|although)\b", clean, flags=re.I):
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
    h=norm(heading)
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
    if any(x in h for x in ["method", "study areas", "landscape scenarios", "modelling approach", "data analysis", "material and methods", "literature search", "data collection"]):
        claim_type="methodological claim"
    if claim_type != "methodological claim" and any(k in l for k in ["should", "recommend", "target", "monitoring", "intervention", "strategic plan"]):
        claim_type="policy implication"
    if claim_type != "methodological claim" and any(k in l for k in ["lack", "missing", "unclear", "not addressed", "doubts", "concerns", "hinder", "risk"]):
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


def import_parse_map_bibliography(source_id: str, parse_map_path: Path|None, *, clear_existing: bool=False) -> dict[str, Any]:
    """Import parser sidecar references/citations into source_references/citation_contexts.

    The sidecar's `reference_id` values are local to the parser output (e.g. ref-001).
    They are converted to stable DB instance IDs: REF-{source_id}-{reference_id}.
    """
    if not parse_map_path or not Path(parse_map_path).exists():
        return {"references": 0, "citation_contexts": 0, "skipped": "no_parse_map"}
    init_db(True)
    parse_map=json.loads(Path(parse_map_path).read_text(encoding="utf-8"))
    try:
        text=read_source_text(source_id)
    except SystemExit:
        text=""
    offsets=line_offsets(text or "")
    refs=parse_map.get("references", []) or []
    cites=parse_map.get("citations", []) or []
    conn=db(); cur=conn.cursor()
    if clear_existing:
        cur.execute("DELETE FROM citation_contexts WHERE citing_source_id=?", (source_id,))
        cur.execute("DELETE FROM source_references WHERE source_id=?", (source_id,))
    ref_id_map={}
    inserted_refs=0
    for i,r in enumerate(refs,1):
        local_id=str(r.get("reference_id") or r.get("id") or f"ref-{i:03d}")
        raw=clean_reference_raw(r.get("raw_text") or r.get("text") or "")
        doi=extract_doi_from_text(r.get("doi") or raw)
        year=str(r.get("year") or year_token_from_text(raw) or "")
        title=str(r.get("title") or "")
        authors=r.get("authors") or []
        author_part="; ".join(authors) if isinstance(authors, list) else str(authors or "")
        author_key=str(r.get("author_key") or author_key_from_author_part(author_part or raw))
        if not title or not author_key or not year:
            ak2,y2,title2,_ap=parse_reference_author_year_title(raw)
            author_key=author_key or ak2; year=year or y2; title=title or title2
        canonical=str(r.get("canonical_reference_id") or r.get("canonical_source_id") or deterministic_source_id(author_key, year, title, doi))
        matched=match_local_source(author_key, year, source_id, doi=doi, canonical_source_id=canonical)
        db_ref_id=f"REF-{source_id}-{local_id}"
        ref_id_map[local_id]=db_ref_id
        cur.execute("""INSERT OR REPLACE INTO source_references
            (reference_id, source_id, reference_anchor, raw_text, author_key, year, title, doi, canonical_source_id, matched_source_id, status, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (db_ref_id, source_id, local_id, raw, author_key, year, title, doi, canonical, matched, "matched_local" if matched else "missing_source", now()))
        inserted_refs += 1
    inserted_cites=0
    for i,c in enumerate(cites,1):
        local_ref=str(c.get("reference_id") or "")
        db_ref=ref_id_map.get(local_ref) or (f"REF-{source_id}-{local_ref}" if local_ref else None)
        ref_row=cur.execute("SELECT * FROM source_references WHERE reference_id=?", (db_ref,)).fetchone() if db_ref else None
        start=c.get("char_start"); end=c.get("char_end")
        ctx_start=c.get("context_char_start", start); ctx_end=c.get("context_char_end", end)
        try:
            start_i=int(start); end_i=int(end); ctx_start_i=int(ctx_start); ctx_end_i=int(ctx_end)
        except Exception:
            continue
        context_text=c.get("sentence_context") or c.get("context_text") or (text[ctx_start_i:ctx_end_i].strip() if text else "")
        citation_text=c.get("raw_text") or c.get("label") or (text[start_i:end_i].strip() if text else "")
        author_key=ref_row["author_key"] if ref_row else ""
        year=ref_row["year"] if ref_row else year_token_from_text(citation_text)
        canonical=ref_row["canonical_source_id"] if ref_row else ""
        matched=ref_row["matched_source_id"] if ref_row else None
        context_id=str(c.get("citation_id") or f"cit-{i:05d}")
        if not context_id.startswith("CITCTX-"):
            context_id=f"CITCTX-{source_id}-{i:05d}"
        cur.execute("""INSERT OR REPLACE INTO citation_contexts
            (context_id, citing_source_id, reference_id, reference_anchor, canonical_source_id, cited_author_key, cited_year, citation_text, char_start, char_end, line_start, line_end, context_text, citation_function, matched_source_id, verification_status, verification_note, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            context_id, source_id, db_ref, local_ref, canonical, author_key, year, citation_text, start_i, end_i,
            line_no(offsets,start_i) if text else None, line_no(offsets,end_i) if text else None, context_text,
            classify_citation_function(context_text), matched, "needs_verification" if matched else "missing_source", "imported_from_parse_map", now(), now()
        ))
        inserted_cites += 1
    conn.commit(); conn.close()
    return {"references": inserted_refs, "citation_contexts": inserted_cites}


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


def brief_usage_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in EXPORTS.glob("chapter_brief_*.json"):
        try:
            packet=json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for sec in packet.get("sections", []):
            for c in sec.get("claims", []):
                cid=c.get("claim_id")
                if cid:
                    counts[cid]=counts.get(cid,0)+1
    return counts


def review_labels_for(claim_id: str) -> list[str]:
    init_db(True)
    conn=db()
    rows=conn.execute("SELECT DISTINCT label FROM review_labels WHERE claim_id=? ORDER BY label", (claim_id,)).fetchall()
    conn.close()
    return [r["label"] for r in rows]


def review_history_for(claim_id: str) -> list[dict[str, Any]]:
    init_db(True)
    conn=db()
    rows=conn.execute("SELECT * FROM review_events WHERE claim_id=? ORDER BY created_at DESC", (claim_id,)).fetchall()
    labels=conn.execute("SELECT event_id, label FROM review_labels WHERE claim_id=? ORDER BY label", (claim_id,)).fetchall()
    conn.close()
    by_event: dict[str, list[str]] = {}
    for r in labels:
        by_event.setdefault(r["event_id"], []).append(r["label"])
    out=[]
    for r in rows:
        d=dict(r); d["labels"]=by_event.get(d["event_id"], [])
        out.append(d)
    return out


def insert_review_event(conn: sqlite3.Connection, claim_id: str, from_status: str, to_status: str, note: str="", actor: str="human", labels: list[str]|None=None) -> str:
    event_id=str(uuid.uuid4())
    conn.execute("INSERT INTO review_events VALUES (?,?,?,?,?,?,?)", (event_id, claim_id, from_status, to_status, note or "", actor or "human", now()))
    for label in labels or []:
        if label:
            conn.execute("INSERT OR IGNORE INTO review_labels VALUES (?,?,?,?)", (event_id, claim_id, label, now()))
    return event_id


def update_claim_fts(cur: sqlite3.Cursor, claim_id: str) -> None:
    row=cur.execute("SELECT claim, evidence FROM source_cards WHERE claim_id=?", (claim_id,)).fetchone()
    if not row:
        return
    tags=" ".join(f"{r['tag_type']}:{r['tag']}" for r in cur.execute("SELECT tag_type, tag FROM claim_tags WHERE claim_id=?", (claim_id,)).fetchall())
    cur.execute("DELETE FROM claims_fts WHERE claim_id=?", (claim_id,))
    cur.execute("INSERT INTO claims_fts(claim_id, claim, evidence, tags) VALUES (?,?,?,?)", (claim_id, row["claim"], row["evidence"], tags))


def claim_queue_score(card: dict[str, Any], usage: int, relation_count: int) -> float:
    status_weight={"needs_source_check": 50, "needs_page_check": 40, "candidate_needs_review": 30, "verified": 5, "superseded": -50, "rejected": -100}.get(card.get("status"), 10)
    grade_weight={"D": 25, "C": 18, "B": 8, "A": 0, "X": -50}.get(card.get("evidence_grade"), 10)
    return status_weight + grade_weight + usage*6 + relation_count*3


def cmd_review_queue(args):
    init_db(True)
    conn=db()
    rows=conn.execute("SELECT * FROM source_cards WHERE verification_status!='rejected' ORDER BY source_id, claim_id").fetchall()
    usage=brief_usage_counts()
    items=[]
    for r in rows:
        card=claim_card(r, "standard")
        grade=card.get("evidence_grade")
        if args.source_id and card.get("source_id") != args.source_id: continue
        if args.status and card.get("status") not in split_list(args.status): continue
        if args.grade and grade != args.grade: continue
        if args.claim_type and card.get("claim_type") not in split_list(args.claim_type): continue
        labels=review_labels_for(card["claim_id"])
        if args.label and args.label not in labels: continue
        rel_count=conn.execute("SELECT COUNT(*) FROM claim_relations WHERE claim_a=? OR claim_b=?", (card["claim_id"], card["claim_id"])).fetchone()[0]
        used=usage.get(card["claim_id"],0)
        if args.centrality == "high" and not (used or rel_count): continue
        score=claim_queue_score(card, used, rel_count)
        items.append({**card, "queue_score": round(score,2), "used_in_briefs": used, "relation_count": rel_count, "review_labels": labels, "review_packet": f"python rh2.py review-packet {card['claim_id']}"})
    conn.close()
    items=sorted(items, key=lambda x: x["queue_score"], reverse=True)[:args.limit]
    if args.json:
        print_json({"count": len(items), "items": items})
    else:
        print(f"Review queue: {len(items)} item(s)")
        for c in items:
            print(f"- {c['claim_id']} score:{c['queue_score']} grade:{c['evidence_grade']} {c['status']} used:{c['used_in_briefs']} rel:{c['relation_count']} {c['claim_type']}")
            print(f"  {short(c['claim'], 220)}")
            print(f"  packet: python rh2.py review-packet {c['claim_id']}")


def cmd_review_packet(args):
    init_db(True)
    conn=db(); row=conn.execute("SELECT * FROM source_cards WHERE claim_id=?", (args.claim_id,)).fetchone()
    if not row: raise SystemExit(f"Unknown claim_id: {args.claim_id}")
    d=dict(row); card=claim_card(d, "full")
    source=dict(conn.execute("SELECT * FROM sources WHERE source_id=?", (d["source_id"],)).fetchone() or {})
    usage=brief_usage_counts().get(args.claim_id,0)
    nearby=[]
    if d.get("char_start") is not None:
        rows=conn.execute("""
            SELECT * FROM source_cards WHERE source_id=? AND claim_id!=? AND char_start IS NOT NULL
            ORDER BY ABS(char_start - ?) LIMIT ?
        """, (d["source_id"], args.claim_id, int(d.get("char_start") or 0), args.nearby)).fetchall()
        nearby=[claim_card(r, "minimal") for r in rows]
    citation_contexts=[]
    try:
        citation_contexts=[dict(r) for r in conn.execute("""
            SELECT context_id, citing_source_id, matched_source_id, author_key, year, context_text, citation_function, verification_status
            FROM citation_contexts
            WHERE matched_source_id=? OR citing_source_id=?
            ORDER BY created_at DESC LIMIT ?
        """, (d["source_id"], d["source_id"], args.citation_limit)).fetchall()]
    except sqlite3.OperationalError:
        citation_contexts=[]
    conn.close()
    context_payload=None; context_error=""
    try:
        text=read_source_text(d["source_id"])
        if args.context_mode == "char":
            start=max(0, int(d.get("char_start") or 0)-args.window); end=min(len(text), int(d.get("char_end") or d.get("char_start") or 0)+args.window)
            context_payload={"mode":"char", "context_start": start, "context_end": end, "context": text[start:end]}
        else:
            context_payload=sentence_aware_context(d["source_id"], int(d.get("char_start") or 0), int(d.get("char_end") or d.get("char_start") or 0), radius=args.sentence_radius, outside_paragraph=args.outside_paragraph)
    except SystemExit as e:
        context_error=str(e)
    payload={
        "claim": card,
        "source": source,
        "usage": {"chapter_brief_count": usage},
        "review_history": review_history_for(args.claim_id),
        "relations": claim_relations_for(args.claim_id),
        "nearby_claims": nearby,
        "citation_contexts": citation_contexts,
        "context": context_payload,
        "context_error": context_error,
        "suggested_actions": [
            f"python rh2.py review {args.claim_id} verified --label good_claim --note 'Checked source/page/scope.'",
            f"python rh2.py revise-claim {args.claim_id} --scope-note '...'",
            f"python rh2.py supersede {args.claim_id} NEW_CLAIM_ID --note 'Tighter source-range claim.'",
        ]
    }
    if args.json: print_json(payload)
    else:
        print(f"# Review packet: {args.claim_id}")
        print(f"Status/grade/type: {card.get('status')} / {card.get('evidence_grade')} / {card.get('claim_type')}")
        print(f"Source: {card.get('citation_hint')} p.{card.get('page') or '?'}")
        print(f"Claim: {card.get('claim')}")
        if card.get("evidence"): print(f"Evidence: {card.get('evidence')}")
        if card.get("scope_note"): print(f"Scope: {card.get('scope_note')}")
        if context_error: print(f"\nContext unavailable: {context_error}")
        elif context_payload: print(f"\n--- context ({context_payload.get('mode')}) ---\n{context_payload.get('context')}")
        if payload["relations"]:
            print("\nRelations:"); [print(f"- {r['claim_a']} --{r['relation_type']}--> {r['claim_b']} ({r['status']}) {r.get('note','')}") for r in payload["relations"]]
        if nearby:
            print("\nNearby claims:"); [print(f"- {c['claim_id']} {short(c['claim'], 140)}") for c in nearby]
        if citation_contexts:
            print("\nCitation contexts:"); [print(f"- {c['context_id']} {short(c.get('context_text',''), 180)}") for c in citation_contexts[:args.citation_limit]]
        print("\nSuggested actions:"); [print(f"- {x}") for x in payload["suggested_actions"]]


def cmd_revise_claim(args):
    init_db(True)
    conn=db(); cur=conn.cursor(); row=cur.execute("SELECT * FROM source_cards WHERE claim_id=?", (args.claim_id,)).fetchone()
    if not row: raise SystemExit(f"Unknown claim_id: {args.claim_id}")
    old_status=row["verification_status"]
    fields=[]; vals=[]
    for attr,col in [("claim","claim"),("evidence","evidence"),("scope_note","scope_note"),("claim_type","claim_type"),("confidence","confidence"),("status","verification_status")]:
        val=getattr(args, attr)
        if val is not None:
            fields.append(f"{col}=?"); vals.append(val)
    if args.char_start is not None and args.char_end is not None:
        page,_=page_for_char(row["source_id"], args.char_start)
        fields += ["char_start=?", "char_end=?", "line_start=?", "line_end=?", "page=?", "page_status=?"]
        text=""
        try:
            text=read_source_text(row["source_id"])
            offs=line_offsets(text); ls=line_no(offs,args.char_start); le=line_no(offs,args.char_end)
        except SystemExit:
            ls=le=None
        vals += [args.char_start, args.char_end, ls, le, page or row["page"], "page_matched_from_source_span" if page else row["page_status"]]
        if args.evidence is None:
            fields.append("evidence=?"); vals.append(source_slice(row["source_id"], args.char_start, args.char_end) or row["evidence"])
    if not fields and not args.label and not args.note:
        raise SystemExit("Nothing to revise. Provide fields, --label or --note.")
    if fields:
        fields.append("updated_at=?"); vals.append(now()); vals.append(args.claim_id)
        cur.execute(f"UPDATE source_cards SET {', '.join(fields)} WHERE claim_id=?", vals)
        update_claim_fts(cur, args.claim_id)
    new_status=args.status or old_status
    insert_review_event(conn, args.claim_id, old_status, new_status, args.note or "Claim revised.", args.actor or "human", args.label or ["revised"])
    conn.commit(); row=conn.execute("SELECT * FROM source_cards WHERE claim_id=?", (args.claim_id,)).fetchone(); conn.close()
    print_json({"updated": True, "claim": claim_card(row, "full")}) if args.json else print(f"Updated {args.claim_id}")


def cmd_supersede(args):
    init_db(True)
    conn=db(); cur=conn.cursor()
    old=cur.execute("SELECT verification_status FROM source_cards WHERE claim_id=?", (args.old_claim,)).fetchone()
    new=cur.execute("SELECT verification_status FROM source_cards WHERE claim_id=?", (args.new_claim,)).fetchone()
    if not old: raise SystemExit(f"Unknown old claim_id: {args.old_claim}")
    if not new: raise SystemExit(f"Unknown new claim_id: {args.new_claim}")
    cur.execute("UPDATE source_cards SET verification_status='superseded', updated_at=? WHERE claim_id=?", (now(), args.old_claim))
    insert_review_event(conn, args.old_claim, old["verification_status"], "superseded", args.note or f"Superseded by {args.new_claim}", args.actor or "human", ["superseded"])
    rid=args.relation_id or f"REL-{sha1_short(args.new_claim + args.old_claim + 'supersedes')[:10]}"
    cur.execute("INSERT OR REPLACE INTO claim_relations VALUES (?,?,?,?,?,?,?)", (rid, args.new_claim, args.old_claim, "supersedes", args.note or "", "verified" if args.verify_relation else "candidate_needs_review", now()))
    update_claim_fts(cur, args.old_claim)
    conn.commit(); conn.close()
    print(f"{args.old_claim}: superseded by {args.new_claim} ({rid})")


def cmd_duplicate(args):
    init_db(True)
    conn=db(); cur=conn.cursor()
    for cid in [args.duplicate_claim, args.canonical_claim]:
        if not cur.execute("SELECT verification_status FROM source_cards WHERE claim_id=?", (cid,)).fetchone():
            raise SystemExit(f"Unknown claim_id: {cid}")
    rid=args.relation_id or f"REL-{sha1_short(args.duplicate_claim + args.canonical_claim + 'duplicate')[:10]}"
    cur.execute("INSERT OR REPLACE INTO claim_relations VALUES (?,?,?,?,?,?,?)", (rid, args.duplicate_claim, args.canonical_claim, "duplicate_of", args.note or "", args.status or "candidate_needs_review", now()))
    if args.reject_duplicate:
        old=cur.execute("SELECT verification_status FROM source_cards WHERE claim_id=?", (args.duplicate_claim,)).fetchone()["verification_status"]
        cur.execute("UPDATE source_cards SET verification_status='rejected', updated_at=? WHERE claim_id=?", (now(), args.duplicate_claim))
        insert_review_event(conn, args.duplicate_claim, old, "rejected", args.note or f"Duplicate of {args.canonical_claim}", args.actor or "human", ["duplicate"])
        update_claim_fts(cur, args.duplicate_claim)
    conn.commit(); conn.close()
    print(f"{rid}: {args.duplicate_claim} duplicate_of {args.canonical_claim}")


def cmd_split_claim(args):
    init_db(True)
    conn=db(); base=conn.execute("SELECT * FROM source_cards WHERE claim_id=?", (args.claim_id,)).fetchone(); conn.close()
    if not base: raise SystemExit(f"Unknown claim_id: {args.claim_id}")
    specs=json.loads(Path(args.file).read_text(encoding="utf-8"))
    if not isinstance(specs, list): raise SystemExit("Split file must be a JSON array of claim specs.")
    created=[]
    inherited_tags={}
    if args.inherit_tags:
        for t in tags_for_claim(args.claim_id):
            typ,_,tag=t.partition(":")
            inherited_tags.setdefault(typ, []).append(tag)
    for spec in specs:
        sid=base["source_id"]
        char_start=spec.get("char_start"); char_end=spec.get("char_end")
        evidence=spec.get("evidence") or ""
        loc={}
        if char_start is not None and char_end is not None:
            evidence=evidence or source_slice(sid, int(char_start), int(char_end))
            try:
                offs=line_offsets(read_source_text(sid)); loc={"char_start": int(char_start), "char_end": int(char_end), "line_start": line_no(offs,int(char_start)), "line_end": line_no(offs,int(char_end))}
            except SystemExit:
                loc={"char_start": int(char_start), "char_end": int(char_end), "line_start": None, "line_end": None}
        elif evidence:
            try:
                text=read_source_text(sid); loc=locate_span(text, evidence)
            except SystemExit:
                loc={}
        page=spec.get("page") or page_for_char(sid, loc.get("char_start"))[0]
        claim={"claim_id": spec.get("claim_id") or next_claim_id(sid), "source_id": sid, "claim": spec.get("claim") or evidence,
            "evidence": evidence, "claim_representation": spec.get("claim_representation") or ("source_range" if char_start is not None else "paraphrase"),
            "claim_type": spec.get("claim_type") or base["claim_type"], "page": page,
            "page_status": "page_matched_from_source_span" if page else "needs_page_check", "verification_status": spec.get("status") or args.status,
            "confidence": spec.get("confidence") or base["confidence"], "scope_note": spec.get("scope_note") or f"Split from {args.claim_id}.",
            "char_start": loc.get("char_start"), "char_end": loc.get("char_end"), "line_start": loc.get("line_start"), "line_end": loc.get("line_end"),
            "extraction_mode": "split_claim"}
        tags={**inherited_tags, **(spec.get("tags") or {})}
        upsert_claim(claim, tags)
        created.append(claim["claim_id"])
        cconn=db(); rid=f"REL-{sha1_short(claim['claim_id'] + args.claim_id + 'qualifies')[:10]}"
        cconn.execute("INSERT OR REPLACE INTO claim_relations VALUES (?,?,?,?,?,?,?)", (rid, claim["claim_id"], args.claim_id, "qualifies", args.note or "Split from broader claim.", "candidate_needs_review", now()))
        cconn.commit(); cconn.close()
    if args.supersede_original and created:
        class Obj: pass
        o=Obj(); o.old_claim=args.claim_id; o.new_claim=created[0]; o.note=args.note or "Split into narrower claims."; o.actor=args.actor; o.relation_id=None; o.verify_relation=False
        cmd_supersede(o)
    print_json({"split_from": args.claim_id, "created": created}) if args.json else print(f"Created {len(created)} split claims: {', '.join(created)}")


PAGE_LOCATOR_RE = re.compile(r"(?:\bpp?\.?\s*(\d{1,5})(?:\s*[–—-]\s*(\d{1,5}))?|\b\d{4}[a-z]?\s*:\s*(\d{1,5})(?:\s*[–—-]\s*(\d{1,5}))?)", re.I)


def parse_citation_locator(*texts: str) -> dict[str, Any]:
    raw=" ".join(str(t or "") for t in texts)
    m=PAGE_LOCATOR_RE.search(raw)
    if not m:
        return {"locator_raw":"", "page_start":"", "page_end":"", "pages":[]}
    start=m.group(1) or m.group(3); end=m.group(2) or m.group(4) or start
    pages=[]
    try:
        a,b=int(start),int(end)
        if a <= b and b-a <= 50:
            pages=[str(x) for x in range(a,b+1)]
    except Exception:
        pages=[start]
    return {"locator_raw": m.group(0), "page_start": start, "page_end": end, "pages": pages}


CITATION_STOPWORDS = {
    "and", "or", "the", "this", "that", "these", "those", "with", "from", "into", "onto", "over", "under", "between", "among",
    "have", "has", "had", "been", "being", "were", "was", "are", "is", "their", "there", "where", "which", "while", "within",
    "farmers", "farmer", "farming", "agricultural", "agriculture", "study", "studies", "paper", "authors", "research", "literature",
    "variables", "variable", "describing", "importance", "important", "place", "play", "played", "role", "measure", "regarding", "more",
    "dessart", "canessa", "et", "al", "however", "also", "e.g", "e", "g", "such", "may", "might", "could", "would", "should",
}

DOMAIN_IMPORTANCE = {
    "behavioural": 1.8, "behavioral": 1.8, "dispositional": 2.2, "cognitive": 2.0, "social": 1.4,
    "attitude": 2.0, "attitudes": 2.0, "norm": 2.0, "norms": 2.0, "awareness": 2.0, "concern": 1.8,
    "risk": 2.0, "risks": 2.0, "uncertainty": 1.8, "control": 1.8, "costs": 1.6, "benefits": 1.6,
    "objectives": 1.8, "communication": 2.0, "interpersonal": 2.2, "adoption": 1.6, "participation": 1.6,
    "sustainable": 1.3, "practices": 1.3, "environmental": 1.4, "aectm": 1.4, "aecm": 1.4,
    "contract": 1.8, "transaction": 1.8, "monitoring": 1.8, "perceived": 1.5, "relevance": 1.8,
}

TERM_SYNONYMS = {
    "attitude": ["environmental concern", "moral concern", "dispositional", "environmental attitudes"],
    "attitudes": ["environmental concern", "moral concern", "dispositional", "environmental attitudes"],
    "awareness": ["environmental concern", "problem awareness", "knowledge"],
    "relevance": ["relative advantage", "perceived benefits", "perceived relevance"],
    "benefit": ["relative advantage", "perceived benefits"],
    "benefits": ["relative advantage", "perceived benefits"],
    "communication": ["interpersonal relationships", "social interaction", "advisors", "neighbours", "neighbors"],
    "interpersonal": ["interpersonal relationships", "social interaction", "social norms"],
    "norms": ["social norms", "injunctive norms", "descriptive norms"],
    "norm": ["social norm", "injunctive norm", "descriptive norm"],
    "control": ["perceived behavioural control", "perceived behavioral control", "ability", "capacity", "constraints"],
    "monitoring": ["control", "administrative", "transaction costs"],
    "costs": ["transaction costs", "administrative burden", "direct costs"],
    "contract": ["scheme design", "contract design", "flexibility"],
    "objectives": ["farming objectives", "economic objectives", "environmental objectives", "lifestyle objectives"],
    "risk": ["risk tolerance", "perceived risks", "uncertainty"],
    "risks": ["risk tolerance", "perceived risks", "uncertainty"],
}

BOILERPLATE_RE = re.compile(r"copyright|downloaded from|creative commons|guest on|doi:|journal:|author:|^---$|^tags:", re.I)


def citation_terms(text: str) -> list[str]:
    toks=[]
    for w in re.findall(r"[A-Za-z][A-Za-z-]{2,}", norm(text)):
        w=w.strip("-")
        if len(w) < 4 or w in CITATION_STOPWORDS or re.fullmatch(r"\d+", w):
            continue
        toks.append(w)
    # preserve order, unique
    seen=set(); out=[]
    for t in toks:
        if t not in seen:
            seen.add(t); out.append(t)
    return out


def weighted_term_coverage(query: str, candidate_text: str) -> tuple[float, list[str], list[str]]:
    qterms=citation_terms(query)
    if not qterms:
        return 0.0, [], []
    cn=norm(candidate_text)
    matched=[]; missing=[]; total=0.0; got=0.0
    for t in qterms:
        weight=DOMAIN_IMPORTANCE.get(t, 1.0)
        total += weight
        alternatives=[t] + TERM_SYNONYMS.get(t, [])
        hit=""
        for alt in alternatives:
            alt_n=norm(alt)
            if re.search(rf"\b{re.escape(alt_n)}\b", cn) or (len(alt_n) >= 7 and alt_n in cn):
                hit=alt; break
        if hit:
            got += weight; matched.append(t if hit == t else f"{t}~{hit}")
        else:
            missing.append(t)
    return got / max(1.0, total), matched, missing


def source_intro_start(text: str) -> int:
    """Return first plausible Introduction offset; used to suppress metadata/abstract/front matter for citation-location search."""
    patterns=[
        r"(?im)^#\s*1\.?\s+Introduction\b",
        r"(?im)^#\s*1\.1\.?\s+Context\b",
        r"(?im)^#\s*Introduction\b",
        r"(?im)^1\.?\s+Introduction\b",
    ]
    for pat in patterns:
        m=re.search(pat, text or "")
        if m:
            return m.start()
    # Fallback: after YAML/front matter if present, otherwise zero.
    m=re.match(r"\A---\s*\n[\s\S]*?\n---\s*\n", text or "")
    return m.end() if m else 0


def source_backmatter_start(text: str) -> int:
    """Return offset where references/back matter begins, or len(text) if absent."""
    patterns=[r"(?im)^#\s*References\b", r"(?im)^#\s*Bibliography\b", r"(?im)^References\s*$", r"(?im)^Bibliography\s*$"]
    for pat in patterns:
        m=re.search(pat, text or "")
        if m:
            return m.start()
    return len(text or "")


def span_quality_penalty(text: str, heading: str="", kind: str="") -> tuple[float, list[str]]:
    penalties=[]; penalty=0.0
    sample=(text or "")[:1200]
    low=norm(sample)
    if BOILERPLATE_RE.search(sample) or "[[" in sample or "#ma/" in low:
        penalty += 0.35; penalties.append("boilerplate_or_annotation")
    # Penalize table-heavy fragments unless they have very strong lexical support.
    lines=[ln for ln in (text or "").splitlines() if ln.strip()]
    if lines:
        pipe_lines=sum(1 for ln in lines if ln.count("|") >= 3)
        if pipe_lines / max(1, len(lines)) > 0.35:
            penalty += 0.25; penalties.append("table_heavy")
    if kind == "chunk":
        penalty += 0.06; penalties.append("chunk_broadness")
    if len(text or "") > 1800:
        penalty += 0.04; penalties.append("long_span")
    if heading and re.match(r"^\d{3,}\b|^F\. J\. Dessart", heading.strip()):
        penalty += 0.18; penalties.append("page_header_heading")
    return penalty, penalties


def heading_term_bonus(query: str, heading: str) -> tuple[float, str]:
    if not heading:
        return 0.0, ""
    cov, matched, _ = weighted_term_coverage(query, heading)
    if cov <= 0:
        return 0.0, ""
    bonus=min(0.24, cov * 0.30)
    q=norm(query); h=norm(heading)
    for phrase in ["environmental concern", "moral concern", "farming objectives", "perceived behavioural control", "interpersonal relationships", "social norms", "perceived risks", "costs and benefits"]:
        if phrase in q and phrase in h:
            bonus += 0.12
            matched.append(f"heading_exact:{phrase}")
            break
    return min(0.34, bonus), "heading_terms:" + ",".join(matched[:5])


def repaired_citation_context(ctx: dict[str, Any]) -> str:
    """Expand a citation context to a fuller sentence window when the citing source blob is available."""
    fallback=ctx.get("context_text") or ctx.get("citation_text") or ""
    try:
        start=ctx.get("char_start"); end=ctx.get("char_end") or start
        if start is None:
            return fallback
        repaired=sentence_aware_context(ctx.get("citing_source_id"), int(start), int(end), radius=1, outside_paragraph=True)
        return repaired.get("context") or fallback
    except BaseException:
        return fallback


def is_reference_or_backmatter(heading: str, text: str) -> bool:
    h=norm(heading)
    if re.match(r"^(references|bibliography|acknowledg|appendix|supplement)", h):
        return True
    sample=text[:1500]
    if len(re.findall(r"https?://|\bdoi\b|\(\d{4}\)", sample, flags=re.I)) >= 5:
        # Real prose can contain a few references; many DOI/URL/year patterns means bibliography/table.
        return True
    return False


def sentence_window_candidates(source_id: str, span: dict[str, Any], source_text: str, query: str, locator: dict[str, Any], function: str) -> list[dict[str, Any]]:
    start=span.get("char_start"); end=span.get("char_end")
    if start is None or end is None:
        return []
    try:
        start_i=int(start); end_i=int(end)
    except Exception:
        return []
    local=source_text[start_i:end_i]
    sents=sentence_spans(local, start_i)
    if not sents:
        return []
    out=[]
    for i,(_ss,_se,_sent) in enumerate(sents):
        for radius in [0,1]:
            lo=max(0,i-radius); hi=min(len(sents),i+radius+1)
            wstart=sents[lo][0]; wend=sents[hi-1][1]
            text=source_text[wstart:wend].strip()
            if len(text) < 50:
                continue
            page=span.get("page_start") or candidate_page_for_span(source_id, wstart) or ""
            score, why=citation_candidate_score(query, text, page=page, locator=locator, function=function, heading=span.get("heading") or "", kind="sentence_window")
            if score > 0:
                out.append({"target_type":"sentence_window", "target_id":f"SENTWIN-{source_id}-{wstart}-{wend}", "source_id":source_id, "char_start":wstart, "char_end":wend, "page_start":page, "page_end":page, "score":round(score + 0.04,4), "score_json":why, "matched_text":short(text, 900), "handle":source_range_handle(source_id,wstart,wend)})
            if radius == 0:
                break  # prefer exact sentence; radius=1 only evaluated by outer loop? disabled for now to avoid duplicates
    return out


def normalize_citation_query(ctx: dict[str, Any]) -> str:
    text=" ".join([ctx.get("context_text") or "", ctx.get("citation_text") or ""])
    # Remove citation clutter but preserve substantive words around it.
    text=re.sub(r"\([^)]{0,180}\b\d{4}[a-z]?[^)]{0,180}\)", " ", text)
    text=re.sub(r"\b[A-Z][A-Za-z-]+\s+et\s+al\.?\s*\(?\d{4}[a-z]?\)?", " ", text)
    text=re.sub(r"\bpp?\.?\s*\d{1,5}(?:\s*[–—-]\s*\d{1,5})?", " ", text, flags=re.I)
    text=re.sub(r"\[[^\]]{0,80}\]\([^)]{0,120}\)", " ", text)
    text=re.sub(r"==", "", text)
    text=re.sub(r"\s+", " ", text).strip(" .;,")
    # Domain expansion for frequent citing paraphrases. This is transparent and only affects retrieval.
    low=norm(text)
    expansions=[]
    if "attitude" in low and "environment" in low:
        expansions.append("environmental concern moral concern dispositional factors")
    if "perceived relevance" in low or "relevance" in low:
        expansions.append("relative advantage perceived benefits adoption")
    if "interpersonal" in low or "communication" in low:
        expansions.append("interpersonal relationships social norms neighbours advisors")
    if "perceived behavioural control" in low or "behavioral control" in low:
        expansions.append("perceived self-efficacy skills time control")
    if "farming objectives" in low:
        expansions.append("economic objectives environmental objectives lifestyle objectives")
    if expansions:
        text = (text + " " + " ".join(expansions)).strip()
    return text or (ctx.get("context_text") or ctx.get("citation_text") or "")


def citation_section_prior(function: str, heading: str, text: str) -> tuple[float, str]:
    f=norm(function); h=norm(heading); t=norm(text[:500])
    hay=" ".join([h,t])
    priors={
        "definition": ["definition", "theory", "background", "introduction", "conceptual"],
        "method_or_framework": ["method", "model", "data", "framework", "approach"],
        "supporting_evidence": ["result", "finding", "discussion", "analysis"],
        "contradiction_or_qualification": ["discussion", "limitation", "result", "conclusion"],
        "background": ["introduction", "background", "literature", "review"],
    }
    keys=priors.get(f, [])
    for k in keys:
        if k in hay:
            return 0.12, f"section_prior:{k}"
    return 0.0, ""


def infer_relation_from_citation_function(function: str) -> str:
    f=norm(function)
    if "contradiction" in f or "qualification" in f:
        return "qualifies"
    if "method" in f or "framework" in f:
        return "supports"
    if "definition" in f or "background" in f:
        return "supports"
    return "supports"


def page_match_score(candidate_page: str|None, locator: dict[str, Any]) -> tuple[float, str]:
    pages=set(locator.get("pages") or [])
    if not pages:
        return 0.0, ""
    if candidate_page and str(candidate_page) in pages:
        return 0.25, "page_match"
    return -0.08, "page_mismatch"


def citation_candidate_score(query: str, candidate_text: str, *, page: str|None, locator: dict[str, Any], function: str, heading: str="", status: str="", grade: str="", kind: str="") -> tuple[float, dict[str, Any]]:
    # Main signal: weighted coverage of substantive citation-context terms, not generic section priors.
    coverage, matched_terms, missing_terms = weighted_term_coverage(query, candidate_text)
    raw_overlap=token_overlap_score(query, candidate_text)
    phrase_bonus=0.0
    qterms=citation_terms(query)
    cnorm=norm(candidate_text)
    for n in [3,2]:
        for i in range(0, max(0, len(qterms)-n+1)):
            phrase=" ".join(qterms[i:i+n])
            if len(phrase) >= 10 and phrase in cnorm:
                phrase_bonus += 0.04 if n == 2 else 0.07
    phrase_bonus=min(0.18, phrase_bonus)
    pscore, pwhy=page_match_score(page, locator)
    # Section prior is gated: do not let a weak lexical match win merely because it is in Introduction/Literature.
    sscore, swhy=(0.0, "")
    if coverage >= 0.12 or raw_overlap >= 0.08:
        sscore, swhy=citation_section_prior(function, heading, candidate_text)
    hbonus, hwhy=heading_term_bonus(query, heading)
    grade_bonus={"A":0.08,"B":0.05,"C":0.02,"D":0.0,"X":-0.2}.get(grade,0.0)
    status_bonus={"verified":0.08,"needs_page_check":0.02,"candidate_needs_review":0.01,"needs_source_check":0.0,"superseded":-0.2,"rejected":-0.3}.get(status,0.0)
    penalty, penalty_reasons=span_quality_penalty(candidate_text, heading, kind)
    # Require a non-trivial substantive overlap unless a page locator explicitly matches.
    if coverage < 0.08 and not (pscore > 0):
        base=0.0
    else:
        base=coverage*0.62 + raw_overlap*0.18 + phrase_bonus + pscore + sscore + hbonus + grade_bonus + status_bonus - penalty
    score=max(0.0, base)
    why={
        "term_coverage": round(coverage,4), "raw_overlap": round(raw_overlap,4), "matched_terms": matched_terms[:12], "missing_terms": missing_terms[:12],
        "phrase_bonus": round(phrase_bonus,4), "page_score": pscore, "section_score": sscore, "heading_bonus": round(hbonus,4),
        "grade_bonus": grade_bonus, "status_bonus": status_bonus, "penalty": round(penalty,4),
        "why": [x for x in [pwhy,swhy,hwhy] if x] + penalty_reasons
    }
    return score, why


def candidate_page_for_span(source_id: str, start: int|None) -> str|None:
    try:
        return page_for_char(source_id, start)[0]
    except Exception:
        return None


def source_card_suggestion_overlap_bonus(conn: sqlite3.Connection, source_id: str, start: int|None, end: int|None) -> tuple[float, str]:
    """Small tie-breaker when a citation target overlaps an independently suggested source card.

    Deliberately tiny: source-card suggestions should not overpower direct citation-context matching.
    """
    if start is None or end is None:
        return 0.0, ""
    try:
        start_i=int(start); end_i=int(end)
    except Exception:
        return 0.0, ""
    try:
        rows=conn.execute("""
            SELECT suggestion_id, score, char_start, char_end FROM source_card_suggestions
            WHERE source_id=? AND status!='rejected' AND char_start IS NOT NULL AND char_end IS NOT NULL
              AND NOT (char_end < ? OR char_start > ?)
            ORDER BY score DESC LIMIT 5
        """, (source_id, start_i, end_i)).fetchall()
    except sqlite3.OperationalError:
        return 0.0, ""
    best=None; best_overlap=0.0
    span_len=max(1, end_i-start_i)
    for r in rows:
        rs=int(r["char_start"]); re_=int(r["char_end"])
        inter=max(0, min(end_i,re_) - max(start_i,rs))
        overlap=inter / max(1, min(span_len, re_-rs))
        if overlap > best_overlap:
            best=r; best_overlap=overlap
    if not best or best_overlap < 0.55:
        return 0.0, ""
    return min(0.035, float(best["score"] or 0)*0.025), f"source_card_suggestion:{best['suggestion_id']}"


def suggest_cited_claim_locations(context_id: str, *, limit: int=10, include_claims: bool=True, include_spans: bool=True, min_score: float=0.05) -> dict[str, Any]:
    init_db(True)
    conn=db(); ctx=conn.execute("SELECT * FROM citation_contexts WHERE context_id=?", (context_id,)).fetchone()
    if not ctx:
        conn.close(); raise SystemExit(f"Unknown context_id: {context_id}")
    c=dict(ctx)
    target_source=c.get("matched_source_id")
    if not target_source:
        conn.close(); return {"context_id": context_id, "error": "citation context has no matched_source_id", "suggestions": []}
    repaired_context=repaired_citation_context(c)
    c_for_query={**c, "context_text": repaired_context}
    query=normalize_citation_query(c_for_query)
    locator=parse_citation_locator(c.get("citation_text"), repaired_context)
    suggestions=[]
    relation=infer_relation_from_citation_function(c.get("citation_function") or "")
    source_text=""
    intro_cutoff=0
    backmatter_cutoff=10**18
    try:
        source_text=read_source_text(target_source)
        intro_cutoff=source_intro_start(source_text)
        backmatter_cutoff=source_backmatter_start(source_text)
    except SystemExit:
        source_text=""
        intro_cutoff=0
        backmatter_cutoff=10**18

    if include_claims:
        rows=conn.execute("SELECT * FROM source_cards WHERE source_id=? AND verification_status!='rejected'", (target_source,)).fetchall()
        for r in rows:
            d=dict(r); card=claim_card(d, "standard")
            # Ignore cards anchored before the introduction/front-matter boundary when offsets are available.
            if source_text and d.get("char_start") is not None and (int(d.get("char_start") or 0) < intro_cutoff or int(d.get("char_start") or 0) >= backmatter_cutoff):
                continue
            text=" ".join([card.get("claim") or "", card.get("evidence") or "", card.get("scope_note") or ""])
            if is_reference_or_backmatter("", text):
                continue
            score, why=citation_candidate_score(query, text, page=card.get("page"), locator=locator, function=c.get("citation_function") or "", status=card.get("status") or "", grade=card.get("evidence_grade") or "", kind="claim")
            if score >= min_score:
                suggestions.append({"target_type":"claim", "target_id":card["claim_id"], "source_id":target_source, "char_start":d.get("char_start"), "char_end":d.get("char_end"), "page_start":card.get("page") or "", "page_end":card.get("page") or "", "score":round(score,4), "score_json":why, "matched_text":short(text, 900), "suggested_relation":relation, "status":"candidate_location", "handle":claim_handle(card["claim_id"])})

    if include_spans:
        if source_text:
            # Prefer paragraph/source-range granularity; chunks are fallback, pages are too broad for citation support localization.
            span_rows=conn.execute("SELECT * FROM spans WHERE source_id=? AND kind IN ('paragraph','chunk') ORDER BY kind, char_start", (target_source,)).fetchall()
            for sp in span_rows:
                d=dict(sp)
                start=d.get("char_start"); end=d.get("char_end")
                if start is None or end is None: continue
                if int(start) < intro_cutoff or int(start) >= backmatter_cutoff:
                    continue
                try:
                    text=source_text[int(start):int(end)].strip()
                except Exception:
                    continue
                if not text or len(text) < 40: continue
                kind=d.get("kind") or "span"
                heading=d.get("heading") or ""
                if is_reference_or_backmatter(heading, text):
                    continue
                # Add tight sentence-level windows first; paragraph/chunk remains fallback.
                if kind == "paragraph":
                    for sw in sentence_window_candidates(target_source, d, source_text, query, locator, c.get("citation_function") or ""):
                        boost, boost_why=source_card_suggestion_overlap_bonus(conn, target_source, sw.get("char_start"), sw.get("char_end"))
                        if boost:
                            sw["score"] = round(sw["score"] + boost, 4)
                            sw.setdefault("score_json", {}).setdefault("why", []).append(boost_why)
                            sw.setdefault("score_json", {})["source_card_suggestion_boost"] = round(boost,4)
                        if sw["score"] >= min_score:
                            sw.update({"suggested_relation": relation, "status": "candidate_location"})
                            suggestions.append(sw)
                page=d.get("page_start") or candidate_page_for_span(target_source, start) or ""
                score, why=citation_candidate_score(query, text, page=page, locator=locator, function=c.get("citation_function") or "", heading=heading, kind=kind)
                boost, boost_why=source_card_suggestion_overlap_bonus(conn, target_source, start, end)
                if boost:
                    score += boost
                    why.setdefault("why", []).append(boost_why)
                    why["source_card_suggestion_boost"] = round(boost,4)
                if score >= min_score:
                    suggestions.append({"target_type":kind, "target_id":d.get("span_id"), "source_id":target_source, "char_start":start, "char_end":end, "page_start":page, "page_end":d.get("page_end") or page or "", "score":round(score,4), "score_json":why, "matched_text":short(text, 900), "suggested_relation":relation, "status":"candidate_location", "handle":source_range_handle(target_source, int(start), int(end))})
    conn.close()
    # Deduplicate exact ranges/claims and prefer higher scores.
    by_key={}
    for s in suggestions:
        key=(s["target_type"], s.get("target_id"), s.get("char_start"), s.get("char_end"))
        if key not in by_key or s["score"] > by_key[key]["score"]:
            by_key[key]=s
    ranked=sorted(by_key.values(), key=lambda x: x["score"], reverse=True)[:limit]
    return {"context_id": context_id, "citing_source_id": c.get("citing_source_id"), "matched_source_id": target_source, "citation_function": c.get("citation_function"), "citation_text": c.get("citation_text"), "context_text": c.get("context_text"), "repaired_context_text": repaired_context, "query_text": query, "intro_cutoff": intro_cutoff, "backmatter_cutoff": backmatter_cutoff, "locator": locator, "suggestions": ranked}


def store_citation_location_suggestions(packet: dict[str, Any]) -> list[str]:
    init_db(True)
    conn=db(); ids=[]
    for s in packet.get("suggestions", []):
        sid=f"CLOC-{sha1_short(packet['context_id'] + str(s.get('target_type')) + str(s.get('target_id')) + str(s.get('char_start')) + str(s.get('char_end')))[:14]}"
        conn.execute("""INSERT OR REPLACE INTO citation_location_suggestions
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            sid, packet["context_id"], packet.get("matched_source_id"), s.get("target_type"), s.get("target_id"), s.get("source_id"), s.get("char_start"), s.get("char_end"), s.get("page_start"), s.get("page_end"), float(s.get("score") or 0), json.dumps(s.get("score_json") or {}, ensure_ascii=False), packet.get("query_text") or "", s.get("matched_text") or "", s.get("suggested_relation") or "supports", s.get("status") or "candidate_location", now(), now()
        ))
        ids.append(sid)
    conn.commit(); conn.close(); return ids


def cmd_suggest_cited_claim_location(args):
    packet=suggest_cited_claim_locations(args.context_id, limit=max(args.limit*3,args.limit), include_claims=not args.no_claims, include_spans=not args.no_spans, min_score=args.min_score)
    if args.learned and packet.get("suggestions"):
        model=load_learned_rankers().get("citation_location", {})
        weights=model.get("weights") or {}
        for sug in packet["suggestions"]:
            ls=learned_term_score(" ".join([packet.get("query_text") or "", sug.get("matched_text") or ""]), weights)
            sug.setdefault("score_json", {})["learned_score"] = ls
            sug["score"] = round(float(sug.get("score") or 0) + ls * args.learned_weight, 4)
        packet["suggestions"] = sorted(packet["suggestions"], key=lambda x:x.get("score",0), reverse=True)[:args.limit]
    else:
        packet["suggestions"] = (packet.get("suggestions") or [])[:args.limit]
    if args.store and packet.get("suggestions"):
        ids=store_citation_location_suggestions(packet)
        for sid,sug in zip(ids, packet["suggestions"]): sug["suggestion_id"]=sid
    if args.json: print_json(packet)
    else:
        if packet.get("error"):
            print(packet["error"]); return
        print(f"# Citation location suggestions: {args.context_id}")
        print(f"Citing: {packet.get('citing_source_id')} -> {packet.get('matched_source_id')} | function={packet.get('citation_function')} | locator={packet.get('locator',{}).get('locator_raw') or 'none'}")
        print(f"Query: {packet.get('query_text')}")
        for s in packet.get("suggestions", []):
            sid=f" {s.get('suggestion_id')}" if s.get("suggestion_id") else ""
            print(f"- {sid} {s['target_type']} {s.get('target_id')} score:{s['score']} relation:{s.get('suggested_relation')} p.{s.get('page_start') or '?'} handle:{s.get('handle')}")
            why=s.get("score_json") or {}
            print(f"  why: coverage={why.get('term_coverage')} raw={why.get('raw_overlap')} page={why.get('page_score')} section={why.get('section_score')} matched={','.join(why.get('matched_terms') or [])} {', '.join(why.get('why') or [])}")
            print(f"  text: {short(s.get('matched_text'), 260)}")


def cmd_suggest_cited_claim_locations(args):
    init_db(True)
    conn=db(); rows=conn.execute("SELECT context_id FROM citation_contexts " + ("WHERE citing_source_id=? " if args.source_id else "") + "ORDER BY context_id LIMIT ?", ((args.source_id, args.limit) if args.source_id else (args.limit,))).fetchall(); conn.close()
    packets=[]; stored=0
    for r in rows:
        packet=suggest_cited_claim_locations(r["context_id"], limit=max(args.per_context*3,args.per_context), min_score=args.min_score)
        if args.learned and packet.get("suggestions"):
            model=load_learned_rankers().get("citation_location", {})
            weights=model.get("weights") or {}
            for sug in packet["suggestions"]:
                ls=learned_term_score(" ".join([packet.get("query_text") or "", sug.get("matched_text") or ""]), weights)
                sug.setdefault("score_json", {})["learned_score"] = ls
                sug["score"] = round(float(sug.get("score") or 0) + ls * args.learned_weight, 4)
            packet["suggestions"] = sorted(packet["suggestions"], key=lambda x:x.get("score",0), reverse=True)[:args.per_context]
        else:
            packet["suggestions"] = (packet.get("suggestions") or [])[:args.per_context]
        if args.store and packet.get("suggestions"):
            stored += len(store_citation_location_suggestions(packet))
        packets.append(packet)
    payload={"count": len(packets), "stored": stored, "packets": packets}
    if args.json: print_json(payload)
    else:
        print(f"Processed {len(packets)} citation contexts; stored {stored} suggestions")
        for p in packets:
            top=(p.get("suggestions") or [{}])[0]
            print(f"- {p.get('context_id')} -> {p.get('matched_source_id')} suggestions:{len(p.get('suggestions',[]))} top:{top.get('target_type')} {top.get('target_id')} score:{top.get('score')}")


def cmd_citation_location_suggestions(args):
    init_db(True)
    conn=db(); params=[]; where=[]
    if args.context_id: where.append("context_id=?"); params.append(args.context_id)
    if args.status: where.append("status=?"); params.append(args.status)
    sql="SELECT * FROM citation_location_suggestions" + (" WHERE "+" AND ".join(where) if where else "") + " ORDER BY score DESC LIMIT ?"
    rows=[dict(r) for r in conn.execute(sql, (*params,args.limit)).fetchall()]
    conn.close()
    for r in rows:
        try: r["score_json"]=json.loads(r.get("score_json") or "{}")
        except Exception: pass
    if args.json: print_json(rows)
    else:
        for r in rows:
            print(f"{r['suggestion_id']} | {r['context_id']} -> {r['target_type']}:{r['target_id']} score:{r['score']} {r['status']} p.{r.get('page_start') or '?'}")


def cmd_verify_location(args):
    init_db(True)
    conn=db(); row=conn.execute("SELECT suggestion_id FROM citation_location_suggestions WHERE suggestion_id=?", (args.suggestion_id,)).fetchone()
    if not row: raise SystemExit(f"Unknown suggestion_id: {args.suggestion_id}")
    conn.execute("UPDATE citation_location_suggestions SET status=?, updated_at=? WHERE suggestion_id=?", (args.status, now(), args.suggestion_id))
    if args.note:
        # Keep the note on the citation context verification_note if requested by user.
        ctx=conn.execute("SELECT context_id FROM citation_location_suggestions WHERE suggestion_id=?", (args.suggestion_id,)).fetchone()["context_id"]
        conn.execute("UPDATE citation_contexts SET verification_note=?, updated_at=? WHERE context_id=?", (args.note, now(), ctx))
    conn.commit(); conn.close(); print(f"{args.suggestion_id} -> {args.status}")


def cmd_accept_citation_location(args):
    init_db(True)
    conn=db(); row=conn.execute("SELECT * FROM citation_location_suggestions WHERE suggestion_id=?", (args.suggestion_id,)).fetchone()
    if not row: raise SystemExit(f"Unknown suggestion_id: {args.suggestion_id}")
    s=dict(row)
    conn.execute("UPDATE citation_location_suggestions SET status='accepted', updated_at=? WHERE suggestion_id=?", (now(), args.suggestion_id))
    if args.citing_claim_id and s.get("target_type") == "claim" and s.get("target_id"):
        for cid in [args.citing_claim_id, s["target_id"]]:
            if not conn.execute("SELECT 1 FROM source_cards WHERE claim_id=?", (cid,)).fetchone():
                raise SystemExit(f"Unknown claim_id: {cid}")
        rid=args.relation_id or f"REL-{sha1_short(args.citing_claim_id + s['target_id'] + args.relation_type)[:10]}"
        conn.execute("INSERT OR REPLACE INTO claim_relations VALUES (?,?,?,?,?,?,?)", (rid, args.citing_claim_id, s["target_id"], args.relation_type, args.note or f"Accepted from citation location {args.suggestion_id}", args.status or "candidate_needs_review", now()))
    else:
        rid=""
    conn.commit(); conn.close()
    print(f"accepted {args.suggestion_id}" + (f" -> relation {rid}" if rid else ""))


def section_role(heading: str, text: str="") -> str:
    h=norm(heading); sample=norm((heading or "") + " " + (text or "")[:900])
    if re.search(r"\b(references|bibliography)\b", h): return "references"
    if re.search(r"\b(abstract)\b", h): return "abstract"
    if re.search(r"\b(method|data|model|approach|materials)\b", sample): return "methods"
    if re.search(r"\b(result|finding|empirical|analysis)\b", sample): return "results"
    if re.search(r"\b(discussion|implication|policy|recommend|conclusion)\b", sample): return "discussion"
    if re.search(r"\b(limitation|caveat|future research|external validity|validity)\b", sample): return "limitations"
    if re.search(r"\b(theory|framework|conceptual|definition|defined as|taxonomy|classif)\b", sample): return "theory"
    if re.search(r"\b(introduction|background|literature|review|context)\b", sample): return "background"
    return "body"


SOURCE_CARD_CUE_PATTERNS = [
    ("definition", re.compile(r"\b(refers to|is defined as|we define|means|relates to)\b", re.I), 1.0),
    ("methodological claim", re.compile(r"\b(we review|we organise|we classify|we distinguish|method|framework|taxonomy|model|data)\b", re.I), 0.75),
    ("empirical finding", re.compile(r"\b(associated with|correlated with|increases?|decreases?|positively|negatively|significant|evidence shows|review shows|found to|influence[s]? adoption)\b", re.I), 1.0),
    ("policy implication", re.compile(r"\b(policy|policies|should|recommend|intervention|design|scheme|payments|subsidy|nudge)\b", re.I), 0.85),
    ("limitation", re.compile(r"\b(limitation|caveat|uncertain|validity|lack of|scant|few studies|not covered|cannot|may not)\b", re.I), 0.8),
    ("theoretical claim", re.compile(r"\b(theory|mechanism|bias|decision-making|behavioural factors|cognitive|social|dispositional)\b", re.I), 0.7),
]


def suggest_claim_type_and_role(text: str, heading: str="") -> tuple[str, str, list[str]]:
    scores={}; reasons=[]
    hay=f"{heading}\n{text}"
    for ctype, pat, weight in SOURCE_CARD_CUE_PATTERNS:
        hits=pat.findall(hay)
        if hits:
            scores[ctype]=scores.get(ctype,0)+weight+min(0.5, len(hits)*0.08)
            reasons.append(f"{ctype}:{len(hits)}")
    role=section_role(heading, text)
    if role == "methods": scores["methodological claim"]=scores.get("methodological claim",0)+0.45
    if role in {"results", "discussion"}: scores["empirical finding"]=scores.get("empirical finding",0)+0.18
    if role == "limitations": scores["limitation"]=scores.get("limitation",0)+0.45
    if role == "theory": scores["theoretical claim"]=scores.get("theoretical claim",0)+0.35
    if not scores:
        return "background", "background_card", ["fallback_background"]
    ctype=max(scores, key=scores.get)
    fake={"claim_type":ctype, "verification_status":"candidate_needs_review"}
    return ctype, card_role(fake), reasons


def source_card_candidate_score(text: str, heading: str="", kind: str="") -> tuple[float, dict[str, Any]]:
    words=re.findall(r"\b[A-Za-z][A-Za-z-]+\b", text or "")
    if len(words) < 18:
        return 0.0, {"reject":"too_short"}
    if len(words) > 180:
        length_score=0.55
    elif 28 <= len(words) <= 95:
        length_score=1.0
    else:
        length_score=0.78
    ctype, role, reasons=suggest_claim_type_and_role(text, heading)
    cue_strength=0.18*len(reasons)
    domain_terms=citation_terms(text)
    domain_density=min(1.0, len([t for t in domain_terms if t in DOMAIN_IMPORTANCE]) / 8)
    penalty, penalty_reasons=span_quality_penalty(text, heading, kind)
    sr=section_role(heading, text)
    section_bonus={"methods":0.08,"results":0.12,"discussion":0.14,"limitations":0.12,"theory":0.10,"background":0.04,"body":0.0}.get(sr,0.0)
    score=max(0.0, 0.35*length_score + cue_strength + 0.22*domain_density + section_bonus - penalty)
    return score, {"word_count":len(words), "length_score":round(length_score,3), "cue_reasons":reasons, "domain_density":round(domain_density,3), "section_role":sr, "section_bonus":section_bonus, "penalty":penalty, "penalty_reasons":penalty_reasons, "suggested_claim_type":ctype, "suggested_card_role":role}


def source_card_suggestion_candidates(source_id: str, *, limit: int=80, min_score: float=0.25) -> list[dict[str, Any]]:
    init_db(True)
    text=read_source_text(source_id)
    intro=source_intro_start(text); back=source_backmatter_start(text)
    conn=db(); spans=[dict(r) for r in conn.execute("SELECT * FROM spans WHERE source_id=? AND kind IN ('paragraph','chunk','section') ORDER BY char_start", (source_id,)).fetchall()]; conn.close()
    out=[]
    for sp in spans:
        start=sp.get("char_start"); end=sp.get("char_end")
        if start is None or end is None: continue
        start=int(start); end=int(end)
        if start < intro or start >= back: continue
        block=text[start:end].strip()
        if not block or is_reference_or_backmatter(sp.get("heading") or "", block): continue
        # Sentence windows from paragraphs/sections.
        if sp.get("kind") in {"paragraph", "section"}:
            for i,(ss,se,sent) in enumerate(sentence_spans(block, start)):
                # exact sentence
                score, why=source_card_candidate_score(sent, sp.get("heading") or "", "sentence_window")
                if score >= min_score:
                    out.append({"source_id":source_id,"target_type":"sentence_window","char_start":ss,"char_end":se,"page_start":candidate_page_for_span(source_id, ss) or "","page_end":candidate_page_for_span(source_id, se-1) or "","heading":sp.get("heading") or "","section_role":why.get("section_role"),"suggested_claim_type":why.get("suggested_claim_type"),"suggested_card_role":why.get("suggested_card_role"),"score":round(score+0.04,4),"score_json":why,"text":sent,"status":"candidate"})
                # two-sentence window
                sents=sentence_spans(block, start)
                if i+1 < len(sents):
                    wstart=ss; wend=sents[i+1][1]; wtxt=text[wstart:wend].strip()
                    score, why=source_card_candidate_score(wtxt, sp.get("heading") or "", "sentence_window")
                    if score >= min_score:
                        out.append({"source_id":source_id,"target_type":"sentence_window","char_start":wstart,"char_end":wend,"page_start":candidate_page_for_span(source_id, wstart) or "","page_end":candidate_page_for_span(source_id, wend-1) or "","heading":sp.get("heading") or "","section_role":why.get("section_role"),"suggested_claim_type":why.get("suggested_claim_type"),"suggested_card_role":why.get("suggested_card_role"),"score":round(score,4),"score_json":why,"text":wtxt,"status":"candidate"})
        # Paragraph as fallback if it is not too broad.
        if sp.get("kind") == "paragraph":
            score, why=source_card_candidate_score(block, sp.get("heading") or "", "paragraph")
            if score >= min_score and why.get("word_count",999) <= 170:
                out.append({"source_id":source_id,"target_type":"paragraph","char_start":start,"char_end":end,"page_start":candidate_page_for_span(source_id, start) or "","page_end":candidate_page_for_span(source_id, end-1) or "","heading":sp.get("heading") or "","section_role":why.get("section_role"),"suggested_claim_type":why.get("suggested_claim_type"),"suggested_card_role":why.get("suggested_card_role"),"score":round(score,4),"score_json":why,"text":block,"status":"candidate"})
    # Deduplicate overlapping identical texts/ranges and avoid returning many windows from same place.
    dedup={}
    for c in out:
        key=(c["char_start"], c["char_end"])
        if key not in dedup or c["score"] > dedup[key]["score"]:
            dedup[key]=c
    ranked=sorted(dedup.values(), key=lambda x: x["score"], reverse=True)
    return ranked[:limit]


def store_source_card_suggestions(candidates: list[dict[str, Any]]) -> list[str]:
    init_db(True)
    conn=db(); ids=[]
    for c in candidates:
        sid=f"SCSUG-{sha1_short(c['source_id'] + str(c['char_start']) + str(c['char_end']) + c.get('suggested_card_role',''))[:14]}"
        conn.execute("""INSERT OR REPLACE INTO source_card_suggestions
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            sid, c["source_id"], c.get("target_type"), c.get("char_start"), c.get("char_end"), c.get("page_start"), c.get("page_end"), c.get("heading"), c.get("section_role"), c.get("suggested_claim_type"), c.get("suggested_card_role"), float(c.get("score") or 0), json.dumps(c.get("score_json") or {}, ensure_ascii=False), c.get("text") or "", c.get("status") or "candidate", now(), now()
        ))
        ids.append(sid)
    conn.commit(); conn.close(); return ids


def cmd_suggest_source_cards(args):
    candidates=source_card_suggestion_candidates(args.source_id, limit=max(args.limit * 3, args.limit), min_score=args.min_score)
    if args.learned:
        model=load_learned_rankers().get("source_card", {})
        weights=model.get("weights") or {}
        for c in candidates:
            ls=learned_term_score(c.get("text") or "", weights)
            c.setdefault("score_json", {})["learned_score"] = ls
            c["score"] = round(float(c.get("score") or 0) + ls * args.learned_weight, 4)
        candidates=sorted(candidates, key=lambda x:x.get("score",0), reverse=True)[:args.limit]
    else:
        candidates=candidates[:args.limit]
    if args.store and candidates:
        ids=store_source_card_suggestions(candidates)
        for sid,c in zip(ids,candidates): c["suggestion_id"]=sid
    payload={"source_id":args.source_id,"count":len(candidates),"candidates":candidates}
    if args.json: print_json(payload)
    else:
        print(f"Source-card suggestions: {args.source_id} ({len(candidates)})")
        for c in candidates[:args.limit]:
            sid=f" {c.get('suggestion_id')}" if c.get("suggestion_id") else ""
            print(f"- {sid} score:{c['score']} {c.get('suggested_card_role')} / {c.get('suggested_claim_type')} {source_range_handle(c['source_id'], c['char_start'], c['char_end'])}")
            print(f"  {short(c.get('text'), 260)}")


def cmd_source_card_suggestions(args):
    init_db(True)
    conn=db(); params=[]; where=[]
    if args.source_id: where.append("source_id=?"); params.append(args.source_id)
    if args.status: where.append("status=?"); params.append(args.status)
    if args.card_role: where.append("suggested_card_role=?"); params.append(args.card_role)
    sql="SELECT * FROM source_card_suggestions" + (" WHERE "+" AND ".join(where) if where else "") + " ORDER BY score DESC LIMIT ?"
    rows=[dict(r) for r in conn.execute(sql, (*params,args.limit)).fetchall()]
    conn.close()
    for r in rows:
        try: r["score_json"]=json.loads(r.get("score_json") or "{}")
        except Exception: pass
    if args.json: print_json(rows)
    else:
        for r in rows:
            print(f"{r['suggestion_id']} | {r['source_id']} {r['suggested_card_role']} {r['suggested_claim_type']} score:{r['score']} {r['status']} {source_range_handle(r['source_id'], r['char_start'], r['char_end'])}")
            print(f"  {short(r.get('text'), 220)}")


def cmd_accept_source_card_suggestion(args):
    init_db(True)
    conn=db(); row=conn.execute("SELECT * FROM source_card_suggestions WHERE suggestion_id=?", (args.suggestion_id,)).fetchone()
    if not row: raise SystemExit(f"Unknown suggestion_id: {args.suggestion_id}")
    s=dict(row); conn.close()
    claim={"claim_id": args.claim_id or next_claim_id(s["source_id"]), "source_id": s["source_id"], "claim": args.claim or s["text"], "evidence": s["text"],
        "claim_representation": "source_range", "claim_type": args.claim_type or s.get("suggested_claim_type") or "unknown", "page": s.get("page_start") or "",
        "page_status": "page_matched_from_source_span" if s.get("page_start") else "needs_page_check", "verification_status": args.status,
        "confidence": args.confidence, "scope_note": args.scope_note or f"Accepted from source-card suggestion {args.suggestion_id}.",
        "char_start": s.get("char_start"), "char_end": s.get("char_end"), "line_start": None, "line_end": None, "extraction_mode": "source_card_suggestion"}
    try:
        offs=line_offsets(read_source_text(s["source_id"])); claim["line_start"]=line_no(offs, int(s["char_start"])); claim["line_end"]=line_no(offs, int(s["char_end"]))
    except SystemExit:
        pass
    upsert_claim(claim, {})
    conn=db(); conn.execute("UPDATE source_card_suggestions SET status='accepted', updated_at=? WHERE suggestion_id=?", (now(), args.suggestion_id)); conn.commit(); row=conn.execute("SELECT * FROM source_cards WHERE claim_id=?", (claim["claim_id"],)).fetchone(); conn.close()
    print_json({"accepted": args.suggestion_id, "claim": claim_card(row,"full")}) if args.json else print(f"Accepted {args.suggestion_id} -> {claim['claim_id']}")


def cmd_reject_source_card_suggestion(args):
    init_db(True)
    conn=db(); row=conn.execute("SELECT suggestion_id FROM source_card_suggestions WHERE suggestion_id=?", (args.suggestion_id,)).fetchone()
    if not row: raise SystemExit(f"Unknown suggestion_id: {args.suggestion_id}")
    conn.execute("UPDATE source_card_suggestions SET status='rejected', updated_at=? WHERE suggestion_id=?", (now(), args.suggestion_id))
    conn.commit(); conn.close(); print(f"{args.suggestion_id} -> rejected" + (f" ({args.label})" if args.label else ""))


def cmd_repair_citation_contexts(args):
    init_db(True)
    conn=db(); params=[]; where=[]
    if args.source_id:
        where.append("citing_source_id=?"); params.append(args.source_id)
    if args.context_id:
        where.append("context_id=?"); params.append(args.context_id)
    sql="SELECT * FROM citation_contexts" + (" WHERE "+" AND ".join(where) if where else "") + " ORDER BY citing_source_id, context_id LIMIT ?"
    rows=[dict(r) for r in conn.execute(sql, (*params,args.limit)).fetchall()]
    changes=[]
    for r in rows:
        repaired=repaired_citation_context(r)
        old=r.get("context_text") or ""
        if repaired and repaired.strip() != old.strip():
            changes.append({"context_id": r["context_id"], "old": old, "new": repaired, "old_len": len(old), "new_len": len(repaired)})
            if not args.dry_run:
                note=(r.get("verification_note") or "").strip()
                note=(note + " | " if note else "") + "context_text repaired to sentence boundary"
                conn.execute("UPDATE citation_contexts SET context_text=?, verification_note=?, updated_at=? WHERE context_id=?", (repaired, note, now(), r["context_id"]))
    if not args.dry_run:
        conn.commit()
    conn.close()
    payload={"dry_run": args.dry_run, "checked": len(rows), "changed": len(changes), "changes": changes[:args.show]}
    if args.json: print_json(payload)
    else:
        print(f"Citation context repair {'dry-run ' if args.dry_run else ''}checked {len(rows)} contexts; changed {len(changes)}")
        for ch in changes[:args.show]:
            print(f"- {ch['context_id']} {ch['old_len']} -> {ch['new_len']}")
            print(f"  old: {short(ch['old'], 180)}")
            print(f"  new: {short(ch['new'], 220)}")


# ---------- reference triage / reading priority / learned lightweight rankers ----------

LEARNED_RANKERS_PATH = REPORTS / "learned_rankers.json"


def load_learned_rankers() -> dict[str, Any]:
    if not LEARNED_RANKERS_PATH.exists():
        return {}
    try:
        return json.loads(LEARNED_RANKERS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def learned_term_score(text: str, weights: dict[str, float]|None) -> float:
    if not weights:
        return 0.0
    return round(sum(float(weights.get(t, 0.0)) for t in set(feature_terms(text))), 5)


def feature_terms(text: str) -> list[str]:
    terms=list(citation_terms(text))
    for a,b in zip(terms, terms[1:]):
        if a not in CITATION_STOPWORDS and b not in CITATION_STOPWORDS:
            terms.append(f"{a}_{b}")
    return terms


def train_term_delta(pos_texts: list[str], neg_texts: list[str]) -> dict[str, float]:
    def counts(texts):
        c=collections.Counter()
        for txt in texts:
            c.update(set(feature_terms(txt)))
        return c
    pc=counts(pos_texts); nc=counts(neg_texts); vocab=set(pc)|set(nc)
    pden=max(1,len(pos_texts)); nden=max(1,len(neg_texts)); out={}
    for t in vocab:
        val=((pc[t]+0.5)/(pden+1))-((nc[t]+0.5)/(nden+1))
        if abs(val) >= 0.03: out[t]=round(val,5)
    return dict(sorted(out.items(), key=lambda kv: abs(kv[1]), reverse=True)[:1500])


def cmd_train_rankers(args):
    init_db(True); conn=db()
    sc_rows=[dict(r) for r in conn.execute("SELECT * FROM source_card_suggestions WHERE status IN ('accepted','rejected')").fetchall()]
    cl_rows=[dict(r) for r in conn.execute("SELECT * FROM citation_location_suggestions WHERE status IN ('accepted','rejected','weak_match')").fetchall()]
    sc_pos=[r.get("text") or "" for r in sc_rows if r.get("status") == "accepted"]
    sc_neg=[r.get("text") or "" for r in sc_rows if r.get("status") == "rejected"]
    cl_pos=[" ".join([r.get("query_text") or "", r.get("matched_text") or ""]) for r in cl_rows if r.get("status") == "accepted"]
    cl_neg=[" ".join([r.get("query_text") or "", r.get("matched_text") or ""]) for r in cl_rows if r.get("status") in {"rejected","weak_match"}]
    model={"created_at": now(), "source_card": {"positive": len(sc_pos), "negative": len(sc_neg), "weights": train_term_delta(sc_pos, sc_neg)}, "citation_location": {"positive": len(cl_pos), "negative": len(cl_neg), "weights": train_term_delta(cl_pos, cl_neg)}}
    REPORTS.mkdir(exist_ok=True); LEARNED_RANKERS_PATH.write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding="utf-8")
    conn.close()
    if args.json: print_json(model)
    else:
        print(f"Wrote {LEARNED_RANKERS_PATH.relative_to(ROOT)}")
        print(f"source_card labels: +{len(sc_pos)} / -{len(sc_neg)}")
        print(f"citation_location labels: +{len(cl_pos)} / -{len(cl_neg)}")


def cmd_reference_match_queue(args):
    init_db(True); conn=db(); where=[]; params=[]
    if args.source_id: where.append("source_id=?"); params.append(args.source_id)
    if args.status: where.append("status=?"); params.append(args.status)
    refs=[dict(r) for r in conn.execute("SELECT * FROM source_references" + (" WHERE "+" AND ".join(where) if where else "") + " ORDER BY source_id, reference_id LIMIT ?", (*params,args.limit)).fetchall()]
    sources=[dict(r) for r in conn.execute("SELECT source_id,title,authors,year,doi FROM sources").fetchall()]
    out=[]
    for r in refs:
        candidates=[]
        for src in sources:
            if src["source_id"] == r["source_id"]: continue
            score=0.0; why=[]
            if normalize_doi(r.get("doi")) and normalize_doi(src.get("doi")) == normalize_doi(r.get("doi")):
                score += 1.0; why.append("doi")
            if r.get("year") and str(src.get("year") or "")[:4] == year_base(r.get("year")):
                score += 0.15; why.append("year")
            ak=r.get("author_key") or ""
            if ak and (ak in source_author_keys(src.get("authors")) or ak in compact_key((src.get("authors") or "") + " " + (src.get("title") or ""))):
                score += 0.35; why.append("author")
            title_terms=set(citation_terms(r.get("title") or r.get("raw_text") or "")); src_terms=set(citation_terms(src.get("title") or ""))
            if title_terms:
                ov=len(title_terms & src_terms)/max(1,len(title_terms)); score += ov*0.35
                if ov: why.append(f"title_overlap:{round(ov,2)}")
            if score>0: candidates.append({"source_id":src["source_id"],"title":src.get("title"),"doi":src.get("doi"),"score":round(score,3),"why":why})
        out.append({"reference":r,"candidate_matches":sorted(candidates,key=lambda x:x["score"],reverse=True)[:args.candidates],"priority":len(candidates)+(5 if r.get("status") == "missing_source" else 0)})
    conn.close(); out=sorted(out,key=lambda x:x["priority"],reverse=True)
    if args.json: print_json({"count":len(out),"items":out})
    else:
        print(f"Reference match queue: {len(out)}")
        for item in out:
            r=item["reference"]; print(f"- {r['reference_id']} [{r.get('status')}] {r.get('author_key')} {r.get('year')} · {short(r.get('title') or r.get('raw_text'), 120)}")
            for c in item["candidate_matches"]: print(f"  candidate {c['source_id']} score:{c['score']} why:{','.join(c['why'])}")


def cmd_resolve_reference(args):
    init_db(True); conn=db(); ref=conn.execute("SELECT * FROM source_references WHERE reference_id=?", (args.reference_id,)).fetchone()
    if not ref: raise SystemExit(f"Unknown reference_id: {args.reference_id}")
    if args.matched_source_id and not conn.execute("SELECT 1 FROM sources WHERE source_id=?", (args.matched_source_id,)).fetchone(): raise SystemExit(f"Unknown matched source_id: {args.matched_source_id}")
    status=args.status or ("matched_local" if args.matched_source_id else "missing_source")
    conn.execute("UPDATE source_references SET matched_source_id=?, status=? WHERE reference_id=?", (args.matched_source_id or "", status, args.reference_id))
    conn.execute("UPDATE citation_contexts SET matched_source_id=?, verification_status=?, updated_at=? WHERE reference_id=?", (args.matched_source_id or "", "needs_verification" if args.matched_source_id else "missing_source", now(), args.reference_id))
    conn.commit(); conn.close(); print(f"{args.reference_id} -> {args.matched_source_id or 'NONE'} ({status})")


def cmd_reading_priority(args):
    init_db(True); conn=db(); where=[]; params=[]
    if args.source_id: where.append("sr.source_id=?"); params.append(args.source_id)
    rows=[dict(r) for r in conn.execute(f"""SELECT sr.*, COUNT(cc.context_id) AS cite_count, GROUP_CONCAT(substr(cc.context_text,1,240),' || ') AS contexts FROM source_references sr LEFT JOIN citation_contexts cc ON cc.reference_id=sr.reference_id {('WHERE '+ ' AND '.join(where)) if where else ''} GROUP BY sr.reference_id""", params).fetchall()]
    q=args.query or ""; items=[]
    for r in rows:
        if args.missing_only and r.get("matched_source_id"): continue
        text=" ".join([r.get("title") or "", r.get("raw_text") or "", r.get("contexts") or ""]); qscore=token_overlap_score(q,text) if q else 0.0
        score=(r.get("cite_count") or 0)*1.0 + qscore*3.0 + (0.4 if normalize_doi(r.get("doi")) else 0.0) + (0.5 if not r.get("matched_source_id") else 0.0)
        if score <= 0 and q: continue
        items.append({"score":round(score,3),"reference_id":r["reference_id"],"source_id":r["source_id"],"author_key":r.get("author_key"),"year":r.get("year"),"title":r.get("title"),"doi":r.get("doi"),"matched_source_id":r.get("matched_source_id"),"status":r.get("status"),"citation_context_count":r.get("cite_count"),"context_preview":short(r.get("contexts"),260)})
    conn.close(); items=sorted(items,key=lambda x:x["score"], reverse=True)[:args.limit]
    if args.json: print_json({"count":len(items),"items":items})
    else:
        print(f"Reading priority: {len(items)}")
        for x in items:
            print(f"- score:{x['score']} cites:{x['citation_context_count']} {x['author_key']} {x['year']} {x.get('title') or x['reference_id']} doi:{x.get('doi') or '-'} matched:{x.get('matched_source_id') or '-'}")
            if x.get("context_preview"): print(f"  contexts: {x['context_preview']}")


def assess_citation_support(context_text: str, matched_text: str, score: float=0.0) -> dict[str, Any]:
    c=norm(context_text); m=norm(matched_text); flags=[]; verdict="needs_review"
    if score >= 0.55: verdict="likely_supported"
    elif score >= 0.30: verdict="possibly_supported"
    elif score > 0: verdict="weak_match"
    else: verdict="no_match"
    if any(k in c for k in ["cause","causes","drives","determines","leads to"]) and any(k in m for k in ["associated","correlated","related","may","might"]):
        flags.append("possibly_overstated_causal_language"); verdict="possibly_overstated"
    if (any(k in c for k in ["positive","increase","higher","boost"]) and any(k in m for k in ["negative","decrease","lower","no effect","mixed"])) or (any(k in c for k in ["negative","decrease","lower"]) and any(k in m for k in ["positive","increase","higher"])):
        flags.append("possible_direction_mismatch"); verdict="needs_review"
    return {"verdict":verdict,"flags":flags}


def cmd_assess_citation_location(args):
    init_db(True); conn=db(); row=conn.execute("SELECT * FROM citation_location_suggestions WHERE suggestion_id=?", (args.suggestion_id,)).fetchone()
    if not row: raise SystemExit(f"Unknown suggestion_id: {args.suggestion_id}")
    sug=dict(row); ctx=conn.execute("SELECT * FROM citation_contexts WHERE context_id=?", (sug["context_id"],)).fetchone(); conn.close()
    assessment=assess_citation_support_deep((dict(ctx).get("context_text") if ctx else sug.get("query_text")) or "", sug.get("matched_text") or "", float(sug.get("score") or 0), (dict(ctx).get("citation_function") if ctx else ""))
    payload={"suggestion_id":args.suggestion_id,"context_id":sug.get("context_id"),"score":sug.get("score"),"assessment":assessment,"suggestion":sug}
    if args.json: print_json(payload)
    else:
        print(f"{args.suggestion_id}: {assessment['verdict']} score:{sug.get('score')}")
        if assessment["flags"]: print("flags: " + ", ".join(assessment["flags"]))
        print(short(sug.get("matched_text"), 320))


def reference_match_score_for_source(ref: dict[str, Any], src: dict[str, Any]) -> tuple[float, list[str]]:
    score=0.0; why=[]
    if normalize_doi(ref.get("doi")) and normalize_doi(src.get("doi")) == normalize_doi(ref.get("doi")):
        score += 1.0; why.append("doi")
    if ref.get("year") and str(src.get("year") or "")[:4] == year_base(ref.get("year")):
        score += 0.15; why.append("year")
    ak=ref.get("author_key") or ""
    if ak and (ak in source_author_keys(src.get("authors")) or ak in compact_key((src.get("authors") or "") + " " + (src.get("title") or ""))):
        score += 0.35; why.append("author")
    title_terms=set(citation_terms(ref.get("title") or ref.get("raw_text") or "")); src_terms=set(citation_terms(src.get("title") or ""))
    if title_terms:
        ov=len(title_terms & src_terms)/max(1,len(title_terms)); score += ov*0.35
        if ov: why.append(f"title_overlap:{round(ov,2)}")
    return round(score,3), why


def cmd_backfill_source_matches(args):
    init_db(True)
    conn=db(); target=conn.execute("SELECT * FROM sources WHERE source_id=?", (args.source_id,)).fetchone()
    if not target: raise SystemExit(f"Unknown source_id: {args.source_id}")
    target=dict(target)
    refs=[dict(r) for r in conn.execute("SELECT * FROM source_references WHERE source_id!=?", (args.source_id,)).fetchall()]
    matches=[]; updated_ctx=0
    for r in refs:
        if r.get("matched_source_id") and not args.force:
            continue
        score, why=reference_match_score_for_source(r,target)
        if score >= args.min_score:
            matches.append({"reference_id":r["reference_id"],"citing_source_id":r["source_id"],"score":score,"why":why,"old_match":r.get("matched_source_id")})
            if not args.dry_run:
                conn.execute("UPDATE source_references SET matched_source_id=?, status=? WHERE reference_id=?", (args.source_id, "matched_local", r["reference_id"]))
                cur=conn.execute("UPDATE citation_contexts SET matched_source_id=?, verification_status=?, updated_at=? WHERE reference_id=?", (args.source_id, "needs_verification", now(), r["reference_id"]))
                updated_ctx += cur.rowcount
    if not args.dry_run: conn.commit()
    conn.close()
    payload={"source_id":args.source_id,"dry_run":args.dry_run,"matches":matches,"references_matched":len(matches),"citation_contexts_updated":updated_ctx}
    if args.json: print_json(payload)
    else:
        print(f"Backfill {args.source_id}: matched {len(matches)} references; updated {updated_ctx} citation contexts" + (" (dry run)" if args.dry_run else ""))
        for m in matches[:args.limit]: print(f"- {m['reference_id']} from {m['citing_source_id']} score:{m['score']} why:{','.join(m['why'])}")


def citation_context_ids_for_matched_source(source_id: str, limit: int=100000) -> list[str]:
    init_db(True); conn=db()
    rows=conn.execute("SELECT context_id FROM citation_contexts WHERE matched_source_id=? ORDER BY citing_source_id, context_id LIMIT ?", (source_id, limit)).fetchall()
    conn.close(); return [r["context_id"] for r in rows]


def cmd_suggest_locations_for_cited_source(args):
    ids=citation_context_ids_for_matched_source(args.source_id, args.limit)
    packets=[]; stored=0
    for cid in ids:
        packet=suggest_cited_claim_locations(cid, limit=args.per_context, min_score=args.min_score)
        if args.store and packet.get("suggestions"):
            stored += len(store_citation_location_suggestions(packet))
        packets.append(packet)
    payload={"source_id":args.source_id,"contexts":len(ids),"stored":stored,"packets":packets}
    if args.json: print_json(payload)
    else:
        print(f"Cited-source suggestions for {args.source_id}: contexts={len(ids)} stored={stored}")
        for p in packets[:args.show]:
            top=(p.get("suggestions") or [{}])[0]
            print(f"- {p.get('context_id')} from {p.get('citing_source_id')} top:{top.get('target_type')} {top.get('target_id')} score:{top.get('score')}")


def overlap_ratio(a0:int,a1:int,b0:int,b1:int) -> float:
    inter=max(0,min(a1,b1)-max(a0,b0)); return inter/max(1,min(a1-a0,b1-b0))


def source_location_usage(source_id: str, *, min_overlap: float=0.45) -> list[dict[str, Any]]:
    init_db(True)
    conn=db(); rows=[dict(r) for r in conn.execute("""
        SELECT cls.*, cc.citing_source_id, cc.citation_function, cc.verification_status AS context_status
        FROM citation_location_suggestions cls
        LEFT JOIN citation_contexts cc ON cc.context_id=cls.context_id
        WHERE cls.source_id=? AND cls.char_start IS NOT NULL AND cls.char_end IS NOT NULL
        ORDER BY cls.char_start, cls.char_end
    """, (source_id,)).fetchall()]
    conn.close()
    clusters=[]
    for r in rows:
        try: start=int(r["char_start"]); end=int(r["char_end"])
        except Exception: continue
        placed=False
        for cl in clusters:
            if overlap_ratio(start,end,cl["char_start"],cl["char_end"]) >= min_overlap:
                cl["char_start"]=min(cl["char_start"],start); cl["char_end"]=max(cl["char_end"],end); cl["items"].append(r); placed=True; break
        if not placed: clusters.append({"source_id":source_id,"char_start":start,"char_end":end,"items":[r]})
    out=[]
    for cl in clusters:
        items=cl["items"]; statuses=collections.Counter(i.get("status") or "" for i in items); funcs=collections.Counter(i.get("citation_function") or "" for i in items); citing={i.get("citing_source_id") for i in items if i.get("citing_source_id")}
        accepted=statuses.get("accepted",0); weak=statuses.get("weak_match",0); rejected=statuses.get("rejected",0); candidates=statuses.get("candidate_location",0)
        strength=round(len(items)*0.35 + len(citing)*0.55 + accepted*1.2 + candidates*0.1 - weak*0.35 - rejected*1.0,3)
        status_rank={"accepted":4,"candidate_location":2,"needs_review":1,"weak_match":0,"rejected":-2}
        best=max(items, key=lambda i: (status_rank.get(i.get("status"),0), float(i.get("score") or 0)))
        rep_start=int(best.get("char_start") or cl["char_start"]); rep_end=int(best.get("char_end") or cl["char_end"])
        out.append({"source_id":source_id,"char_start":cl["char_start"],"char_end":cl["char_end"],"representative_char_start":rep_start,"representative_char_end":rep_end,"handle":source_range_handle(source_id,rep_start,rep_end),"cluster_handle":source_range_handle(source_id,cl["char_start"],cl["char_end"]),"strength":strength,"context_count":len(items),"citing_source_count":len(citing),"accepted":accepted,"weak":weak,"rejected":rejected,"candidate":candidates,"status_counts":dict(statuses),"citation_functions":dict(funcs),"representative_suggestion_id":best.get("suggestion_id"),"suggestion_ids":[i["suggestion_id"] for i in items[:20]]})
    return sorted(out,key=lambda x:x["strength"],reverse=True)


def cmd_source_location_usage(args):
    usage=source_location_usage(args.source_id, min_overlap=args.min_overlap)[:args.limit]
    if args.json: print_json({"source_id":args.source_id,"count":len(usage),"locations":usage})
    else:
        print(f"Source-location usage: {args.source_id} ({len(usage)})")
        for u in usage:
            print(f"- strength:{u['strength']} contexts:{u['context_count']} sources:{u['citing_source_count']} accepted:{u['accepted']} weak:{u['weak']} rejected:{u['rejected']} {u['handle']}")
            print(f"  functions: {u['citation_functions']}")


def cmd_promote_cited_locations(args):
    init_db(True)
    locs=[u for u in source_location_usage(args.source_id) if u["strength"] >= args.min_strength][:args.limit]
    text=""
    try: text=read_source_text(args.source_id)
    except SystemExit: text=""
    candidates=[]
    for u in locs:
        start=u.get("representative_char_start", u["char_start"]); end=u.get("representative_char_end", u["char_end"])
        snippet=source_slice(args.source_id,start,end) if text else ""
        if not snippet: continue
        score, why=source_card_candidate_score(snippet, "", "citation_usage")
        usage_boost=min(0.45, u["strength"]*0.08)
        why["citation_usage_strength"]=u["strength"]; why["citation_usage_boost"]=round(usage_boost,4); why["citation_context_count"]=u["context_count"]
        ctype=why.get("suggested_claim_type") or infer_claim_type(snippet); role=why.get("suggested_card_role") or card_role({"claim_type":ctype})
        candidates.append({"source_id":args.source_id,"target_type":"citation_usage_range","char_start":start,"char_end":end,"page_start":candidate_page_for_span(args.source_id,start) or "","page_end":candidate_page_for_span(args.source_id,end-1) or "","heading":"","section_role":"cited_location","suggested_claim_type":ctype,"suggested_card_role":role,"score":round(score+usage_boost,4),"score_json":why,"text":snippet,"status":"candidate"})
    ids=[]
    if args.store and candidates:
        ids=store_source_card_suggestions(candidates)
        for sid,c in zip(ids,candidates): c["suggestion_id"]=sid
    if args.json: print_json({"source_id":args.source_id,"count":len(candidates),"stored":len(ids),"candidates":candidates})
    else:
        print(f"Promoted cited locations for {args.source_id}: {len(candidates)} candidates" + (f", stored {len(ids)}" if ids else ""))
        for c in candidates:
            print(f"- score:{c['score']} {c['suggested_card_role']} {source_range_handle(c['source_id'],c['char_start'],c['char_end'])}")
            print(f"  {short(c['text'],220)}")


# ---------- graph analytics / draft red-team ----------

def claim_usage_from_citation_locations() -> dict[str, dict[str, Any]]:
    init_db(True); conn=db()
    rows=[dict(r) for r in conn.execute("""
        SELECT cls.*, cc.citing_source_id, cc.citation_function
        FROM citation_location_suggestions cls
        LEFT JOIN citation_contexts cc ON cc.context_id=cls.context_id
        WHERE cls.target_type='claim' AND cls.target_id IS NOT NULL
    """).fetchall()]
    conn.close(); out={}
    for r in rows:
        cid=r.get("target_id"); d=out.setdefault(cid,{"contexts":0,"citing_sources":set(),"accepted":0,"weak":0,"rejected":0,"candidate":0,"functions":collections.Counter()})
        d["contexts"] += 1
        if r.get("citing_source_id"): d["citing_sources"].add(r["citing_source_id"])
        st=r.get("status") or "candidate_location"
        if st == "accepted": d["accepted"] += 1
        elif st == "weak_match": d["weak"] += 1
        elif st == "rejected": d["rejected"] += 1
        else: d["candidate"] += 1
        if r.get("citation_function"): d["functions"][r["citation_function"]]+=1
    for d in out.values():
        d["citing_source_count"]=len(d["citing_sources"]); d["citing_sources"]=sorted(d["citing_sources"]); d["functions"]=dict(d["functions"])
    return out


def centrality_score(card: dict[str, Any], usage: dict[str, Any], brief_count: int, relation_count: int) -> float:
    grade_bonus={"A":1.0,"B":0.7,"C":0.25,"D":0.0,"X":-1.0}.get(card.get("evidence_grade"),0)
    status_bonus={"verified":1.0,"needs_page_check":0.3,"candidate_needs_review":0.2,"needs_source_check":0.1,"superseded":-1,"rejected":-2}.get(card.get("status"),0)
    return round(usage.get("contexts",0)*0.5 + usage.get("citing_source_count",0)*1.0 + usage.get("accepted",0)*2.0 - usage.get("rejected",0)*1.5 + brief_count*0.8 + relation_count*0.4 + grade_bonus + status_bonus, 3)


def cmd_central_claims(args):
    init_db(True); conn=db(); usage=claim_usage_from_citation_locations(); briefs=brief_usage_counts(); rows=conn.execute("SELECT * FROM source_cards WHERE verification_status!='rejected'").fetchall(); out=[]
    for r in rows:
        card=claim_card(r,"standard"); cid=card["claim_id"]
        if args.source_id and card["source_id"] != args.source_id: continue
        if args.topic:
            hay=" ".join([card.get("claim") or "", card.get("evidence") or "", " ".join(tags_for_claim(cid))])
            if token_overlap_score(args.topic, hay) <= 0: continue
        rel_count=conn.execute("SELECT COUNT(*) FROM claim_relations WHERE claim_a=? OR claim_b=?", (cid,cid)).fetchone()[0]
        u=usage.get(cid,{"contexts":0,"citing_source_count":0,"accepted":0,"weak":0,"rejected":0,"candidate":0,"functions":{}})
        score=centrality_score(card,u,briefs.get(cid,0),rel_count)
        if score <= 0 and args.nonzero: continue
        out.append({**card,"centrality_score":score,"citation_usage":u,"brief_count":briefs.get(cid,0),"relation_count":rel_count})
    conn.close(); out=sorted(out,key=lambda x:x["centrality_score"], reverse=True)[:args.limit]
    if args.json: print_json({"count":len(out),"items":out})
    else:
        print(f"Central claims: {len(out)}")
        for c in out:
            print(f"- {c['claim_id']} centrality:{c['centrality_score']} grade:{c['evidence_grade']} {c['status']} cited:{c['citation_usage'].get('contexts',0)} briefs:{c['brief_count']} rel:{c['relation_count']}")
            print(f"  {short(c['claim'],220)}")


def cmd_most_cited_unverified(args):
    class Obj: pass
    o=Obj(); o.source_id=args.source_id; o.topic=args.topic; o.limit=args.limit*5; o.nonzero=True; o.json=True
    init_db(True); conn=db(); usage=claim_usage_from_citation_locations(); rows=conn.execute("SELECT * FROM source_cards WHERE verification_status NOT IN ('verified','rejected','superseded')").fetchall(); out=[]
    for r in rows:
        card=claim_card(r,"standard"); cid=card["claim_id"]
        if args.source_id and card["source_id"] != args.source_id: continue
        u=usage.get(cid,{"contexts":0,"citing_source_count":0,"accepted":0,"weak":0,"rejected":0,"candidate":0,"functions":{}})
        if not u.get("contexts") and args.cited_only: continue
        score=centrality_score(card,u,brief_usage_counts().get(cid,0),0)
        out.append({**card,"centrality_score":score,"citation_usage":u})
    conn.close(); out=sorted(out,key=lambda x:x["centrality_score"], reverse=True)[:args.limit]
    if args.json: print_json({"count":len(out),"items":out})
    else:
        print(f"Most-cited unverified: {len(out)}")
        for c in out:
            print(f"- {c['claim_id']} score:{c['centrality_score']} cited:{c['citation_usage'].get('contexts',0)} {c['status']} {short(c['claim'],180)}")


def cmd_source_neighborhood(args):
    init_db(True); conn=db(); src=conn.execute("SELECT * FROM sources WHERE source_id=?", (args.source_id,)).fetchone()
    if not src: raise SystemExit(f"Unknown source_id: {args.source_id}")
    outgoing=[dict(r) for r in conn.execute("SELECT * FROM citation_contexts WHERE citing_source_id=? ORDER BY context_id LIMIT ?", (args.source_id,args.limit)).fetchall()]
    incoming=[dict(r) for r in conn.execute("SELECT * FROM citation_contexts WHERE matched_source_id=? ORDER BY citing_source_id, context_id LIMIT ?", (args.source_id,args.limit)).fetchall()]
    refs=[dict(r) for r in conn.execute("SELECT * FROM source_references WHERE source_id=? ORDER BY reference_id LIMIT ?", (args.source_id,args.limit)).fetchall()]
    cards=[claim_card(r,"minimal") for r in conn.execute("SELECT * FROM source_cards WHERE source_id=? ORDER BY claim_id LIMIT ?", (args.source_id,args.limit)).fetchall()]
    conn.close(); payload={"source":dict(src),"outgoing_citation_contexts":outgoing,"incoming_citation_contexts":incoming,"references":refs,"source_cards":cards,"location_usage":source_location_usage(args.source_id)[:args.limit]}
    if args.json: print_json(payload)
    else:
        print(f"# Source neighborhood: {args.source_id}")
        print(f"Title: {src['title']}")
        print(f"Outgoing citation contexts: {len(outgoing)} | Incoming citation contexts: {len(incoming)} | Source cards: {len(cards)}")
        if incoming:
            print("\nIncoming citations:")
            for c in incoming[:10]: print(f"- {c['citing_source_id']} {c['context_id']} {short(c.get('context_text'),180)}")
        if payload["location_usage"]:
            print("\nTop cited locations:")
            for u in payload["location_usage"][:10]: print(f"- strength:{u['strength']} contexts:{u['context_count']} {u['handle']}")


def cmd_claim_network(args):
    init_db(True); conn=db(); usage=claim_usage_from_citation_locations(); nodes=[]; edges=[]
    if args.source_id:
        source_ids=[args.source_id]
    else:
        source_ids=[r["source_id"] for r in conn.execute("SELECT source_id FROM sources ORDER BY source_id").fetchall()]
    for sid in source_ids:
        nodes.append({"id":f"source:{sid}","kind":"source","label":sid})
    cards=[dict(r) for r in conn.execute("SELECT * FROM source_cards" + (" WHERE source_id=?" if args.source_id else ""), ((args.source_id,) if args.source_id else ())).fetchall()]
    for r in cards[:args.limit]:
        card=claim_card(r,"minimal"); cid=card["claim_id"]
        nodes.append({"id":f"claim:{cid}","kind":"claim","label":cid,"source_id":card["source_id"],"status":card["status"],"grade":card["evidence_grade"],"centrality":centrality_score(card,usage.get(cid,{}),brief_usage_counts().get(cid,0),0)})
        edges.append({"source":f"source:{card['source_id']}","target":f"claim:{cid}","kind":"has_card"})
    for r in conn.execute("SELECT * FROM claim_relations LIMIT ?", (args.limit,)).fetchall():
        edges.append({"source":f"claim:{r['claim_a']}","target":f"claim:{r['claim_b']}","kind":r["relation_type"],"status":r["status"]})
    for r in conn.execute("SELECT citing_source_id, matched_source_id, COUNT(*) n FROM citation_contexts WHERE matched_source_id IS NOT NULL AND matched_source_id!='' GROUP BY citing_source_id, matched_source_id LIMIT ?", (args.limit,)).fetchall():
        nodes.append({"id":f"source:{r['citing_source_id']}","kind":"source","label":r['citing_source_id']})
        nodes.append({"id":f"source:{r['matched_source_id']}","kind":"source","label":r['matched_source_id']})
        edges.append({"source":f"source:{r['citing_source_id']}","target":f"source:{r['matched_source_id']}","kind":"cites","weight":r["n"]})
    conn.close(); payload={"nodes":nodes,"edges":edges}
    if args.json: print_json(payload)
    else:
        print(f"Claim network: {len(nodes)} nodes, {len(edges)} edges")
        for e in edges[:args.limit]: print(f"- {e['source']} --{e['kind']}--> {e['target']}")


def cmd_redteam_draft(args):
    text=Path(args.path).read_text(encoding="utf-8", errors="ignore")
    ids=draft_claim_ids(text); conn=db(); known=[]; unknown=[]
    for cid in ids:
        row=conn.execute("SELECT * FROM source_cards WHERE claim_id=?", (cid,)).fetchone()
        if row: known.append(claim_card(row,"standard"))
        else: unknown.append(cid)
    status_counts=collections.Counter(c["status"] for c in known); grade_counts=collections.Counter(c["evidence_grade"] for c in known); source_counts=collections.Counter(c["source_id"] for c in known)
    issues=[]
    for c in known:
        if c["status"] != "verified": issues.append({"severity":"high","type":"unverified_claim_used","claim_id":c["claim_id"],"detail":c["status"]})
        if c["evidence_grade"] in {"C","D","X"}: issues.append({"severity":"medium","type":"weak_evidence_grade","claim_id":c["claim_id"],"detail":c["evidence_grade"]})
        if not c.get("page"): issues.append({"severity":"medium","type":"missing_page","claim_id":c["claim_id"]})
    uncited=substantive_sentences_without_claim_ids(text,args.min_words)
    for u in uncited[:args.show_uncited]: issues.append({"severity":"medium","type":"uncited_substantive_sentence","sentence_no":u["sentence_no"],"detail":u["text"]})
    if len(source_counts)==1 and len(known)>=args.min_claims_for_source_warning:
        issues.append({"severity":"medium","type":"single_source_dependency","detail":dict(source_counts)})
    if unknown: issues.append({"severity":"high","type":"unknown_claim_ids","detail":unknown})
    # Simple overclaim lexicon in uncited text.
    for u in uncited:
        if re.search(r"\b(proves|demonstrates|always|never|causes|determines)\b", u["text"], re.I):
            issues.append({"severity":"medium","type":"strong_language_without_claim","sentence_no":u["sentence_no"],"detail":u["text"]})
    payload={"draft":str(args.path),"claim_ids":ids,"status_counts":dict(status_counts),"evidence_grade_counts":dict(grade_counts),"source_counts":dict(source_counts),"issues":issues,"issue_counts":dict(collections.Counter(i["type"] for i in issues)),"recommendations":[]}
    if any(i["type"]=="unverified_claim_used" for i in issues): payload["recommendations"].append("Verify or downgrade language around non-verified claims.")
    if any(i["type"]=="uncited_substantive_sentence" for i in issues): payload["recommendations"].append("Attach claim IDs or mark unsupported sentences as interpretation/hypothesis.")
    if any(i["type"]=="single_source_dependency" for i in issues): payload["recommendations"].append("Increase source diversity or explicitly justify why one source carries the section.")
    conn.close()
    if args.json: print_json(payload)
    else:
        print(f"Red-team draft: {args.path}")
        print(f"Claims: {len(known)} known, {len(unknown)} unknown | Sources: {dict(source_counts)} | Statuses: {dict(status_counts)} | Grades: {dict(grade_counts)}")
        print(f"Issues: {len(issues)}")
        for i in issues[:args.limit]: print(f"- [{i['severity']}] {i['type']}: {i.get('claim_id') or i.get('sentence_no') or ''} {short(i.get('detail'),220)}")
        if payload["recommendations"]:
            print("Recommendations:"); [print(f"- {r}") for r in payload["recommendations"]]


# ---------- source-location signals / synthesis cards / review UI ----------

def insert_location_signal(cur: sqlite3.Cursor, *, source_id: str, char_start: int|None, char_end: int|None,
                           signal_type: str, polarity: str, strength: float,
                           originating_context_id: str="", originating_suggestion_id: str="",
                           citing_source_id: str="", citation_function: str="", note: str="") -> str:
    key="|".join(map(str,[source_id,char_start,char_end,signal_type,polarity,originating_context_id,originating_suggestion_id,citing_source_id,citation_function,note]))
    signal_id=f"SIG-{sha1_short(key)[:14]}"
    cur.execute("""INSERT OR REPLACE INTO source_location_signals
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        signal_id, source_id, char_start, char_end, signal_type, polarity, float(strength), originating_context_id, originating_suggestion_id, citing_source_id, citation_function, note, now()
    ))
    return signal_id


def rebuild_source_location_signals(source_id: str|None=None) -> dict[str, Any]:
    init_db(True)
    conn=db(); cur=conn.cursor()
    if source_id:
        cur.execute("DELETE FROM source_location_signals WHERE source_id=?", (source_id,))
    else:
        cur.execute("DELETE FROM source_location_signals")
    where="WHERE cls.char_start IS NOT NULL AND cls.char_end IS NOT NULL"
    params=[]
    if source_id:
        where += " AND cls.source_id=?"; params.append(source_id)
    rows=[dict(r) for r in cur.execute(f"""
        SELECT cls.*, cc.citing_source_id, cc.citation_function
        FROM citation_location_suggestions cls
        LEFT JOIN citation_contexts cc ON cc.context_id=cls.context_id
        {where}
    """, params).fetchall()]
    n=0
    for r in rows:
        st=r.get("status") or "candidate_location"
        if st == "accepted": pol="positive"; strength=1.0
        elif st == "weak_match": pol="weak"; strength=0.25
        elif st == "rejected": pol="negative"; strength=-0.7
        else: pol="candidate"; strength=0.15
        insert_location_signal(cur, source_id=r["source_id"], char_start=r.get("char_start"), char_end=r.get("char_end"), signal_type=f"{st}_backtrack", polarity=pol, strength=strength, originating_context_id=r.get("context_id") or "", originating_suggestion_id=r.get("suggestion_id") or "", citing_source_id=r.get("citing_source_id") or "", citation_function=r.get("citation_function") or "", note="from citation_location_suggestions")
        n += 1
    q="SELECT * FROM source_card_suggestions WHERE char_start IS NOT NULL AND char_end IS NOT NULL AND status IN ('accepted','rejected')"
    sc_params=[]
    if source_id:
        q += " AND source_id=?"; sc_params.append(source_id)
    for r in [dict(x) for x in cur.execute(q, sc_params).fetchall()]:
        pol="positive" if r.get("status") == "accepted" else "negative"
        strength=1.1 if pol == "positive" else -0.8
        insert_location_signal(cur, source_id=r["source_id"], char_start=r.get("char_start"), char_end=r.get("char_end"), signal_type=f"source_card_{r.get('status')}", polarity=pol, strength=strength, originating_suggestion_id=r.get("suggestion_id") or "", note=f"source-card suggestion {r.get('status')}")
        n += 1
    conn.commit(); conn.close(); return {"source_id": source_id or "all", "signals_written": n}


def cmd_refresh_location_signals(args):
    result=rebuild_source_location_signals(args.source_id)
    if args.json: print_json(result)
    else: print(f"Refreshed source-location signals for {result['source_id']}: {result['signals_written']}")


def signals_for_range(source_id: str, start: int, end: int, min_overlap: float=0.35) -> list[dict[str, Any]]:
    init_db(True); conn=db()
    rows=[dict(r) for r in conn.execute("""
        SELECT * FROM source_location_signals
        WHERE source_id=? AND char_start IS NOT NULL AND char_end IS NOT NULL
          AND NOT (char_end < ? OR char_start > ?)
    """, (source_id, start, end)).fetchall()]
    conn.close(); out=[]
    for r in rows:
        try:
            if overlap_ratio(start,end,int(r['char_start']),int(r['char_end'])) >= min_overlap:
                out.append(r)
        except Exception:
            pass
    return out


def location_signal_score(source_id: str, start: int|None, end: int|None) -> float:
    if start is None or end is None: return 0.0
    try: start_i=int(start); end_i=int(end)
    except Exception: return 0.0
    return round(sum(float(s.get('strength') or 0) for s in signals_for_range(source_id,start_i,end_i)),4)


def cmd_location_signals(args):
    init_db(True); conn=db(); params=[]; where=[]
    if args.source_id: where.append("source_id=?"); params.append(args.source_id)
    if args.signal_type: where.append("signal_type=?"); params.append(args.signal_type)
    if args.polarity: where.append("polarity=?"); params.append(args.polarity)
    sql="SELECT * FROM source_location_signals" + (" WHERE "+" AND ".join(where) if where else "") + " ORDER BY ABS(strength) DESC, created_at DESC LIMIT ?"
    rows=[dict(r) for r in conn.execute(sql, (*params,args.limit)).fetchall()]
    conn.close()
    if args.json: print_json(rows)
    else:
        print(f"Location signals: {len(rows)}")
        for r in rows:
            print(f"- {r['signal_id']} {r['polarity']} {r['strength']} {r['signal_type']} {source_range_handle(r['source_id'], r['char_start'], r['char_end'])} ctx:{r.get('originating_context_id') or '-'}")


def cmd_create_synthesis(args):
    init_db(True)
    claim_ids=[]
    for x in args.claims: claim_ids.extend(split_list(x))
    if not claim_ids: raise SystemExit("Provide at least one claim id with --claims")
    conn=db(); cur=conn.cursor()
    for cid in claim_ids:
        if not cur.execute("SELECT 1 FROM source_cards WHERE claim_id=?", (cid,)).fetchone(): raise SystemExit(f"Unknown claim_id: {cid}")
    sid=args.synthesis_id or f"SYN-{sha1_short(args.title + args.text + ''.join(claim_ids))[:12]}"
    cur.execute("INSERT OR REPLACE INTO synthesis_cards VALUES (?,?,?,?,?,?,?,?)", (sid,args.title or sid,args.text,args.status,args.topic or "",args.scope_note or "",now(),now()))
    for cid in claim_ids: cur.execute("INSERT OR REPLACE INTO synthesis_claims VALUES (?,?,?,?)", (sid,cid,args.role or "supports",args.note or ""))
    conn.commit(); conn.close(); print(f"{sid}: {len(claim_ids)} claims")


def synthesis_payload(synthesis_id: str) -> dict[str, Any]:
    conn=db(); row=conn.execute("SELECT * FROM synthesis_cards WHERE synthesis_id=?", (synthesis_id,)).fetchone()
    if not row: raise SystemExit(f"Unknown synthesis_id: {synthesis_id}")
    links=[dict(r) for r in conn.execute("SELECT * FROM synthesis_claims WHERE synthesis_id=? ORDER BY claim_id", (synthesis_id,)).fetchall()]
    cards=[]
    for l in links:
        r=conn.execute("SELECT * FROM source_cards WHERE claim_id=?", (l["claim_id"],)).fetchone()
        if r: cards.append({**claim_card(r,"standard"), "synthesis_role": l.get("role"), "synthesis_note": l.get("note")})
    conn.close(); return {"synthesis":dict(row),"claims":cards}


def cmd_synthesis_cards(args):
    init_db(True); conn=db(); params=[]; where=[]
    if args.topic: where.append("topic LIKE ?"); params.append(f"%{args.topic}%")
    if args.status: where.append("status=?"); params.append(args.status)
    rows=[dict(r) for r in conn.execute("SELECT * FROM synthesis_cards" + (" WHERE "+" AND ".join(where) if where else "") + " ORDER BY updated_at DESC LIMIT ?", (*params,args.limit)).fetchall()]
    conn.close()
    if args.json: print_json({"count":len(rows),"items":rows})
    else:
        print(f"Synthesis cards: {len(rows)}")
        for r in rows: print(f"- {r['synthesis_id']} [{r['status']}] {r.get('topic') or '-'} {r['title']}: {short(r['synthesis'],180)}")


def cmd_synthesis_brief(args):
    payload=synthesis_payload(args.synthesis_id)
    if args.json: print_json(payload); return
    syn=payload["synthesis"]
    print(f"# Synthesis: {syn['title']}")
    print(f"Status: {syn['status']} | Topic: {syn.get('topic') or '-'}")
    if syn.get("scope_note"): print(f"Scope: {syn['scope_note']}")
    print("\n## Synthesis")
    print(syn["synthesis"])
    print("\n## Supporting source cards")
    for c in payload["claims"]:
        print(f"- **{c['claim_id']}** grade:{c.get('evidence_grade')} {c['citation_hint']} p.{c.get('page') or '?'} · {c['status']} · {c.get('card_role')} · {c.get('claim_type')}")
        print(f"  - Claim: {c.get('claim')}")
        if c.get("evidence"): print(f"  - Evidence: {short(c.get('evidence'),260)}")


def cmd_export_review_ui(args):
    init_db(True); out_dir=Path(args.out); out_dir.mkdir(parents=True, exist_ok=True); data_dir=out_dir / "data"; data_dir.mkdir(exist_ok=True)
    conn=db(); usage=brief_usage_counts(); loc_usage=claim_usage_from_citation_locations(); queue=[]
    for r in conn.execute("SELECT * FROM source_cards WHERE verification_status!='rejected' ORDER BY updated_at DESC LIMIT ?", (args.limit,)).fetchall():
        card=claim_card(r,"standard"); cid=card["claim_id"]
        rel_count=conn.execute("SELECT COUNT(*) FROM claim_relations WHERE claim_a=? OR claim_b=?", (cid,cid)).fetchone()[0]
        queue.append({**card,"centrality_score":centrality_score(card,loc_usage.get(cid,{}),usage.get(cid,0),rel_count),"brief_count":usage.get(cid,0),"relation_count":rel_count})
    scs=[dict(r) for r in conn.execute("SELECT * FROM source_card_suggestions ORDER BY score DESC LIMIT ?", (args.limit,)).fetchall()]
    cls=[dict(r) for r in conn.execute("SELECT * FROM citation_location_suggestions ORDER BY score DESC LIMIT ?", (args.limit,)).fetchall()]
    synth=[dict(r) for r in conn.execute("SELECT * FROM synthesis_cards ORDER BY updated_at DESC LIMIT ?", (args.limit,)).fetchall()]
    stats={}
    for t in ["sources","source_cards","source_card_suggestions","citation_contexts","citation_location_suggestions","synthesis_cards","review_events"]:
        try: stats[t]=conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception: stats[t]=0
    conn.close()
    payload={"generated_at":now(),"stats":stats,"review_queue":sorted(queue,key=lambda x:x.get("centrality_score",0),reverse=True)[:args.limit],"source_card_suggestions":scs,"citation_location_suggestions":cls,"synthesis_cards":synth}
    (data_dir / "review_export.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    html=REVIEW_UI_HTML
    (out_dir / "review.html").write_text(html, encoding="utf-8")
    print(f"Wrote {out_dir/'review.html'} and {data_dir/'review_export.json'}")


# ---------- embeddings / parser tournament / deeper correctness ----------

EMBED_DIM_DEFAULT = 2048


def embedding_tokens(text: str) -> list[str]:
    terms=list(citation_terms(text))
    out=terms[:]
    for n in (2,3):
        for i in range(0, max(0, len(terms)-n+1)):
            out.append("_".join(terms[i:i+n]))
    return out


def sparse_embed(text: str, dim: int=EMBED_DIM_DEFAULT) -> dict[str, float]:
    vec={}
    for t in embedding_tokens(text):
        idx=int(hashlib.sha1(t.encode('utf-8')).hexdigest()[:8],16)%dim
        vec[str(idx)]=vec.get(str(idx),0.0)+DOMAIN_IMPORTANCE.get(t,1.0)
    n=sum(v*v for v in vec.values())**0.5
    return {k:round(v/n,6) for k,v in vec.items()} if n else {}


def sparse_cosine(a: dict[str,float], b: dict[str,float]) -> float:
    if len(a)>len(b): a,b=b,a
    return sum(v*b.get(k,0.0) for k,v in a.items())


def source_card_embedding_text(row: sqlite3.Row|dict[str,Any]) -> str:
    d=dict(row)
    return "\n".join([d.get('claim') or '', d.get('evidence') or '', d.get('scope_note') or '', ' '.join(tags_for_claim(d.get('claim_id')))])


def cmd_build_embeddings(args):
    init_db(True); conn=db(); cur=conn.cursor(); written=0
    levels=split_list(args.level) or ['source_cards']
    if 'source_cards' in levels:
        rows=cur.execute("SELECT * FROM source_cards" + (" WHERE source_id=?" if args.source_id else ""), ((args.source_id,) if args.source_id else ())).fetchall()
        for r in rows:
            txt=source_card_embedding_text(r); vec=sparse_embed(txt,args.dim)
            cur.execute("INSERT OR REPLACE INTO local_embeddings VALUES (?,?,?,?,?,?,?)", (f"claim:{r['claim_id']}",'source_card',r['source_id'],sha1_short(txt),args.dim,json.dumps(vec,separators=(',',':')),now()))
            written+=1
    conn.commit(); conn.close()
    print_json({'written':written,'levels':levels,'dim':args.dim}) if args.json else print(f"Wrote {written} local embeddings")


def cmd_hybrid_retrieve(args):
    lex=retrieve_claims(args.query, max(args.limit*4,30), 'standard', {'source_id':args.source_id,'verified_only':args.verified_only,'statuses':split_list(args.status),'claim_types':split_list(args.claim_type),'card_roles':split_list(args.card_role)})
    qv=sparse_embed(args.query,args.dim)
    conn=db(); emb=conn.execute("SELECT * FROM local_embeddings WHERE item_type='source_card'" + (" AND source_id=?" if args.source_id else ""), ((args.source_id,) if args.source_id else ())).fetchall(); conn.close()
    sem={}
    for r in emb:
        cid=str(r['item_id']).split(':',1)[1]
        sem[cid]=sparse_cosine(qv,json.loads(r['vector_json'] or '{}'))
    by={c['claim_id']:c for c in lex}
    if args.semantic_only or args.include_semantic_only:
        conn=db(); rows=conn.execute("SELECT * FROM source_cards WHERE verification_status!='rejected'" + (" AND source_id=?" if args.source_id else ""), ((args.source_id,) if args.source_id else ())).fetchall(); conn.close()
        for r in rows:
            cid=r['claim_id']
            if sem.get(cid,0)>0 and cid not in by: by[cid]=claim_card(r,'standard')
    out=[]
    for cid,c in by.items():
        hybrid=(1-args.semantic_weight)*float(c.get('score') or 0)+args.semantic_weight*sem.get(cid,0)*10
        out.append({**c,'semantic_score':round(sem.get(cid,0),4),'hybrid_score':round(hybrid,4)})
    out=sorted(out,key=lambda x:x['hybrid_score'],reverse=True)[:args.limit]
    if args.json: print_json({'query':args.query,'count':len(out),'results':out})
    else:
        print(f"# hybrid retrieve: {args.query}\n")
        for r in out:
            print(f"- **{r['claim_id']}** hybrid:{r['hybrid_score']} sem:{r['semantic_score']} lex:{r.get('score')} {r['claim']}")


def cmd_parser_tournament(args):
    import subprocess
    pdf=Path(args.pdf); modes=split_list(args.modes) or ['pdftotext','pymupdf4llm','auto','reorder']; results=[]
    for mode in modes:
        t0=datetime.now(timezone.utc)
        try:
            cp=subprocess.run([sys.executable,'-m','pdf_pipeline.text_extract',str(pdf),'--mode',mode],capture_output=True,text=True,timeout=args.timeout)
            sec=(datetime.now(timezone.utc)-t0).total_seconds(); txt=cp.stdout or ''
            results.append({'mode':mode,'ok':cp.returncode==0,'seconds':round(sec,2),'chars':len(txt),'words':len(re.findall(r'\w+',txt)),'citation_like':len(re.findall(r'\b[A-Z][A-Za-z-]+\s+et\s+al\.?,?\s+\d{4}|\([A-Z][^)]*\d{4}[^)]*\)',txt)),'stderr':short(cp.stderr,240)})
        except Exception as e: results.append({'mode':mode,'ok':False,'error':str(e)})
    payload={'pdf':str(pdf),'results':results,'best_by_words':max(results,key=lambda x:x.get('words',0)) if results else None}
    if args.out:
        od=Path(args.out); od.mkdir(parents=True,exist_ok=True); (od/'parser_tournament.json').write_text(json.dumps(payload,indent=2),encoding='utf-8')
        (od/'parser_tournament.html').write_text('<html><body><pre>'+json.dumps(payload,indent=2)+'</pre></body></html>',encoding='utf-8')
    if args.json: print_json(payload)
    else:
        for r in results: print(f"{r.get('mode')}: ok={r.get('ok')} words={r.get('words')} cites={r.get('citation_like')} seconds={r.get('seconds')} {r.get('error','')}")


def assess_citation_support_deep(context_text: str, matched_text: str, score: float=0.0, citation_function: str='') -> dict[str, Any]:
    c=norm(context_text); m=norm(matched_text); flags=[]
    cov, matched, missing=weighted_term_coverage(context_text, matched_text)
    verdict='likely_supported' if score>=0.62 and cov>=0.35 else 'possibly_supported' if score>=0.35 and cov>=0.2 else 'weak_match' if score>0 or cov>0.08 else 'no_match'
    if any(k in c for k in ['cause','causes','drives','determines','leads to']) and any(k in m for k in ['associated','correlated','related','may','might']): flags.append('possibly_overstated_causal_language'); verdict='possibly_overstated'
    if (any(k in c for k in ['positive','increase','higher','boost']) and any(k in m for k in ['negative','decrease','lower','no effect','mixed'])) or (any(k in c for k in ['negative','decrease','lower']) and any(k in m for k in ['positive','increase','higher'])): flags.append('possible_direction_mismatch'); verdict='needs_review'
    if 'method' in norm(citation_function) and not re.search(r"method|model|data|approach|framework|review", m): flags.append('possible_method_mismatch')
    return {'verdict':verdict,'flags':flags,'term_coverage':round(cov,4),'matched_terms':matched[:12],'missing_terms':missing[:12]}


# ---------- interactive review UI / synthesis red-team / recursive orchestration ----------

def git_commit_short() -> str:
    try:
        return subprocess.check_output(['git','rev-parse','--short','HEAD'], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return ''


def review_graph_data(limit: int=240) -> dict[str, Any]:
    """Small graph payload for the review cockpit/static review export."""
    init_db(True)
    conn=db()
    nodes={}; edges=[]
    def add_node(node_id, label, kind, **extra):
        if not node_id: return
        if node_id not in nodes:
            nodes[node_id]={"id":node_id,"label":label,"kind":kind,**extra}
    def add_edge(a,b,kind,weight=1,**extra):
        if not a or not b: return
        edges.append({"source":a,"target":b,"kind":kind,"weight":weight,**extra})
    # Sources and top source cards.
    for r in conn.execute("SELECT source_id,title,year FROM sources ORDER BY source_id").fetchall():
        add_node(f"source:{r['source_id']}", r['source_id'], "source", title=r['title'], year=r['year'])
    cards=conn.execute("SELECT * FROM source_cards WHERE verification_status!='rejected' ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    for r in cards:
        card=claim_card(r,"minimal"); cid=card['claim_id']; sid=card['source_id']
        add_node(f"claim:{cid}", cid, "claim", status=card.get('status'), grade=card.get('evidence_grade'), claim=short(card.get('claim'),160), source_id=sid)
        add_edge(f"source:{sid}", f"claim:{cid}", "has_card", 1)
    # Source citation edges.
    for r in conn.execute("SELECT citing_source_id, matched_source_id, COUNT(*) n FROM citation_contexts WHERE matched_source_id IS NOT NULL AND matched_source_id!='' GROUP BY citing_source_id, matched_source_id ORDER BY n DESC LIMIT ?", (limit,)).fetchall():
        add_node(f"source:{r['citing_source_id']}", r['citing_source_id'], "source")
        add_node(f"source:{r['matched_source_id']}", r['matched_source_id'], "source")
        add_edge(f"source:{r['citing_source_id']}", f"source:{r['matched_source_id']}", "cites", r['n'])
    # Claim relations.
    for r in conn.execute("SELECT * FROM claim_relations ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall():
        add_node(f"claim:{r['claim_a']}", r['claim_a'], "claim")
        add_node(f"claim:{r['claim_b']}", r['claim_b'], "claim")
        add_edge(f"claim:{r['claim_a']}", f"claim:{r['claim_b']}", r['relation_type'], 2, status=r['status'])
    # Citation location suggestions to ranges.
    for r in conn.execute("SELECT * FROM citation_location_suggestions WHERE source_id IS NOT NULL ORDER BY score DESC LIMIT ?", (limit,)).fetchall():
        range_id=f"range:{r['source_id']}:{r['char_start']}-{r['char_end']}"
        label=f"{r['source_id']}:{r['char_start']}-{r['char_end']}"
        add_node(range_id,label,"range",source_id=r['source_id'],score=r['score'],status=r['status'],text=short(r.get('matched_text'),140))
        add_edge(f"citation:{r['context_id']}", range_id, "suggests_location", float(r['score'] or 0))
        add_node(f"citation:{r['context_id']}", r['context_id'], "citation")
    conn.close()
    return {"nodes":list(nodes.values()),"edges":edges[:limit*3]}


def review_state(limit: int=120) -> dict[str, Any]:
    init_db(True)
    conn=db(); usage=brief_usage_counts(); loc_usage=claim_usage_from_citation_locations(); queue=[]
    for r in conn.execute("SELECT * FROM source_cards WHERE verification_status!='rejected' ORDER BY updated_at DESC LIMIT ?", (limit*3,)).fetchall():
        card=claim_card(r,"standard"); cid=card["claim_id"]
        rel_count=conn.execute("SELECT COUNT(*) FROM claim_relations WHERE claim_a=? OR claim_b=?", (cid,cid)).fetchone()[0]
        sig=location_signal_score(card.get("source_id"), r["char_start"], r["char_end"])
        queue.append({**card,"centrality_score":centrality_score(card,loc_usage.get(cid,{}),usage.get(cid,0),rel_count)+sig,"brief_count":usage.get(cid,0),"relation_count":rel_count,"location_signal_score":sig})
    scs=[dict(r) for r in conn.execute("SELECT * FROM source_card_suggestions ORDER BY score DESC LIMIT ?", (limit,)).fetchall()]
    cls=[dict(r) for r in conn.execute("SELECT * FROM citation_location_suggestions ORDER BY score DESC LIMIT ?", (limit,)).fetchall()]
    synth=[dict(r) for r in conn.execute("SELECT * FROM synthesis_cards ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()]
    stats={}
    for t in ["sources","source_cards","source_card_suggestions","citation_contexts","citation_location_suggestions","synthesis_cards","review_events","source_location_signals"]:
        try: stats[t]=conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception: stats[t]=0
    stats["git_commit"] = git_commit_short()
    stats["ui_version"] = "review-graph-3d-v2"
    conn.close()
    return {"generated_at":now(),"stats":stats,"review_queue":sorted(queue,key=lambda x:x.get("centrality_score",0),reverse=True)[:limit],"source_card_suggestions":scs,"citation_location_suggestions":cls,"synthesis_cards":synth,"graph":review_graph_data(limit)}


REVIEW_UI_HTML = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Review Cockpit</title><style>
:root{--bg:#0f1115;--panel:#181b22;--panel2:#20242d;--line:#333946;--text:#eef2ff;--muted:#aab2c5;--good:#6ee7b7;--warn:#f6d365;--bad:#fb7185;--blue:#93c5fd}*{box-sizing:border-box}body{font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;background:var(--bg);color:var(--text)}header{padding:14px 18px;border-bottom:1px solid var(--line);position:sticky;top:0;background:rgba(15,17,21,.94);backdrop-filter:blur(10px);z-index:5;display:flex;align-items:center;gap:14px;justify-content:space-between}h1{font-size:20px;margin:0}h2{font-size:16px;margin:0 0 10px}.muted{color:var(--muted)}.wrap{display:grid;grid-template-columns:320px 1fr;gap:14px;padding:14px}.side{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:12px;height:calc(100vh - 78px);overflow:auto;position:sticky;top:78px}.main{display:grid;gap:14px}.card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:14px;box-shadow:0 10px 30px rgba(0,0,0,.18)}.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}.item{border:1px solid transparent;border-top-color:var(--line);padding:12px 0}.item:hover{background:rgba(255,255,255,.025)}.item-title{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.id{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:#dbeafe}.chip{display:inline-flex;border:1px solid #4b5563;border-radius:999px;padding:2px 8px;margin:2px;font-size:12px;color:#dbe4ff}.good{border-color:#167c59;color:var(--good)}.warn{border-color:#8a6d16;color:var(--warn)}.bad{border-color:#8f2745;color:var(--bad)}.blue{border-color:#2563eb;color:var(--blue)}.btn{background:#293244;color:#fff;border:1px solid #4b5563;border-radius:9px;padding:7px 10px;margin:3px;cursor:pointer}.btn:hover{background:#344058}.btn.good{background:#064e3b}.btn.bad{background:#5f1228}.btn.small{font-size:12px;padding:4px 7px}.iconbtn{background:#111827;border:1px solid #3f4654;border-radius:8px;color:#dbeafe;width:28px;height:28px;display:inline-grid;place-items:center;cursor:pointer}.iconbtn.ok{background:#064e3b;border-color:#10b981;color:#d1fae5}.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}.tab{border:1px solid var(--line);border-radius:999px;background:#11151c;color:var(--muted);padding:6px 10px;cursor:pointer}.tab.active{background:#243149;color:#fff;border-color:#4f75bd}input,select,textarea{background:#0b0d12;color:var(--text);border:1px solid #424957;border-radius:9px;padding:8px;width:100%;box-sizing:border-box}.inline-input{width:120px!important;padding:5px!important;font-size:12px}.range-input{width:190px!important;padding:5px!important;font-size:12px}.cmd{font-family:ui-monospace,monospace;background:#090a0d;padding:6px;border-radius:7px;display:inline-block;color:#c7d2fe;max-width:100%;overflow:auto}.text{line-height:1.45}.hidden{display:none}.toast{position:fixed;right:16px;bottom:16px;background:#dbeafe;color:#111827;border-radius:12px;padding:10px 14px;box-shadow:0 8px 30px rgba(0,0,0,.35);z-index:20}.stat{display:flex;justify-content:space-between;border-bottom:1px solid #2d3340;padding:5px 0}.section-head{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:8px}.hint{font-size:12px;color:var(--muted);line-height:1.35}canvas{width:100%;height:680px;background:#0a0c11;border:1px solid var(--line);border-radius:14px}.legend{display:flex;gap:12px;flex-wrap:wrap}.dot{width:10px;height:10px;border-radius:50%;display:inline-block;margin-right:5px}@media(max-width:900px){.wrap{grid-template-columns:1fr}.side{position:relative;top:auto;height:auto}.cols{grid-template-columns:1fr}}</style></head><body><header><div><h1>Academic Database Review Cockpit</h1><div class="muted">Interactive local review + graph triage</div></div><div class="toolbar"><button class="btn" onclick="load()">Refresh</button><button class="btn" onclick="post('/api/refresh-signals',{})">Refresh signals</button><button class="btn" onclick="copyPageSummary()">Copy summary</button></div></header><div class="wrap"><aside class="side"><h2>Control Center</h2><input id="q" placeholder="Filter visible cards…" oninput="render()"><div class="tabs"><button class="tab active" data-tab="graph" onclick="setTab('graph')">Graph</button><button class="tab" data-tab="queue" onclick="setTab('queue')">Review</button><button class="tab" data-tab="scs" onclick="setTab('scs')">Source suggestions</button><button class="tab" data-tab="cls" onclick="setTab('cls')">Citation locations</button><button class="tab" data-tab="syn" onclick="setTab('syn')">Synthesis</button></div><div id="stats"></div><hr style="border-color:#303642"><h2>Quick Actions</h2><p class="hint"><b>central #</b> = review priority/graph centrality. It combines citation usage, chapter-brief use, relation count, evidence grade and review status. Higher = inspect sooner.</p><p class="hint">Graph nodes: sources, claims, citation contexts, source ranges. Drag empty space to rotate, wheel to zoom, Shift-drag to pan, Option-drag a node to move it.</p><p class="hint">Missing page? Use the page box on claim cards. Wrong citation range? Use range <code>START:END</code> on citation-location cards.</p></aside><main class="main"><section class="card" id="graphCard"><div class="section-head"><h2>Evidence Graph</h2><div><button class="btn small" onclick="setGraphMode('2d')">2D</button><button class="btn small" onclick="setGraphMode('3d')">3D</button><button class="btn small" onclick="toggleRotate()">Auto</button><button class="btn small" onclick="state.zoom*=1.15;initGraph(false)">＋</button><button class="btn small" onclick="state.zoom/=1.15;initGraph(false)">−</button><button class="btn small" onclick="resetGraphView()">Reset view</button><button class="btn small" onclick="initGraph(true)">Re-layout</button><button class="btn small" onclick="copyGraph()">Copy graph JSON</button></div></div><div class="legend"><span><i class="dot" style="background:#7dd3fc"></i>source</span><span><i class="dot" style="background:#a78bfa"></i>claim</span><span><i class="dot" style="background:#fcd34d"></i>citation</span><span><i class="dot" style="background:#fb7185"></i>range</span></div><canvas id="graph" width="1400" height="760"></canvas><p id="graphInfo" class="muted"></p></section><section class="card hidden" id="queueCard"><div class="section-head"><h2>High-value review queue</h2><span id="queueCount" class="muted"></span></div><div id="queue"></div></section><section class="card hidden" id="scsCard"><div class="section-head"><h2>Source-card suggestions</h2><span id="scsCount" class="muted"></span></div><div id="scs"></div></section><section class="card hidden" id="clsCard"><div class="section-head"><h2>Citation-location suggestions</h2><span id="clsCount" class="muted"></span></div><div id="cls"></div></section><section class="card hidden" id="synCard"><div class="section-head"><h2>Synthesis cards</h2><span id="synCount" class="muted"></span></div><div id="syn"></div></section></main></div><div id="toast" class="toast hidden"></div><script>
let state={data:null,tab:'graph',graph:null,drag:null,graphMode:'3d',autoRotate:true,rotX:-0.28,rotY:0.75,zoom:1,panX:0,panY:0,hover:null,selected:null,mouseMode:null,lastX:0,lastY:0,downX:0,downY:0}; const $=id=>document.getElementById(id); const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function toast(msg){$('toast').textContent=msg;$('toast').classList.remove('hidden');setTimeout(()=>$('toast').classList.add('hidden'),1400)} async function copyText(txt,btn){try{await navigator.clipboard.writeText(String(txt||'')); if(btn){const old=btn.textContent; btn.textContent='✓'; btn.classList.add('ok'); setTimeout(()=>{btn.textContent=old; btn.classList.remove('ok')},900)} toast('Copied')}catch(e){prompt('Copy:',txt)}} function copyDecoded(txt,btn){copyText(decodeURIComponent(txt||''),btn)}
async function post(url,obj){let r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(obj)}); let j=await r.json(); if(!j.ok) alert(JSON.stringify(j,null,2)); else toast('Updated'); await load();}
function chip(x,cls=''){return x?`<span class="chip ${cls}">${esc(x)}</span>`:''} function statusCls(s){return s==='verified'?'good':s==='rejected'||s==='superseded'?'bad':'warn'} function hay(c){return [c.claim_id,c.suggestion_id,c.synthesis_id,c.status,c.evidence_grade,c.claim,c.text,c.synthesis,c.matched_text,c.source_id,c.citation_hint,c.suggested_card_role,c.suggested_claim_type].join(' ').toLowerCase()} function filter(arr){let q=$('q').value.toLowerCase().trim(); return !q?arr:(arr||[]).filter(c=>hay(c).includes(q))}
function commandFor(c,type){let id=c.claim_id||c.suggestion_id||c.synthesis_id;if(type==='claim')return `python rh2.py review-packet ${id}`; if(type==='scs')return `python rh2.py accept-source-card-suggestion ${id}`; if(type==='cls')return `python rh2.py assess-citation-location ${id}`; if(type==='syn')return `python rh2.py synthesis-brief ${id}`; return id}
function item(c,type){let id=c.claim_id||c.suggestion_id||c.synthesis_id; let body=c.claim||c.text||c.synthesis||c.matched_text||''; let cmd=commandFor(c,type); let idEnc=encodeURIComponent(id), bodyEnc=encodeURIComponent(body), cmdEnc=encodeURIComponent(cmd); return `<article class="item"><div class="item-title"><b class="id">${esc(id)}</b><button class="iconbtn" title="Copy ID" onclick="copyDecoded('${idEnc}',this)">⧉</button><button class="iconbtn" title="Copy text" onclick="copyDecoded('${bodyEnc}',this)">📋</button>${chip(c.status,statusCls(c.status))}${chip(c.evidence_grade,'blue')}${chip(c.suggested_card_role||c.card_role,'blue')}${c.centrality_score?chip('central '+Number(c.centrality_score).toFixed(2),'blue'):''}${c.score?chip('score '+c.score):''}</div><p class="text">${esc(body)}</p><div class="toolbar"><button class="btn small" onclick="copyDecoded('${cmdEnc}',this)">Copy command</button>${type==='claim'?reviewControls(id,c):''}${type==='scs'?sourceSuggestionControls(id):''}${type==='cls'?citationLocationControls(id,c):''}</div><div class="cmd">${esc(cmd)}</div></article>`}
function reviewControls(id,c){return `<select id="st-${id}" style="width:180px"><option>verified</option><option>candidate_needs_review</option><option>needs_page_check</option><option>needs_source_check</option><option>rejected</option><option>superseded</option></select><input id="nt-${id}" placeholder="review note" style="width:220px"><button class="btn small good" onclick="post('/api/review',{claim_id:'${id}',status:$('st-${id}').value,note:$('nt-${id}').value,label:['good_claim']})">Review</button><input class="inline-input" id="pg-${id}" placeholder="page" value="${esc(c.page||'')}"><button class="btn small" onclick="post('/api/update-claim-page',{claim_id:'${id}',page:$('pg-${id}').value})">Set page</button>`}
function sourceSuggestionControls(id){return `<button class="btn small good" onclick="post('/api/accept-source-card',{suggestion_id:'${id}'})">Accept</button><button class="btn small bad" onclick="post('/api/reject-source-card',{suggestion_id:'${id}',label:'too_broad'})">Reject</button>`} function citationLocationControls(id,c){let val=(c.char_start!=null&&c.char_end!=null)?`${c.char_start}:${c.char_end}`:'';return `<button class="btn small good" onclick="post('/api/verify-location',{suggestion_id:'${id}',status:'accepted'})">Accept loc</button><button class="btn small bad" onclick="post('/api/verify-location',{suggestion_id:'${id}',status:'rejected'})">Reject loc</button><input class="range-input" id="rg-${id}" placeholder="START:END" value="${esc(val)}"><button class="btn small" onclick="post('/api/move-citation-location',{suggestion_id:'${id}',range:$('rg-${id}').value})">Move range</button>`}
function setTab(t){state.tab=t; document.querySelectorAll('.tab').forEach(b=>b.classList.toggle('active',b.dataset.tab===t)); ['graph','queue','scs','cls','syn'].forEach(x=>$(x+'Card').classList.toggle('hidden',x!==t)); if(t==='graph') setTimeout(()=>initGraph(false),50); render()}
function render(){let d=state.data;if(!d)return; $('stats').innerHTML='<h2>Stats</h2>'+Object.entries(d.stats).map(([k,v])=>`<div class="stat"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join(''); let q=filter(d.review_queue), scs=filter(d.source_card_suggestions), cls=filter(d.citation_location_suggestions), syn=filter(d.synthesis_cards); $('queueCount').textContent=q.length; $('scsCount').textContent=scs.length; $('clsCount').textContent=cls.length; $('synCount').textContent=syn.length; $('queue').innerHTML=q.map(x=>item(x,'claim')).join('')||'<p class="muted">No items.</p>'; $('scs').innerHTML=scs.map(x=>item(x,'scs')).join('')||'<p class="muted">No suggestions.</p>'; $('cls').innerHTML=cls.map(x=>item(x,'cls')).join('')||'<p class="muted">No citation locations.</p>'; $('syn').innerHTML=syn.map(x=>item(x,'syn')).join('')||'<p class="muted">No synthesis cards.</p>'; if(state.tab==='graph') initGraph(false)}
async function load(){try{state.data=await (await fetch('/api/state?limit=180')).json()}catch(e){state.data=await (await fetch('data/review_export.json')).json()} render()} function copyPageSummary(){let d=state.data;if(!d)return; copyText(`Academic Database Review Summary\n${JSON.stringify(d.stats,null,2)}`)} function copyGraph(){copyText(JSON.stringify(state.data?.graph||{},null,2))}
function setGraphMode(m){state.graphMode=m;state.graph=null;initGraph(true);toast('Graph mode: '+m.toUpperCase())}
function toggleRotate(){state.autoRotate=!state.autoRotate;toast('Auto-rotate '+(state.autoRotate?'on':'off'))}
function resetGraphView(){state.rotX=-0.28;state.rotY=0.75;state.zoom=1;state.panX=0;state.panY=0;state.autoRotate=false;initGraph(false);toast('Graph view reset')}
function initGraph(force){
  let g=state.data?.graph;if(!g)return; const canvas=$('graph'); if(!canvas)return; const ctx=canvas.getContext('2d'); const W=canvas.width,H=canvas.height; const colors={source:'#7dd3fc',claim:'#a78bfa',citation:'#fcd34d',range:'#fb7185'};
  if(force||!state.graph){let nodes=(g.nodes||[]).slice(0,520).map((n,i)=>{let a=i*2.399963,r=150+310*Math.random();return {...n,x:Math.cos(a)*r,y:(Math.random()-.5)*460,z:Math.sin(a)*r,vx:0,vy:0,vz:0}}); state.graph={nodes,edges:(g.edges||[]).slice(0,1100),settled:false};}
  let nodes=state.graph.nodes, edges=state.graph.edges, map=Object.fromEntries(nodes.map(n=>[n.id,n])); let steps=force||!state.graph.settled?90:2;
  for(let step=0;step<steps;step++){
    for(const n of nodes){n.vx+=(-n.x)*0.0007;n.vy+=(-n.y)*0.0007;n.vz+=(-n.z)*0.0007}
    for(let i=0;i<nodes.length;i++)for(let j=i+1;j<nodes.length;j++){let a=nodes[i],b=nodes[j],dx=a.x-b.x,dy=a.y-b.y,dz=a.z-b.z,d2=dx*dx+dy*dy+dz*dz+140,f=300/d2;a.vx+=dx*f;b.vx-=dx*f;a.vy+=dy*f;b.vy-=dy*f;a.vz+=dz*f;b.vz-=dz*f}
    for(const e of edges){let a=map[e.source],b=map[e.target]; if(!a||!b)continue; let dx=b.x-a.x,dy=b.y-a.y,dz=b.z-a.z,d=Math.sqrt(dx*dx+dy*dy+dz*dz)||1,target=145+(e.weight||1)*9,f=(d-target)*0.006;a.vx+=dx/d*f;b.vx-=dx/d*f;a.vy+=dy/d*f;b.vy-=dy/d*f;a.vz+=dz/d*f;b.vz-=dz/d*f}
    for(const n of nodes){n.vx*=.86;n.vy*=.86;n.vz*=.86;n.x+=n.vx;n.y+=n.vy;n.z+=n.vz}
  }
  state.graph.settled=true; if(state.graphMode==='3d'&&state.autoRotate)state.rotY+=0.01;
  function rotate(n){let x=n.x,y=n.y,z=n.z; let cy=Math.cos(state.rotY),sy=Math.sin(state.rotY),cx=Math.cos(state.rotX),sx=Math.sin(state.rotX); let x1=x*cy-z*sy,z1=x*sy+z*cy; let y1=y*cx-z1*sx,z2=y*sx+z1*cx; return {x:x1,y:y1,z:z2}}
  function project(n){if(state.graphMode==='2d')return {x:W/2+state.panX+n.x*state.zoom,y:H/2+state.panY+n.y*state.zoom,scale:state.zoom,depth:0}; let r=rotate(n),fov=680,scale=(fov/(fov+r.z))*state.zoom; return {x:W/2+state.panX+r.x*scale,y:H/2+state.panY+r.y*scale,scale,depth:r.z}}
  for(const n of nodes){let p=project(n);n.sx=p.x;n.sy=p.y;n.depth=p.depth;n.scale=p.scale}
  ctx.clearRect(0,0,W,H); let grad=ctx.createRadialGradient(W/2,H/2,20,W/2,H/2,Math.max(W,H)/1.45);grad.addColorStop(0,'rgba(38,54,88,.45)');grad.addColorStop(.55,'rgba(12,17,29,.92)');grad.addColorStop(1,'rgba(5,7,12,1)');ctx.fillStyle=grad;ctx.fillRect(0,0,W,H);
  ctx.save(); ctx.globalCompositeOperation='screen'; for(let i=0;i<80;i++){let x=(Math.sin(i*12.989+state.rotY)*43758.5453%1)*W,y=(Math.sin(i*78.23)*23454.13%1)*H;ctx.fillStyle='rgba(147,197,253,.08)';ctx.fillRect(Math.abs(x),Math.abs(y),1,1)} ctx.restore();
  let sortedEdges=edges.slice().sort((ea,eb)=>((map[ea.source]?.depth||0)+(map[ea.target]?.depth||0))-((map[eb.source]?.depth||0)+(map[eb.target]?.depth||0)));
  for(const e of sortedEdges){let a=map[e.source],b=map[e.target]; if(!a||!b)continue;let alpha=state.graphMode==='3d'?Math.max(.06,Math.min(.46,(a.scale+b.scale)/5)):.24;ctx.strokeStyle=`rgba(180,195,230,${alpha})`;ctx.lineWidth=Math.min(4,.45+(e.weight||1)*.25*((a.scale+b.scale)/2));ctx.beginPath();ctx.moveTo(a.sx,a.sy);ctx.lineTo(b.sx,b.sy);ctx.stroke()}
  let sorted=nodes.slice().sort((a,b)=>a.depth-b.depth); state.hover=null;
  for(const n of sorted){let base=n.kind==='source'?12:n.kind==='claim'?5:n.kind==='citation'?6:8,r=base*(state.graphMode==='3d'?Math.max(.42,Math.min(2.0,n.scale)):state.zoom);n.r=r;ctx.fillStyle=colors[n.kind]||'#94a3b8';ctx.globalAlpha=state.graphMode==='3d'?Math.max(.38,Math.min(1,n.scale)):.95;ctx.beginPath();ctx.arc(n.sx,n.sy,r,0,Math.PI*2);ctx.fill();ctx.globalAlpha=1;ctx.strokeStyle='rgba(255,255,255,.18)';ctx.stroke(); if(['source','range'].includes(n.kind)||r>9){ctx.fillStyle='#dbeafe';ctx.font=`${Math.max(10,12*(state.graphMode==='3d'?Math.min(1.4,n.scale):1))}px system-ui`;ctx.fillText(String(n.label||n.id).slice(0,34),n.sx+r+4,n.sy+4)}}
  if(state.hover){ctx.fillStyle='rgba(15,17,21,.92)';ctx.strokeStyle='rgba(147,197,253,.6)';let label=String(state.hover.label||state.hover.id);ctx.font='13px system-ui';let w=Math.min(520,ctx.measureText(label).width+24);ctx.fillRect(16,H-52,w,34);ctx.strokeRect(16,H-52,w,34);ctx.fillStyle='#dbeafe';ctx.fillText(label,28,H-31)}
  let selectedHtml=''; if(state.selected){let sid=encodeURIComponent(state.selected.id); selectedHtml=`<br><b>Selected:</b> ${esc(state.selected.label||state.selected.id)} <button class='btn small' onclick="copyDecoded('${sid}',this)">Copy node ID</button>`} $('graphInfo').innerHTML=`${nodes.length} nodes · ${edges.length} edges · ${state.graphMode.toUpperCase()} · drag rotate · wheel zoom · shift-drag pan ${state.autoRotate?'· auto-rotating':''}`+selectedHtml;
  function pointer(ev){let rect=canvas.getBoundingClientRect();return {x:(ev.clientX-rect.left)*W/rect.width,y:(ev.clientY-rect.top)*H/rect.height}}
  canvas.onmousedown=ev=>{let p=pointer(ev);state.lastX=p.x;state.lastY=p.y;state.downX=p.x;state.downY=p.y;let hit=nodes.find(n=>(n.sx-p.x)**2+(n.sy-p.y)**2<(Math.max(12,n.r)+7)**2); if(hit&&ev.altKey){state.drag=hit;state.mouseMode='node'} else if(ev.shiftKey||ev.button===2){state.mouseMode='pan'} else {state.mouseMode='rotate';state.autoRotate=false}};
  canvas.onmousemove=ev=>{let p=pointer(ev);let dx=p.x-state.lastX,dy=p.y-state.lastY;let hit=nodes.find(n=>(n.sx-p.x)**2+(n.sy-p.y)**2<(Math.max(12,n.r)+7)**2);state.hover=hit||null;if(state.mouseMode==='rotate'){state.rotY+=dx*.006;state.rotX+=dy*.006;state.rotX=Math.max(-1.45,Math.min(1.45,state.rotX));initGraph(false)}else if(state.mouseMode==='pan'){state.panX+=dx;state.panY+=dy;initGraph(false)}else if(state.mouseMode==='node'&&state.drag){state.drag.x+=dx/state.zoom;state.drag.y+=dy/state.zoom;initGraph(false)}else{initGraph(false)}state.lastX=p.x;state.lastY=p.y};
  canvas.onmouseup=ev=>{let p=pointer(ev);let moved=Math.hypot(p.x-state.downX,p.y-state.downY);if(moved<4){let hit=nodes.find(n=>(n.sx-p.x)**2+(n.sy-p.y)**2<(Math.max(12,n.r)+7)**2);state.selected=hit||null;initGraph(false)}state.drag=null;state.mouseMode=null}; canvas.onmouseleave=()=>{state.drag=null;state.mouseMode=null}; canvas.oncontextmenu=e=>e.preventDefault(); canvas.onwheel=ev=>{ev.preventDefault();state.zoom*=ev.deltaY<0?1.1:.9;state.zoom=Math.max(.25,Math.min(4,state.zoom));initGraph(false)};
}
setInterval(()=>{if(state.tab==='graph'&&state.graphMode==='3d'&&state.autoRotate) initGraph(false)},70);
window.addEventListener('keydown',e=>{let tag=(e.target&&e.target.tagName||'').toLowerCase();let editing=['input','textarea','select'].includes(tag)||e.target?.isContentEditable;if((e.metaKey||e.ctrlKey)&&e.key==='r'){e.preventDefault();load();return} if(editing)return; if(e.key==='0')setTab('graph'); if(e.key==='1')setTab('queue'); if(e.key==='2')setTab('scs'); if(e.key==='3')setTab('cls'); if(e.key==='4')setTab('syn')}); load();
</script></body></html>'''


def cmd_review_ui(args):
    import http.server, socketserver, urllib.parse
    class Handler(http.server.BaseHTTPRequestHandler):
        def _json(self,obj,code=200):
            data=json.dumps(obj,ensure_ascii=False).encode(); self.send_response(code); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
        def _html(self,text):
            data=text.encode(); self.send_response(200); self.send_header('Content-Type','text/html'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
        def _body(self):
            n=int(self.headers.get('Content-Length') or 0); return json.loads(self.rfile.read(n).decode() or '{}') if n else {}
        def do_GET(self):
            if self.path.startswith('/api/state'):
                return self._json(review_state(args.limit))
            return self._html(REVIEW_UI_HTML)
        def do_POST(self):
            try:
                b=self._body(); path=urllib.parse.urlparse(self.path).path
                if path=='/api/review':
                    class O: pass
                    o=O(); o.claim_id=b['claim_id']; o.status=b.get('status','candidate_needs_review'); o.note=b.get('note',''); o.actor='review-ui'; o.label=b.get('labels') or b.get('label') or []
                    if isinstance(o.label,str): o.label=[o.label]
                    cmd_review(o); return self._json({'ok':True})
                if path=='/api/update-claim-page':
                    cid=b.get('claim_id'); page=str(b.get('page') or '').strip()
                    if not cid: return self._json({'error':'claim_id required'},400)
                    conn=db(); row=conn.execute('SELECT claim_id FROM source_cards WHERE claim_id=?',(cid,)).fetchone()
                    if not row: conn.close(); return self._json({'error':'unknown claim_id'},404)
                    conn.execute('UPDATE source_cards SET page=?, page_status=?, updated_at=? WHERE claim_id=?',(page, 'page_set_manually' if page else 'needs_page_check', now(), cid))
                    conn.commit(); conn.close(); return self._json({'ok':True})
                if path=='/api/move-citation-location':
                    sid=b.get('suggestion_id'); raw=str(b.get('range') or '').strip()
                    if not sid or not raw: return self._json({'error':'suggestion_id and range required'},400)
                    try:
                        start,end=parse_char_range(raw)
                    except Exception as e:
                        return self._json({'error':str(e)},400)
                    conn=db(); row=conn.execute('SELECT * FROM citation_location_suggestions WHERE suggestion_id=?',(sid,)).fetchone()
                    if not row: conn.close(); return self._json({'error':'unknown suggestion_id'},404)
                    d=dict(row); source_id=d.get('source_id') or d.get('matched_source_id')
                    try:
                        text=source_slice(source_id,start,end)
                    except Exception:
                        text=''
                    page_start=candidate_page_for_span(source_id,start) or ''
                    page_end=candidate_page_for_span(source_id,end-1) or page_start
                    score_json={}
                    try: score_json=json.loads(d.get('score_json') or '{}')
                    except Exception: score_json={}
                    score_json['moved_manually']=True
                    conn.execute('''UPDATE citation_location_suggestions SET target_type=?, target_id=?, char_start=?, char_end=?, page_start=?, page_end=?, matched_text=?, score_json=?, status=?, updated_at=? WHERE suggestion_id=?''',('source_range','manual_range',start,end,page_start,page_end,text or d.get('matched_text') or '',json.dumps(score_json,ensure_ascii=False),'needs_review',now(),sid))
                    conn.commit(); conn.close(); return self._json({'ok':True})
                if path=='/api/reject-source-card':
                    class O: pass
                    o=O(); o.suggestion_id=b['suggestion_id']; o.label=b.get('label'); cmd_reject_source_card_suggestion(o); return self._json({'ok':True})
                if path=='/api/accept-source-card':
                    class O: pass
                    o=O(); o.suggestion_id=b['suggestion_id']; o.claim_id=None; o.claim=None; o.claim_type=None; o.scope_note=None; o.confidence='medium'; o.status='candidate_needs_review'; o.json=True; cmd_accept_source_card_suggestion(o); return self._json({'ok':True})
                if path=='/api/verify-location':
                    class O: pass
                    o=O(); o.suggestion_id=b['suggestion_id']; o.status=b.get('status','accepted'); o.note=b.get('note',''); cmd_verify_location(o); return self._json({'ok':True})
                if path=='/api/refresh-signals': rebuild_source_location_signals(b.get('source_id')); return self._json({'ok':True})
                return self._json({'error':'unknown endpoint'},404)
            except Exception as e: return self._json({'error':str(e)},500)
        def log_message(self,fmt,*a):
            if args.verbose: super().log_message(fmt,*a)
    with socketserver.TCPServer((args.host,args.port),Handler) as httpd:
        print(f"Review UI running at http://{args.host}:{args.port}"); httpd.serve_forever()


def redteam_synthesis_payload(synthesis_id: str) -> dict[str, Any]:
    payload=synthesis_payload(synthesis_id); syn=payload['synthesis']; cards=payload['claims']; issues=[]
    sources=collections.Counter(c.get('source_id') for c in cards); statuses=collections.Counter(c.get('status') for c in cards); grades=collections.Counter(c.get('evidence_grade') for c in cards)
    if not cards: issues.append({'severity':'high','type':'no_supporting_claims','detail':'Synthesis has no linked source cards.'})
    for c in cards:
        if c.get('status')!='verified': issues.append({'severity':'high','type':'unverified_supporting_claim','claim_id':c.get('claim_id'),'detail':c.get('status')})
        if c.get('evidence_grade') in {'C','D','X'}: issues.append({'severity':'medium','type':'weak_supporting_evidence','claim_id':c.get('claim_id'),'detail':c.get('evidence_grade')})
        if not c.get('page'): issues.append({'severity':'medium','type':'support_missing_page','claim_id':c.get('claim_id')})
    support_text=' '.join([c.get('claim','')+' '+c.get('evidence','') for c in cards])
    cov, matched, missing=weighted_term_coverage(syn.get('synthesis',''), support_text)
    if cov < 0.25 and cards: issues.append({'severity':'medium','type':'low_term_alignment','detail':{'coverage':round(cov,3),'missing':missing[:12]}})
    if len(sources)==1 and len(cards)>=2: issues.append({'severity':'medium','type':'single_source_synthesis','detail':dict(sources)})
    if re.search(r"\b(proves|always|never|causes|determines)\b", syn.get('synthesis',''), re.I): issues.append({'severity':'medium','type':'strong_synthesis_language','detail':'Strong wording should be explicitly supported or softened.'})
    rec=[]
    if any(i['type']=='unverified_supporting_claim' for i in issues): rec.append('Verify supporting source cards before using this synthesis as final prose.')
    if any(i['type']=='low_term_alignment' for i in issues): rec.append('Revise synthesis text or add source cards that support its central terms.')
    return {'synthesis':syn,'support_count':len(cards),'source_counts':dict(sources),'status_counts':dict(statuses),'evidence_grade_counts':dict(grades),'term_alignment':{'coverage':round(cov,4),'matched_terms':matched[:12],'missing_terms':missing[:12]},'issues':issues,'issue_counts':dict(collections.Counter(i['type'] for i in issues)),'recommendations':rec,'supporting_claims':cards}


def cmd_redteam_synthesis(args):
    payload=redteam_synthesis_payload(args.synthesis_id)
    if args.json: print_json(payload); return
    syn=payload['synthesis']; print(f"Red-team synthesis: {syn['synthesis_id']} · {syn['title']}"); print(f"Support: {payload['support_count']} | Sources: {payload['source_counts']} | Statuses: {payload['status_counts']} | Grades: {payload['evidence_grade_counts']}"); print(f"Term alignment: {payload['term_alignment']['coverage']} matched={payload['term_alignment']['matched_terms']}")
    for i in payload['issues'][:args.limit]: print(f"- [{i['severity']}] {i['type']}: {short(i.get('detail'),220)}")
    if payload['recommendations']: print('Recommendations:'); [print(f"- {r}") for r in payload['recommendations']]


def cmd_recursive_run(args):
    def run_step(step: dict[str, Any], name: str, fn, obj=None):
        import contextlib, io
        buf=io.StringIO()
        try:
            if args.json or args.quiet:
                with contextlib.redirect_stdout(buf):
                    fn(obj) if obj is not None else fn()
            else:
                fn(obj) if obj is not None else fn()
            step['actions'].append(name)
            if buf.getvalue(): step.setdefault('logs', {})[name]=buf.getvalue()[-1200:]
        except BaseException as e:
            step.setdefault('errors', []).append({'step': name, 'error': str(e), 'log': buf.getvalue()[-1200:]})
            if not args.quiet and not args.json:
                print(f"[recursive-run] {name} failed: {e}")
    trace=[]; prev=None
    for round_i in range(1,args.rounds+1):
        step={'round':round_i,'actions':[]}
        if args.source_id:
            if args.backfill:
                class O: pass
                o=O(); o.source_id=args.source_id; o.min_score=args.min_match_score; o.force=False; o.dry_run=args.dry_run; o.limit=9999; o.json=False
                run_step(step,'backfill_source_matches',cmd_backfill_source_matches,o)
            if args.suggest_locations:
                class O: pass
                o=O(); o.source_id=args.source_id; o.limit=args.context_limit; o.per_context=args.per_context; o.min_score=args.min_location_score; o.store=not args.dry_run; o.show=5; o.json=False
                run_step(step,'suggest_locations_for_cited_source',cmd_suggest_locations_for_cited_source,o)
            if args.suggest_cards:
                class O: pass
                o=O(); o.source_id=args.source_id; o.limit=args.card_limit; o.min_score=args.min_card_score; o.learned=args.learned; o.learned_weight=args.learned_weight; o.store=not args.dry_run; o.json=False
                run_step(step,'suggest_source_cards',cmd_suggest_source_cards,o)
            if args.promote:
                class O: pass
                o=O(); o.source_id=args.source_id; o.min_strength=args.min_usage_strength; o.limit=args.card_limit; o.store=not args.dry_run; o.json=False
                run_step(step,'promote_cited_locations',cmd_promote_cited_locations,o)
        if args.refresh_signals and not args.dry_run:
            run_step(step,'refresh_location_signals',lambda: rebuild_source_location_signals(args.source_id))
        if args.train and not args.dry_run:
            class O: pass
            o=O(); o.json=False
            run_step(step,'train_rankers',cmd_train_rankers,o)
        stats=review_state(args.review_limit)['stats']; step['stats']=stats; trace.append(step); stable=(stats==prev); prev=stats
        if args.until_stable and stable: break
    payload={'source_id':args.source_id,'rounds_run':len(trace),'trace':trace}
    if args.json: print_json(payload)
    elif not args.quiet: print('recursive-run complete')


# ---------- research memory tools / graph cache / context planner / snapshots ----------

TOOL_SCHEMAS = {
    "retrieve_claims": {"description":"Retrieve source cards/claims", "input":{"query":"str", "limit":"int=8", "source_id":"str?", "fields":"minimal|standard|full"}},
    "hybrid_retrieve": {"description":"Retrieve with lexical + local sparse semantic ranking", "input":{"query":"str", "limit":"int=8"}},
    "get_context": {"description":"Get bounded source context for a claim", "input":{"claim_id":"str", "window":"int=500"}},
    "review_packet": {"description":"Assemble review packet for a claim", "input":{"claim_id":"str"}},
    "source_neighborhood": {"description":"Show citation/card neighborhood for a source", "input":{"source_id":"str"}},
    "reading_priority": {"description":"Rank missing/high-value references", "input":{"query":"str?", "limit":"int=20"}},
    "context_plan": {"description":"Build token-budgeted evidence/context plan", "input":{"query":"str", "budget":"int=3000"}},
    "redteam_draft": {"description":"Audit draft defensibility", "input":{"path":"str"}},
}


def cmd_tool_schema(args):
    print_json({"tools": TOOL_SCHEMAS, "protocol": "jsonl", "example": {"tool":"retrieve_claims", "args":{"query":"trust policy stability", "limit":5}}})


def run_tool_call(tool: str, kwargs: dict[str, Any]) -> Any:
    tool=tool.replace('-','_')
    if tool == "retrieve_claims":
        return {"results": retrieve_claims(kwargs.get("query",""), int(kwargs.get("limit",8)), kwargs.get("fields","standard"), {"source_id": kwargs.get("source_id"), "statuses": split_list(kwargs.get("status")), "claim_types": split_list(kwargs.get("claim_type"))})}
    if tool == "hybrid_retrieve":
        # return compact implementation without invoking stdout command
        q=kwargs.get("query",""); limit=int(kwargs.get("limit",8)); lex=retrieve_claims(q, max(limit*4,30), "standard", {})
        return {"results": lex[:limit], "note":"Use CLI hybrid-retrieve for semantic-only expansion; MCP returns lexical-compatible cards."}
    if tool == "get_context":
        cid=kwargs.get("claim_id")
        if not cid: raise ValueError("claim_id required")
        conn=db(); row=conn.execute("SELECT * FROM source_cards WHERE claim_id=?", (cid,)).fetchone(); conn.close()
        if not row: raise ValueError(f"unknown claim_id {cid}")
        d=dict(row); text=read_source_text(d["source_id"]); window=int(kwargs.get("window",500)); start=max(0,int(d.get("char_start") or 0)-window); end=min(len(text),int(d.get("char_end") or d.get("char_start") or 0)+window)
        return {"claim": claim_card(d,"standard"), "context": text[start:end], "context_start": start, "context_end": end}
    if tool == "review_packet":
        cid=kwargs.get("claim_id")
        if not cid: raise ValueError("claim_id required")
        return review_packet_payload(cid, nearby=int(kwargs.get("nearby",5)), citation_limit=int(kwargs.get("citation_limit",5)))
    if tool == "source_neighborhood":
        sid=kwargs.get("source_id")
        if not sid: raise ValueError("source_id required")
        return source_neighborhood_payload(sid, limit=int(kwargs.get("limit",30)))
    if tool == "reading_priority":
        return reading_priority_items(query=kwargs.get("query",""), source_id=kwargs.get("source_id"), missing_only=bool(kwargs.get("missing_only",False)), limit=int(kwargs.get("limit",20)))
    if tool == "context_plan":
        return build_context_plan(kwargs.get("query",""), budget=int(kwargs.get("budget",3000)), limit=int(kwargs.get("limit",20)))
    raise ValueError(f"unknown tool: {tool}")


def cmd_mcp_server(args):
    # JSON-lines, MCP-like stdio tool loop. Each line: {"tool":"...", "args":{...}, "id":"optional"}
    print_json({"ready": True, "tools": list(TOOL_SCHEMAS)})
    sys.stdout.flush()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req=json.loads(line)
            result=run_tool_call(req.get("tool") or req.get("name"), req.get("args") or req.get("arguments") or {})
            print_json({"id": req.get("id"), "ok": True, "result": result})
        except Exception as e:
            print_json({"ok": False, "error": str(e)})
        sys.stdout.flush()


def rebuild_graph_cache() -> dict[str, Any]:
    init_db(True); conn=db(); cur=conn.cursor(); cur.execute("DELETE FROM graph_edges_cache"); n=0
    def add(a,b,k,w=1.0,meta=None):
        nonlocal n
        eid=f"GEDGE-{sha1_short(a+'|'+b+'|'+k)[:14]}"; cur.execute("INSERT OR REPLACE INTO graph_edges_cache VALUES (?,?,?,?,?,?,?)", (eid,a,b,k,float(w),json.dumps(meta or {},ensure_ascii=False),now())); n+=1
    for r in cur.execute("SELECT source_id FROM sources").fetchall():
        add("corpus", f"source:{r['source_id']}", "has_source", 1)
    for r in cur.execute("SELECT claim_id,source_id,verification_status,claim_type FROM source_cards").fetchall():
        add(f"source:{r['source_id']}", f"claim:{r['claim_id']}", "has_card", 1, {"status":r["verification_status"],"claim_type":r["claim_type"]})
    for r in cur.execute("SELECT * FROM claim_relations").fetchall():
        add(f"claim:{r['claim_a']}", f"claim:{r['claim_b']}", r["relation_type"], 1, {"status":r["status"],"note":r["note"]})
    for r in cur.execute("SELECT citing_source_id, matched_source_id, COUNT(*) n FROM citation_contexts WHERE matched_source_id IS NOT NULL AND matched_source_id!='' GROUP BY citing_source_id, matched_source_id").fetchall():
        add(f"source:{r['citing_source_id']}", f"source:{r['matched_source_id']}", "cites", r["n"])
    for r in cur.execute("SELECT context_id, source_id, char_start, char_end, status, score FROM citation_location_suggestions WHERE source_id IS NOT NULL").fetchall():
        if r["char_start"] is not None and r["char_end"] is not None:
            add(f"citation_context:{r['context_id']}", source_range_handle(r["source_id"], r["char_start"], r["char_end"]), "suggests_location", r["score"] or 0, {"status":r["status"]})
    conn.commit(); conn.close(); return {"edges": n}


def cmd_rebuild_graph_cache(args):
    result=rebuild_graph_cache(); print_json(result) if args.json else print(f"Rebuilt graph cache: {result['edges']} edges")


def cmd_graph_query(args):
    init_db(True); conn=db(); params=[]; where=[]
    if args.node: where.append("(source=? OR target=?)"); params += [args.node,args.node]
    if args.kind: where.append("kind=?"); params.append(args.kind)
    sql="SELECT * FROM graph_edges_cache" + (" WHERE "+" AND ".join(where) if where else "") + " ORDER BY weight DESC LIMIT ?"
    rows=[dict(r) for r in conn.execute(sql, (*params,args.limit)).fetchall()]
    conn.close()
    for r in rows:
        try: r['meta']=json.loads(r.get('meta_json') or '{}')
        except Exception: r['meta']={}
    print_json({"count":len(rows),"edges":rows}) if args.json else [print(f"{r['source']} --{r['kind']}({r['weight']})--> {r['target']}") for r in rows]


def source_neighborhood_payload(source_id: str, limit: int=30) -> dict[str, Any]:
    init_db(True); conn=db(); src=conn.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
    if not src: raise ValueError(f"Unknown source_id {source_id}")
    outgoing=[dict(r) for r in conn.execute("SELECT * FROM citation_contexts WHERE citing_source_id=? ORDER BY context_id LIMIT ?", (source_id,limit)).fetchall()]
    incoming=[dict(r) for r in conn.execute("SELECT * FROM citation_contexts WHERE matched_source_id=? ORDER BY citing_source_id, context_id LIMIT ?", (source_id,limit)).fetchall()]
    refs=[dict(r) for r in conn.execute("SELECT * FROM source_references WHERE source_id=? ORDER BY reference_id LIMIT ?", (source_id,limit)).fetchall()]
    cards=[claim_card(r,"minimal") for r in conn.execute("SELECT * FROM source_cards WHERE source_id=? ORDER BY claim_id LIMIT ?", (source_id,limit)).fetchall()]
    conn.close(); return {"source":dict(src),"outgoing_citation_contexts":outgoing,"incoming_citation_contexts":incoming,"references":refs,"source_cards":cards,"location_usage":source_location_usage(source_id)[:limit]}


def reading_priority_items(query: str='', source_id: str|None=None, missing_only: bool=False, limit: int=30) -> list[dict[str, Any]]:
    init_db(True); conn=db(); where=[]; params=[]
    if source_id: where.append("sr.source_id=?"); params.append(source_id)
    rows=[dict(r) for r in conn.execute(f"""SELECT sr.*, COUNT(cc.context_id) AS cite_count, GROUP_CONCAT(substr(cc.context_text,1,240),' || ') AS contexts FROM source_references sr LEFT JOIN citation_contexts cc ON cc.reference_id=sr.reference_id {('WHERE '+ ' AND '.join(where)) if where else ''} GROUP BY sr.reference_id""", params).fetchall()]
    conn.close(); items=[]
    for r in rows:
        if missing_only and r.get("matched_source_id"): continue
        text=" ".join([r.get("title") or "", r.get("raw_text") or "", r.get("contexts") or ""]); qscore=token_overlap_score(query,text) if query else 0.0
        score=(r.get("cite_count") or 0)+qscore*3+(0.4 if normalize_doi(r.get("doi")) else 0)+(0.5 if not r.get("matched_source_id") else 0)
        if score<=0 and query: continue
        items.append({"score":round(score,3),"reference_id":r["reference_id"],"source_id":r["source_id"],"author_key":r.get("author_key"),"year":r.get("year"),"title":r.get("title"),"doi":r.get("doi"),"matched_source_id":r.get("matched_source_id"),"status":r.get("status"),"citation_context_count":r.get("cite_count"),"context_preview":short(r.get("contexts"),260)})
    return sorted(items,key=lambda x:x['score'],reverse=True)[:limit]


def build_context_plan(query: str, budget: int=3000, limit: int=20) -> dict[str, Any]:
    cards=retrieve_claims(query, max(limit*3,30), "standard", {})
    selected=[]; used=0
    for c in cards:
        cost=estimate_tokens(card_to_brief_text(c))
        if selected and used+cost>budget: continue
        selected.append(c); used+=cost
        if len(selected)>=limit: break
    warnings=[]
    if any(c.get('status')!='verified' for c in selected): warnings.append('Contains non-verified cards; review before final writing.')
    if len(set(c.get('source_id') for c in selected))<2 and len(selected)>2: warnings.append('Low source diversity.')
    return {"query":query,"budget":budget,"estimated_tokens":used,"cards":selected,"warnings":warnings,"deep_dive_commands":[f"python rh2.py context {c['claim_id']} --window 600" for c in selected[:8]]}


def cmd_context_plan(args):
    p=build_context_plan(args.query,args.budget,args.limit)
    if args.json: print_json(p)
    else:
        print(f"# Context plan: {args.query}\nTokens: {p['estimated_tokens']} / {p['budget']}")
        for w in p['warnings']: print(f"WARNING: {w}")
        for c in p['cards']: print(f"- {c['claim_id']} grade:{c.get('evidence_grade')} {c.get('status')} {short(c.get('claim'),180)}")


def cmd_benchmark(args):
    import time as _time
    init_db(True); t0=_time.perf_counter(); stats={}; conn=db()
    for table in ['sources','source_cards','citation_contexts','source_references','claim_relations']:
        try: stats[table]=conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except Exception: stats[table]='missing'
    conn.close(); timings={}
    for name,fn in [('retrieve',lambda: retrieve_claims(args.query,10,'minimal',{})),('review_state',lambda: review_state(50)),('graph_cache',lambda: rebuild_graph_cache() if args.rebuild_graph else {'skipped':True})]:
        a=_time.perf_counter(); fn(); timings[name]=round((_time.perf_counter()-a)*1000,2)
    payload={"stats":stats,"timings_ms":timings,"total_ms":round((_time.perf_counter()-t0)*1000,2),"db_bytes":DB_PATH.stat().st_size if DB_PATH.exists() else 0}
    print_json(payload) if args.json else [print(f"{k}: {v}") for k,v in payload.items()]


def cmd_snapshot(args):
    init_db(True); SNAP=ROOT/'snapshots'; SNAP.mkdir(exist_ok=True)
    name=args.name or datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    dest=SNAP/name; dest.mkdir(exist_ok=True)
    shutil.copy2(DB_PATH,dest/'harness_v2.db')
    meta={"name":name,"created_at":now(),"db_bytes":DB_PATH.stat().st_size,"note":args.note or ""}
    (dest/'snapshot.json').write_text(json.dumps(meta,indent=2),encoding='utf-8')
    print_json(meta) if args.json else print(f"Snapshot written: {dest.relative_to(ROOT)}")


def cmd_restore_snapshot(args):
    src=ROOT/'snapshots'/args.name/'harness_v2.db'
    if not src.exists(): raise SystemExit(f"Snapshot not found: {src}")
    if not args.yes: raise SystemExit("Refusing to overwrite DB without --yes")
    shutil.copy2(src,DB_PATH); print(f"Restored snapshot {args.name}")


def cmd_stats(args):
    init_db(True)
    conn=db(); cur=conn.cursor()
    tables=["sources","spans","source_cards","source_card_suggestions","source_location_signals","synthesis_cards","synthesis_claims","local_embeddings","graph_edges_cache","claim_tags","claim_relations","source_references","citation_contexts","citation_location_suggestions","review_events","review_labels"]
    stats={}
    for t in tables:
        try: stats[t]=cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception: stats[t]="missing"
    stats["db_bytes"]=DB_PATH.stat().st_size if DB_PATH.exists() else 0
    stats["blob_bytes"]=sum(p.stat().st_size for p in BLOBS.glob("*.gz"))
    conn.close()
    print_json(stats) if args.json else [print(f"{k}: {v}") for k,v in stats.items()]


def cmd_review(args):
    init_db(True)
    conn=db(); row=conn.execute("SELECT verification_status FROM source_cards WHERE claim_id=?", (args.claim_id,)).fetchone()
    if not row: raise SystemExit(f"Unknown claim_id: {args.claim_id}")
    old=row["verification_status"]
    conn.execute("UPDATE source_cards SET verification_status=?, updated_at=? WHERE claim_id=?", (args.status, now(), args.claim_id))
    insert_review_event(conn, args.claim_id, old, args.status, args.note or "", args.actor or "human", args.label or [])
    conn.commit(); conn.close(); print(f"{args.claim_id}: {old} -> {args.status}" + (f" labels={args.label}" if args.label else ""))

def main():
    ensure_dirs()
    p=argparse.ArgumentParser(description="Research Harness V2")
    sub=p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(func=cmd_init)
    ing=sub.add_parser("ingest"); ing.add_argument("path"); ing.add_argument("--source-id"); ing.add_argument("--title"); ing.add_argument("--authors",default=""); ing.add_argument("--year",default=""); ing.add_argument("--doi",default=""); ing.add_argument("--source-type",default="peer-reviewed article"); ing.add_argument("--disciplines",default=""); ing.add_argument("--geography",default=""); ing.add_argument("--methodology",default=""); ing.add_argument("--theory",default=""); ing.add_argument("--quality",default="unrated"); ing.add_argument("--notes",default=""); ing.add_argument("--clean-markup", action="store_true", help="Remove harness/Obsidian annotation noise such as ==highlight==, [PAGE UNVERIFIED], and #MA tags before canonical ingest"); ing.add_argument("--parse-map", help="Optional parser sidecar JSON with pages/sections/tables/figures char offsets for MD ingest"); ing.set_defaults(func=cmd_ingest)
    iconv=sub.add_parser("ingest-converted", help="Ingest a pdf_pipeline output directory containing paper.md + optional paper.parse.json/paper.json")
    iconv.add_argument("converted_dir"); iconv.add_argument("--source-id"); iconv.add_argument("--title"); iconv.add_argument("--authors"); iconv.add_argument("--year"); iconv.add_argument("--doi"); iconv.add_argument("--source-type"); iconv.add_argument("--disciplines"); iconv.add_argument("--geography"); iconv.add_argument("--methodology"); iconv.add_argument("--theory"); iconv.add_argument("--quality"); iconv.add_argument("--notes"); iconv.add_argument("--clean-markup", action="store_true"); iconv.add_argument("--json", action="store_true"); iconv.set_defaults(func=cmd_ingest_converted)
    imd=sub.add_parser("ingest-md", help="Alias for ingest with --parse-map support")
    imd.add_argument("path"); imd.add_argument("--source-id"); imd.add_argument("--title"); imd.add_argument("--authors",default=""); imd.add_argument("--year",default=""); imd.add_argument("--doi",default=""); imd.add_argument("--source-type",default="peer-reviewed article"); imd.add_argument("--disciplines",default=""); imd.add_argument("--geography",default=""); imd.add_argument("--methodology",default=""); imd.add_argument("--theory",default=""); imd.add_argument("--quality",default="unrated"); imd.add_argument("--notes",default=""); imd.add_argument("--clean-markup", action="store_true"); imd.add_argument("--parse-map"); imd.set_defaults(func=cmd_ingest)
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
    rev=sub.add_parser("review"); rev.add_argument("claim_id"); rev.add_argument("status", choices=sorted(STATUSES)); rev.add_argument("--note"); rev.add_argument("--actor"); rev.add_argument("--label", action="append", choices=sorted(REVIEW_LABELS), help="Structured review label; repeatable"); rev.set_defaults(func=cmd_review)
    rq=sub.add_parser("review-queue", help="Prioritized queue of claims needing review")
    rq.add_argument("--source-id"); rq.add_argument("--status", help="Comma/semicolon separated statuses"); rq.add_argument("--grade", choices=sorted(EVIDENCE_GRADES)); rq.add_argument("--claim-type"); rq.add_argument("--label", choices=sorted(REVIEW_LABELS)); rq.add_argument("--centrality", choices=["any","high"], default="any"); rq.add_argument("--limit", type=int, default=30); rq.add_argument("--json", action="store_true"); rq.set_defaults(func=cmd_review_queue)
    rp=sub.add_parser("review-packet", help="Assemble claim, evidence, context, history, relations and nearby material for review")
    rp.add_argument("claim_id"); rp.add_argument("--context-mode", choices=["sentence","char"], default="sentence"); rp.add_argument("--sentence-radius", type=int, default=1); rp.add_argument("--outside-paragraph", action="store_true"); rp.add_argument("--window", type=int, default=600); rp.add_argument("--nearby", type=int, default=5); rp.add_argument("--citation-limit", type=int, default=5); rp.add_argument("--json", action="store_true"); rp.set_defaults(func=cmd_review_packet)
    rvc=sub.add_parser("revise-claim", help="Revise claim text/evidence/scope/status while recording a structured review event")
    rvc.add_argument("claim_id"); rvc.add_argument("--claim"); rvc.add_argument("--evidence"); rvc.add_argument("--scope-note"); rvc.add_argument("--claim-type", choices=sorted(CLAIM_TYPES)); rvc.add_argument("--confidence", choices=["low","medium","high"]); rvc.add_argument("--status", choices=sorted(STATUSES)); rvc.add_argument("--char-start", type=int); rvc.add_argument("--char-end", type=int); rvc.add_argument("--label", action="append", choices=sorted(REVIEW_LABELS)); rvc.add_argument("--note"); rvc.add_argument("--actor", default="human"); rvc.add_argument("--json", action="store_true"); rvc.set_defaults(func=cmd_revise_claim)
    sup=sub.add_parser("supersede", help="Mark one claim as superseded by a better claim and create a relation")
    sup.add_argument("old_claim"); sup.add_argument("new_claim"); sup.add_argument("--note"); sup.add_argument("--actor", default="human"); sup.add_argument("--relation-id"); sup.add_argument("--verify-relation", action="store_true"); sup.set_defaults(func=cmd_supersede)
    dup=sub.add_parser("duplicate", help="Mark or record one claim as duplicate of another")
    dup.add_argument("duplicate_claim"); dup.add_argument("canonical_claim"); dup.add_argument("--note"); dup.add_argument("--actor", default="human"); dup.add_argument("--relation-id"); dup.add_argument("--status", choices=sorted(STATUSES), default="candidate_needs_review"); dup.add_argument("--reject-duplicate", action="store_true"); dup.set_defaults(func=cmd_duplicate)
    spl=sub.add_parser("split-claim", help="Create narrower claims from a JSON split spec")
    spl.add_argument("claim_id"); spl.add_argument("--file", required=True, help="JSON array of split claim specs"); spl.add_argument("--status", choices=sorted(STATUSES), default="candidate_needs_review"); spl.add_argument("--inherit-tags", action="store_true"); spl.add_argument("--supersede-original", action="store_true"); spl.add_argument("--note"); spl.add_argument("--actor", default="human"); spl.add_argument("--json", action="store_true"); spl.set_defaults(func=cmd_split_claim)
    rel=sub.add_parser("relate", help="Create or update a relation between two claims")
    rel.add_argument("claim_a"); rel.add_argument("claim_b"); rel.add_argument("relation_type", choices=sorted(RELATION_TYPES)); rel.add_argument("--relation-id"); rel.add_argument("--note"); rel.add_argument("--status", choices=sorted(STATUSES), default="candidate_needs_review"); rel.set_defaults(func=cmd_relate)
    rels=sub.add_parser("relations", help="List claim relations / tension-map edges")
    rels.add_argument("--claim-id"); rels.add_argument("--relation-type", choices=sorted(RELATION_TYPES)); rels.add_argument("--limit", type=int, default=50); rels.add_argument("--json", action="store_true"); rels.set_defaults(func=cmd_relations)
    aud=sub.add_parser("audit-draft", help="Audit a markdown draft for claim-ID traceability and weak evidence")
    aud.add_argument("path"); aud.add_argument("--min-words", type=int, default=14); aud.add_argument("--show-uncited", type=int, default=20); aud.add_argument("--min-sources", type=int, default=3); aud.add_argument("--json", action="store_true"); aud.set_defaults(func=cmd_audit_draft)
    grd=sub.add_parser("evidence-grades", help="List computed claim evidence grades")
    grd.add_argument("--grade", choices=sorted(EVIDENCE_GRADES)); grd.add_argument("--limit", type=int, default=80); grd.add_argument("--json", action="store_true"); grd.set_defaults(func=cmd_evidence_grades)
    rcc=sub.add_parser("repair-citation-contexts", help="Repair citation contexts to full sentence boundaries when citing source blobs are available")
    rcc.add_argument("--source-id"); rcc.add_argument("--context-id"); rcc.add_argument("--limit", type=int, default=500); rcc.add_argument("--show", type=int, default=20); rcc.add_argument("--dry-run", action="store_true"); rcc.add_argument("--json", action="store_true"); rcc.set_defaults(func=cmd_repair_citation_contexts)
    scs=sub.add_parser("suggest-source-cards", help="Suggest source-card candidates from a parsed markdown source")
    scs.add_argument("source_id"); scs.add_argument("--limit", type=int, default=80); scs.add_argument("--min-score", type=float, default=0.25); scs.add_argument("--learned", action="store_true"); scs.add_argument("--learned-weight", type=float, default=0.35); scs.add_argument("--store", action="store_true"); scs.add_argument("--json", action="store_true"); scs.set_defaults(func=cmd_suggest_source_cards)
    lscs=sub.add_parser("source-card-suggestions", help="List stored source-card suggestions")
    lscs.add_argument("--source-id"); lscs.add_argument("--status"); lscs.add_argument("--card-role", choices=sorted(CARD_ROLES)); lscs.add_argument("--limit", type=int, default=80); lscs.add_argument("--json", action="store_true"); lscs.set_defaults(func=cmd_source_card_suggestions)
    ascs=sub.add_parser("accept-source-card-suggestion", help="Create a source card from a stored suggestion")
    ascs.add_argument("suggestion_id"); ascs.add_argument("--claim-id"); ascs.add_argument("--claim"); ascs.add_argument("--claim-type", choices=sorted(CLAIM_TYPES)); ascs.add_argument("--scope-note"); ascs.add_argument("--confidence", choices=["low","medium","high"], default="medium"); ascs.add_argument("--status", choices=sorted(STATUSES), default="candidate_needs_review"); ascs.add_argument("--json", action="store_true"); ascs.set_defaults(func=cmd_accept_source_card_suggestion)
    rscs=sub.add_parser("reject-source-card-suggestion", help="Reject a stored source-card suggestion")
    rscs.add_argument("suggestion_id"); rscs.add_argument("--label", choices=sorted(REVIEW_LABELS)); rscs.set_defaults(func=cmd_reject_source_card_suggestion)
    scl=sub.add_parser("suggest-cited-claim-location", help="Suggest where one citation context is supported in the matched/backtracked source")
    scl.add_argument("context_id"); scl.add_argument("--limit", type=int, default=10); scl.add_argument("--min-score", type=float, default=0.05); scl.add_argument("--learned", action="store_true"); scl.add_argument("--learned-weight", type=float, default=0.35); scl.add_argument("--store", action="store_true"); scl.add_argument("--no-claims", action="store_true"); scl.add_argument("--no-spans", action="store_true"); scl.add_argument("--json", action="store_true"); scl.set_defaults(func=cmd_suggest_cited_claim_location)
    scls=sub.add_parser("suggest-cited-claim-locations", help="Batch-generate citation location suggestions")
    scls.add_argument("--source-id"); scls.add_argument("--limit", type=int, default=50); scls.add_argument("--per-context", type=int, default=5); scls.add_argument("--min-score", type=float, default=0.05); scls.add_argument("--learned", action="store_true"); scls.add_argument("--learned-weight", type=float, default=0.35); scls.add_argument("--store", action="store_true"); scls.add_argument("--json", action="store_true"); scls.set_defaults(func=cmd_suggest_cited_claim_locations)
    cls=sub.add_parser("citation-location-suggestions", help="List stored citation location suggestions")
    cls.add_argument("--context-id"); cls.add_argument("--status"); cls.add_argument("--limit", type=int, default=50); cls.add_argument("--json", action="store_true"); cls.set_defaults(func=cmd_citation_location_suggestions)
    vl=sub.add_parser("verify-location", help="Mark a citation location suggestion as accepted/rejected/etc.")
    vl.add_argument("suggestion_id"); vl.add_argument("status", choices=["candidate_location", "accepted", "rejected", "weak_match", "needs_review"]); vl.add_argument("--note"); vl.set_defaults(func=cmd_verify_location)
    al=sub.add_parser("accept-citation-location", help="Accept a stored location suggestion and optionally create a claim relation")
    al.add_argument("suggestion_id"); al.add_argument("--citing-claim-id"); al.add_argument("--relation-type", choices=sorted(RELATION_TYPES), default="supports"); al.add_argument("--relation-id"); al.add_argument("--status", choices=sorted(STATUSES), default="candidate_needs_review"); al.add_argument("--note"); al.set_defaults(func=cmd_accept_citation_location)
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
    tr=sub.add_parser("train-rankers", help="Train transparent local term-rankers from accepted/rejected suggestions")
    tr.add_argument("--json", action="store_true"); tr.set_defaults(func=cmd_train_rankers)
    rmq=sub.add_parser("reference-match-queue", help="Suggest local source matches for unresolved references")
    rmq.add_argument("--source-id"); rmq.add_argument("--status"); rmq.add_argument("--limit", type=int, default=80); rmq.add_argument("--candidates", type=int, default=5); rmq.add_argument("--json", action="store_true"); rmq.set_defaults(func=cmd_reference_match_queue)
    rrref=sub.add_parser("resolve-reference", help="Manually resolve a reference to a local source and cascade citation contexts")
    rrref.add_argument("reference_id"); rrref.add_argument("--matched-source-id", default=""); rrref.add_argument("--status"); rrref.set_defaults(func=cmd_resolve_reference)
    rp=sub.add_parser("reading-priority", help="Rank cited references to read/import next")
    rp.add_argument("--source-id"); rp.add_argument("--query"); rp.add_argument("--missing-only", action="store_true"); rp.add_argument("--limit", type=int, default=30); rp.add_argument("--json", action="store_true"); rp.set_defaults(func=cmd_reading_priority)
    acl=sub.add_parser("assess-citation-location", help="Heuristic support/correctness assessment for a stored citation-location suggestion")
    acl.add_argument("suggestion_id"); acl.add_argument("--json", action="store_true"); acl.set_defaults(func=cmd_assess_citation_location)
    bfm=sub.add_parser("backfill-source-matches", help="Match unresolved existing references/citation contexts to a newly ingested local source")
    bfm.add_argument("source_id"); bfm.add_argument("--min-score", type=float, default=0.75); bfm.add_argument("--force", action="store_true"); bfm.add_argument("--dry-run", action="store_true"); bfm.add_argument("--limit", type=int, default=50); bfm.add_argument("--json", action="store_true"); bfm.set_defaults(func=cmd_backfill_source_matches)
    slcs=sub.add_parser("suggest-locations-for-cited-source", help="Suggest support locations for every citation context matched to one cited source")
    slcs.add_argument("source_id"); slcs.add_argument("--limit", type=int, default=500); slcs.add_argument("--per-context", type=int, default=5); slcs.add_argument("--min-score", type=float, default=0.05); slcs.add_argument("--store", action="store_true"); slcs.add_argument("--show", type=int, default=40); slcs.add_argument("--json", action="store_true"); slcs.set_defaults(func=cmd_suggest_locations_for_cited_source)
    slu=sub.add_parser("source-location-usage", help="Aggregate citation-location suggestions by cited source range")
    slu.add_argument("source_id"); slu.add_argument("--min-overlap", type=float, default=0.45); slu.add_argument("--limit", type=int, default=50); slu.add_argument("--json", action="store_true"); slu.set_defaults(func=cmd_source_location_usage)
    pcl=sub.add_parser("promote-cited-locations", help="Turn repeatedly cited support ranges into source-card suggestions")
    pcl.add_argument("source_id"); pcl.add_argument("--min-strength", type=float, default=1.0); pcl.add_argument("--limit", type=int, default=30); pcl.add_argument("--store", action="store_true"); pcl.add_argument("--json", action="store_true"); pcl.set_defaults(func=cmd_promote_cited_locations)
    ccg=sub.add_parser("central-claims", help="Rank central/high-value source cards by citation usage, review status and brief use")
    ccg.add_argument("--source-id"); ccg.add_argument("--topic"); ccg.add_argument("--limit", type=int, default=30); ccg.add_argument("--nonzero", action="store_true"); ccg.add_argument("--json", action="store_true"); ccg.set_defaults(func=cmd_central_claims)
    mcu=sub.add_parser("most-cited-unverified", help="List unverified source cards with citation usage or high centrality")
    mcu.add_argument("--source-id"); mcu.add_argument("--topic"); mcu.add_argument("--limit", type=int, default=30); mcu.add_argument("--cited-only", action="store_true"); mcu.add_argument("--json", action="store_true"); mcu.set_defaults(func=cmd_most_cited_unverified)
    sn=sub.add_parser("source-neighborhood", help="Show incoming/outgoing citation and source-card neighborhood for one source")
    sn.add_argument("source_id"); sn.add_argument("--limit", type=int, default=30); sn.add_argument("--json", action="store_true"); sn.set_defaults(func=cmd_source_neighborhood)
    cn=sub.add_parser("claim-network", help="Export a lightweight claim/source/citation graph")
    cn.add_argument("--source-id"); cn.add_argument("--limit", type=int, default=500); cn.add_argument("--json", action="store_true"); cn.set_defaults(func=cmd_claim_network)
    rtd=sub.add_parser("redteam-draft", help="Stronger draft audit: unsupported claims, weak evidence, overclaiming and source diversity")
    rtd.add_argument("path"); rtd.add_argument("--min-words", type=int, default=14); rtd.add_argument("--show-uncited", type=int, default=25); rtd.add_argument("--min-claims-for-source-warning", type=int, default=3); rtd.add_argument("--limit", type=int, default=80); rtd.add_argument("--json", action="store_true"); rtd.set_defaults(func=cmd_redteam_draft)
    rls=sub.add_parser("refresh-location-signals", help="Rebuild unified source-location signal table from reviews/backtracks")
    rls.add_argument("--source-id"); rls.add_argument("--json", action="store_true"); rls.set_defaults(func=cmd_refresh_location_signals)
    lsg=sub.add_parser("location-signals", help="List source-location learning/review signals")
    lsg.add_argument("--source-id"); lsg.add_argument("--signal-type"); lsg.add_argument("--polarity"); lsg.add_argument("--limit", type=int, default=80); lsg.add_argument("--json", action="store_true"); lsg.set_defaults(func=cmd_location_signals)
    syn=sub.add_parser("create-synthesis", help="Create/update a thesis synthesis card backed by source cards")
    syn.add_argument("--synthesis-id"); syn.add_argument("--title", default=""); syn.add_argument("--text", required=True); syn.add_argument("--claims", action="append", required=True); syn.add_argument("--topic", default=""); syn.add_argument("--scope-note", default=""); syn.add_argument("--status", choices=sorted(STATUSES), default="candidate_needs_review"); syn.add_argument("--role", default="supports"); syn.add_argument("--note", default=""); syn.set_defaults(func=cmd_create_synthesis)
    syns=sub.add_parser("synthesis-cards", help="List synthesis cards")
    syns.add_argument("--topic"); syns.add_argument("--status"); syns.add_argument("--limit", type=int, default=50); syns.add_argument("--json", action="store_true"); syns.set_defaults(func=cmd_synthesis_cards)
    synb=sub.add_parser("synthesis-brief", help="Render one synthesis card with supporting source cards")
    synb.add_argument("synthesis_id"); synb.add_argument("--json", action="store_true"); synb.set_defaults(func=cmd_synthesis_brief)
    rui=sub.add_parser("export-review-ui", help="Export a static local review cockpit")
    rui.add_argument("--out", default="reports/review_ui"); rui.add_argument("--limit", type=int, default=120); rui.set_defaults(func=cmd_export_review_ui)
    be=sub.add_parser("build-embeddings", help="Build local hashed sparse embeddings for hybrid retrieval")
    be.add_argument("--level", default="source_cards"); be.add_argument("--source-id"); be.add_argument("--dim", type=int, default=2048); be.add_argument("--json", action="store_true"); be.set_defaults(func=cmd_build_embeddings)
    hr=sub.add_parser("hybrid-retrieve", help="Retrieve with lexical + local sparse semantic embeddings")
    hr.add_argument("query"); hr.add_argument("--limit", type=int, default=10); hr.add_argument("--source-id"); hr.add_argument("--verified-only", action="store_true"); hr.add_argument("--status"); hr.add_argument("--claim-type"); hr.add_argument("--card-role"); hr.add_argument("--semantic-weight", type=float, default=0.35); hr.add_argument("--dim", type=int, default=2048); hr.add_argument("--semantic-only", action="store_true"); hr.add_argument("--include-semantic-only", action="store_true"); hr.add_argument("--json", action="store_true"); hr.set_defaults(func=cmd_hybrid_retrieve)
    pt=sub.add_parser("parser-tournament", help="Compare local parser text-extraction modes on one PDF")
    pt.add_argument("pdf"); pt.add_argument("--modes", default="pdftotext,pymupdf4llm,auto,reorder"); pt.add_argument("--timeout", type=int, default=240); pt.add_argument("--out"); pt.add_argument("--json", action="store_true"); pt.set_defaults(func=cmd_parser_tournament)
    rsy=sub.add_parser("redteam-synthesis", help="Audit a synthesis card against its supporting source cards")
    rsy.add_argument("synthesis_id"); rsy.add_argument("--limit", type=int, default=80); rsy.add_argument("--json", action="store_true"); rsy.set_defaults(func=cmd_redteam_synthesis)
    rui2=sub.add_parser("review-ui", help="Run an interactive local review cockpit server")
    rui2.add_argument("--host", default="127.0.0.1"); rui2.add_argument("--port", type=int, default=8765); rui2.add_argument("--limit", type=int, default=120); rui2.add_argument("--verbose", action="store_true"); rui2.set_defaults(func=cmd_review_ui)
    rrn=sub.add_parser("recursive-run", help="Recursive database-native orchestration loop over backfill/suggestions/promotions/training")
    rrn.add_argument("--source-id"); rrn.add_argument("--rounds", type=int, default=3); rrn.add_argument("--until-stable", action="store_true"); rrn.add_argument("--dry-run", action="store_true"); rrn.add_argument("--quiet", action="store_true"); rrn.add_argument("--learned", action="store_true"); rrn.add_argument("--learned-weight", type=float, default=0.35); rrn.add_argument("--backfill", action="store_true", default=True); rrn.add_argument("--no-backfill", dest="backfill", action="store_false"); rrn.add_argument("--suggest-locations", action="store_true", default=True); rrn.add_argument("--no-suggest-locations", dest="suggest_locations", action="store_false"); rrn.add_argument("--suggest-cards", action="store_true", default=True); rrn.add_argument("--no-suggest-cards", dest="suggest_cards", action="store_false"); rrn.add_argument("--promote", action="store_true", default=True); rrn.add_argument("--no-promote", dest="promote", action="store_false"); rrn.add_argument("--refresh-signals", action="store_true", default=True); rrn.add_argument("--train", action="store_true", default=True); rrn.add_argument("--min-match-score", type=float, default=0.75); rrn.add_argument("--min-location-score", type=float, default=0.08); rrn.add_argument("--min-card-score", type=float, default=0.3); rrn.add_argument("--min-usage-strength", type=float, default=1.0); rrn.add_argument("--context-limit", type=int, default=500); rrn.add_argument("--per-context", type=int, default=5); rrn.add_argument("--card-limit", type=int, default=40); rrn.add_argument("--review-limit", type=int, default=120); rrn.add_argument("--json", action="store_true"); rrn.set_defaults(func=cmd_recursive_run)
    ts=sub.add_parser("tool-schema", help="Print JSON schema for lightweight MCP/JSONL tools")
    ts.set_defaults(func=cmd_tool_schema)
    mcp=sub.add_parser("mcp-server", help="Run lightweight JSONL MCP-like tool server over stdin/stdout")
    mcp.set_defaults(func=cmd_mcp_server)
    rgc=sub.add_parser("rebuild-graph-cache", help="Rebuild denormalized graph edge cache")
    rgc.add_argument("--json", action="store_true"); rgc.set_defaults(func=cmd_rebuild_graph_cache)
    gq=sub.add_parser("graph-query", help="Query graph edge cache")
    gq.add_argument("--node"); gq.add_argument("--kind"); gq.add_argument("--limit", type=int, default=50); gq.add_argument("--json", action="store_true"); gq.set_defaults(func=cmd_graph_query)
    cp=sub.add_parser("context-plan", help="Token-budgeted evidence/context plan for a writing task")
    cp.add_argument("query"); cp.add_argument("--budget", type=int, default=3000); cp.add_argument("--limit", type=int, default=20); cp.add_argument("--json", action="store_true"); cp.set_defaults(func=cmd_context_plan)
    bm=sub.add_parser("benchmark", help="Benchmark core query/cache operations")
    bm.add_argument("--query", default="trust policy stability"); bm.add_argument("--rebuild-graph", action="store_true"); bm.add_argument("--json", action="store_true"); bm.set_defaults(func=cmd_benchmark)
    snp=sub.add_parser("snapshot", help="Snapshot the SQLite DB for reproducibility")
    snp.add_argument("--name"); snp.add_argument("--note"); snp.add_argument("--json", action="store_true"); snp.set_defaults(func=cmd_snapshot)
    rst=sub.add_parser("restore-snapshot", help="Restore a DB snapshot")
    rst.add_argument("name"); rst.add_argument("--yes", action="store_true"); rst.set_defaults(func=cmd_restore_snapshot)
    st=sub.add_parser("stats"); st.add_argument("--json", action="store_true"); st.set_defaults(func=cmd_stats)
    args=p.parse_args(); args.func(args)

if __name__ == "__main__":
    main()
