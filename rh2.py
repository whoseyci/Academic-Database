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

import argparse, csv, gzip, hashlib, json, re, shutil, sqlite3, sys, textwrap, uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "harness_v2.db"
BLOBS = ROOT / "blobs"
EXPORTS = ROOT / "exports"
REPORTS = ROOT / "reports"
CHAPTER_PROFILES = ROOT / "config" / "chapter_profiles"

CLAIM_TYPES = {
    "empirical finding", "theoretical claim", "methodological claim", "definition",
    "policy implication", "limitation", "background", "contradiction", "unknown"
}
STATUSES = {"verified", "rejected", "candidate_needs_review", "needs_page_check", "needs_source_check"}

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
    CREATE TABLE IF NOT EXISTS claims (
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
        FOREIGN KEY(claim_id) REFERENCES claims(claim_id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS claim_relations (
        relation_id TEXT PRIMARY KEY,
        claim_a TEXT, claim_b TEXT,
        relation_type TEXT,
        note TEXT,
        status TEXT,
        created_at TEXT,
        FOREIGN KEY(claim_a) REFERENCES claims(claim_id) ON DELETE CASCADE,
        FOREIGN KEY(claim_b) REFERENCES claims(claim_id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS review_events (
        event_id TEXT PRIMARY KEY,
        claim_id TEXT,
        from_status TEXT,
        to_status TEXT,
        note TEXT,
        actor TEXT,
        created_at TEXT,
        FOREIGN KEY(claim_id) REFERENCES claims(claim_id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS query_cache (
        query_hash TEXT PRIMARY KEY,
        query TEXT,
        filters_json TEXT,
        result_json TEXT,
        created_at TEXT
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS claims_fts USING fts5(claim_id UNINDEXED, claim, evidence, tags);
    CREATE INDEX IF NOT EXISTS idx_claims_source ON claims(source_id);
    CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(verification_status);
    CREATE INDEX IF NOT EXISTS idx_claims_type ON claims(claim_type);
    CREATE INDEX IF NOT EXISTS idx_claims_page ON claims(source_id, page);
    CREATE INDEX IF NOT EXISTS idx_spans_source_kind ON spans(source_id, kind);
    CREATE INDEX IF NOT EXISTS idx_spans_offsets ON spans(source_id, char_start, char_end);
    CREATE INDEX IF NOT EXISTS idx_tags_type_tag ON claim_tags(tag_type, tag);
    """)
    conn.commit(); conn.close()
    if not quiet:
        print(f"Initialized V2 DB: {DB_PATH}")


def read_source_text(source_id: str) -> str:
    conn=db(); row=conn.execute("SELECT blob_path FROM sources WHERE source_id=?", (source_id,)).fetchone(); conn.close()
    if not row:
        raise SystemExit(f"Unknown source_id: {source_id}")
    path = ROOT / row["blob_path"]
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
    if char_start is None:
        return None, None
    conn=db()
    row=conn.execute("""SELECT span_id, page_start FROM spans WHERE source_id=? AND kind='page' AND char_start<=? AND char_end>=? ORDER BY char_start LIMIT 1""", (source_id,char_start,char_start)).fetchone()
    conn.close()
    return (row["page_start"], row["span_id"]) if row else (None, None)


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
    conn=db(); rows=conn.execute("SELECT claim_id FROM claims WHERE source_id=?", (source_id,)).fetchall(); conn.close()
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


def tags_for_claim(claim_id: str) -> list[str]:
    conn=db(); rows=conn.execute("SELECT tag_type, tag FROM claim_tags WHERE claim_id=?", (claim_id,)).fetchall(); conn.close()
    return [f"{r['tag_type']}:{r['tag']}" for r in rows]


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
    cur.execute("""INSERT OR REPLACE INTO claims VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
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
    card={
        "claim_id": d.get("claim_id"), "score": round(float(d.get("score", 0) or 0),4),
        "source_id": d.get("source_id"), "citation_hint": citation, "page": d.get("page"),
        "status": d.get("verification_status"), "claim_type": d.get("claim_type"),
        "claim_representation": d.get("claim_representation"), "claim": d.get("claim"),
    }
    if d.get("why_retrieved"): card["why_retrieved"] = d.get("why_retrieved")
    if fields in ["standard", "full"]:
        card.update({"evidence": d.get("evidence"), "scope_note": d.get("scope_note"), "line_start": d.get("line_start"), "line_end": d.get("line_end")})
    if fields == "full":
        card.update(d)
        card["tags"] = tags_for_claim(d.get("claim_id"))
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
    ingest_source(Path(args.path), args.source_id or slug(args.title or Path(args.path).stem), meta)
    print(f"Ingested {args.source_id or slug(args.title or Path(args.path).stem)}")


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
    conn=db(); dup=conn.execute("SELECT * FROM claims WHERE source_id=? AND char_start=? AND char_end=?", (sid, claim["char_start"], claim["char_end"])).fetchall(); conn.close()
    if dup and not args.allow_duplicate:
        payload={"created": False, "reason": "matching source span exists", "existing": [claim_card(r, args.fields) for r in dup], "proposed": claim}
        print_json(payload) if args.json else print_json(payload)
        return
    upsert_claim(claim,tags)
    conn=db(); row=conn.execute("SELECT * FROM claims WHERE claim_id=?", (claim["claim_id"],)).fetchone(); conn.close()
    payload={"created": True, "claim": claim_card(row,args.fields), "chunk_span_id": chunk, "page_span_id": page_span}
    print_json(payload) if args.json else print(f"Created {claim['claim_id']} | {sid} | page {page or '?'}")


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
    all_rows=cur.execute("SELECT * FROM claims WHERE verification_status!='rejected'").fetchall()
    candidates=[]
    for r in all_rows:
        d=dict(r)
        if filters.get("source_id") and d["source_id"] != filters["source_id"]: continue
        if filters.get("source_ids") and d["source_id"] not in filters["source_ids"]: continue
        if filters.get("verified_only") and d["verification_status"] != "verified": continue
        if filters.get("statuses") and d["verification_status"] not in filters["statuses"]: continue
        if filters.get("claim_types") and d["claim_type"] not in filters["claim_types"]: continue
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
    filters={"source_id": args.source_id, "verified_only": args.verified_only, "statuses": split_list(args.status), "claim_types": split_list(args.claim_type)}
    results=retrieve_claims(args.query,args.limit,args.fields,filters)
    payload={"query": args.query, "count": len(results), "results": results}
    if args.json: print_json(payload)
    else:
        print(f"# retrieve: {args.query}\n")
        for r in results:
            print(f"- **{r['claim_id']}** [{r['score']}] {r['citation_hint']} p.{r.get('page') or '?'} · {r['status']} · {r['claim_type']}")
            print(f"  {r['claim']}")
            if args.fields in ["standard","full"] and r.get("evidence"): print(f"  evidence: {short(r['evidence'])}")
            if r.get("why_retrieved"): print(f"  why: {', '.join(r['why_retrieved'])}")


def cmd_context(args):
    conn=db(); row=conn.execute("SELECT * FROM claims WHERE claim_id=?", (args.claim_id,)).fetchone(); conn.close()
    if not row: raise SystemExit(f"Unknown claim_id: {args.claim_id}")
    d=dict(row); text=read_source_text(d["source_id"]); start=max(0, int(d["char_start"] or 0)-args.window); end=min(len(text), int(d["char_end"] or d["char_start"] or 0)+args.window)
    payload={"claim": claim_card(d,args.fields), "context_start": start, "context_end": end, "context": text[start:end]}
    if args.json: print_json(payload)
    else:
        print(f"{d['claim_id']} | {d['source_id']} | p.{d.get('page') or '?'} | lines {d.get('line_start')}-{d.get('line_end')}\n")
        print(textwrap.fill(d["claim"], width=100)); print("\n--- context ---"); print(payload["context"])


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
        evidence=text[int(loc["char_start"]):int(loc["char_end"])].strip()
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


def load_chapter_profile(path_or_id: str) -> dict[str, Any]:
    p=Path(path_or_id)
    if not p.exists(): p=CHAPTER_PROFILES/f"{path_or_id}.json"
    if not p.exists(): raise SystemExit(f"Chapter profile not found: {path_or_id}")
    return json.loads(p.read_text(encoding="utf-8"))


def cmd_chapter_brief(args):
    prof=load_chapter_profile(args.profile)
    chapter_id=prof.get("chapter_id") or slug(prof.get("chapter_title","chapter"))
    global_filters=prof.get("global_filters", {})
    sections=[]; used=set()
    for sec in prof.get("sections", []):
        q=" ".join(sec.get("queries", []) or [sec.get("query", "")])
        filters=dict(global_filters)
        if sec.get("source_ids"): filters["source_ids"]=sec.get("source_ids")
        if sec.get("claim_types"): filters["claim_types"]=sec.get("claim_types")
        if sec.get("statuses"): filters["statuses"]=sec.get("statuses")
        cards=retrieve_claims(q, sec.get("limit", args.limit), "standard", filters)
        for card in cards: used.add(card["claim_id"])
        sections.append({"section_id": sec.get("section_id"), "heading": sec.get("heading"), "writing_goal": sec.get("writing_goal", ""), "query": q, "claims": cards})
    conn=db()
    status_counts={r["verification_status"]: r["n"] for r in conn.execute(f"SELECT verification_status, COUNT(*) n FROM claims WHERE claim_id IN ({','.join(['?']*len(used)) or "''"}) GROUP BY verification_status", list(used)).fetchall()} if used else {}
    source_counts={r["source_id"]: r["n"] for r in conn.execute(f"SELECT source_id, COUNT(*) n FROM claims WHERE claim_id IN ({','.join(['?']*len(used)) or "''"}) GROUP BY source_id", list(used)).fetchall()} if used else {}
    conn.close()
    warnings=[]
    if status_counts.get("verified",0)==0 and used: warnings.append("No verified claims in this chapter brief; use candidate claims for drafting only after review.")
    if len(source_counts) < prof.get("minimum_source_diversity", 1): warnings.append("Source diversity is below profile target.")
    packet={"chapter_id": chapter_id, "chapter_title": prof.get("chapter_title"), "purpose": prof.get("purpose"), "writing_contract": prof.get("writing_contract", default_writing_contract()), "claim_count": len(used), "source_counts": source_counts, "status_counts": status_counts, "warnings": warnings, "sections": sections}
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
        for c in sec.get("claims", []):
            lines.append(f"- **{c['claim_id']}** [{c['score']}] {c['citation_hint']} p.{c.get('page') or '?'} · {c['status']} · {c['claim_type']} · `{c.get('claim_representation')}`")
            lines.append(f"  - Claim: {c['claim']}")
            lines.append(f"  - Evidence: {short(c.get('evidence',''), 300)}")
            lines.append(f"  - Deep dive: `python rh2.py context {c['claim_id']} --window 500`")
            lines.append("")
    return "\n".join(lines)


def cmd_stats(args):
    conn=db(); cur=conn.cursor()
    tables=["sources","spans","claims","claim_tags","claim_relations","review_events"]
    stats={}
    for t in tables:
        try: stats[t]=cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception: stats[t]="missing"
    stats["db_bytes"]=DB_PATH.stat().st_size if DB_PATH.exists() else 0
    stats["blob_bytes"]=sum(p.stat().st_size for p in BLOBS.glob("*.gz"))
    conn.close()
    print_json(stats) if args.json else [print(f"{k}: {v}") for k,v in stats.items()]


def cmd_review(args):
    conn=db(); row=conn.execute("SELECT verification_status FROM claims WHERE claim_id=?", (args.claim_id,)).fetchone()
    if not row: raise SystemExit(f"Unknown claim_id: {args.claim_id}")
    old=row["verification_status"]
    conn.execute("UPDATE claims SET verification_status=?, updated_at=? WHERE claim_id=?", (args.status, now(), args.claim_id))
    conn.execute("INSERT INTO review_events VALUES (?,?,?,?,?,?,?)", (str(uuid.uuid4()), args.claim_id, old, args.status, args.note or "", args.actor or "human", now()))
    conn.commit(); conn.close(); print(f"{args.claim_id}: {old} -> {args.status}")


def main():
    ensure_dirs()
    p=argparse.ArgumentParser(description="Research Harness V2")
    sub=p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(func=cmd_init)
    ing=sub.add_parser("ingest"); ing.add_argument("path"); ing.add_argument("--source-id"); ing.add_argument("--title"); ing.add_argument("--authors",default=""); ing.add_argument("--year",default=""); ing.add_argument("--doi",default=""); ing.add_argument("--source-type",default="peer-reviewed article"); ing.add_argument("--disciplines",default=""); ing.add_argument("--geography",default=""); ing.add_argument("--methodology",default=""); ing.add_argument("--theory",default=""); ing.add_argument("--quality",default="unrated"); ing.add_argument("--notes",default=""); ing.set_defaults(func=cmd_ingest)
    imp=sub.add_parser("import-v1"); imp.add_argument("v1_path"); imp.set_defaults(func=cmd_import_v1)
    mark=sub.add_parser("mark-claim"); mark.add_argument("quote", nargs="?"); mark.add_argument("--text"); mark.add_argument("--source-id"); mark.add_argument("--claim-id"); mark.add_argument("--claim"); mark.add_argument("--claim-representation", choices=["source_quote","lightly_normalized_source","paraphrase"]); mark.add_argument("--claim-type", choices=sorted(CLAIM_TYPES)); mark.add_argument("--constructs"); mark.add_argument("--rq-tags"); mark.add_argument("--discipline"); mark.add_argument("--geography"); mark.add_argument("--methodology"); mark.add_argument("--scope-note"); mark.add_argument("--confidence", choices=["low","medium","high"], default="high"); mark.add_argument("--status", choices=sorted(STATUSES), default="candidate_needs_review"); mark.add_argument("--allow-duplicate", action="store_true"); mark.add_argument("--fields", choices=["minimal","standard","full"], default="standard"); mark.add_argument("--json", action="store_true"); mark.set_defaults(func=cmd_mark_claim)
    ret=sub.add_parser("retrieve"); ret.add_argument("query"); ret.add_argument("--limit", type=int, default=8); ret.add_argument("--source-id"); ret.add_argument("--verified-only", action="store_true"); ret.add_argument("--status"); ret.add_argument("--claim-type"); ret.add_argument("--fields", choices=["minimal","standard","full"], default="minimal"); ret.add_argument("--json", action="store_true"); ret.set_defaults(func=cmd_retrieve)
    ctx=sub.add_parser("context"); ctx.add_argument("claim_id"); ctx.add_argument("--window", type=int, default=500); ctx.add_argument("--fields", choices=["minimal","standard","full"], default="standard"); ctx.add_argument("--json", action="store_true"); ctx.set_defaults(func=cmd_context)
    chap=sub.add_parser("chapter-brief"); chap.add_argument("profile"); chap.add_argument("--limit", type=int, default=10); chap.add_argument("--json", action="store_true"); chap.set_defaults(func=cmd_chapter_brief)
    rev=sub.add_parser("review"); rev.add_argument("claim_id"); rev.add_argument("status", choices=sorted(STATUSES)); rev.add_argument("--note"); rev.add_argument("--actor"); rev.set_defaults(func=cmd_review)
    st=sub.add_parser("stats"); st.add_argument("--json", action="store_true"); st.set_defaults(func=cmd_stats)
    args=p.parse_args(); args.func(args)

if __name__ == "__main__":
    main()
