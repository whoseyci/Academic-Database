#!/usr/bin/env python3
"""Export V2 database contents to a static JSON bundle for GitHub Pages.

The static export deliberately avoids publishing full source texts. It exports:
- source metadata,
- claim cards and evidence excerpts,
- bounded context snippets around evidence,
- tags,
- graph/network nodes and edges,
- chapter briefs.
"""
from __future__ import annotations

import json, sqlite3, gzip, re
from pathlib import Path
from collections import Counter, defaultdict

ROOT = Path(__file__).resolve().parent
DB = ROOT / "harness_v2.db"
DOCS = ROOT / "docs"
DATA = DOCS / "data"
BLOBS = ROOT / "blobs"
EXPORT = DATA / "harness_export.json"


def short(s, n=280):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def read_blob(row):
    p = ROOT / row.get("blob_path", "")
    if not p.exists():
        return ""
    try:
        with gzip.open(p, "rt", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


def infer_topic(claim, tags):
    constructs = [t["tag"] for t in tags if t["tag_type"] == "construct"]
    if constructs:
        return constructs[0]
    rq = [t["tag"] for t in tags if t["tag_type"] == "rq"]
    if rq:
        return rq[0]
    text = (claim.get("claim", "") + " " + claim.get("evidence", "")).lower()
    rules = [
        ("institutional trust", ["trust", "policy stability", "institution"]),
        ("social norms", ["neighbour", "neighbor", "peers", "social", "opinion"]),
        ("contract design", ["contract", "flexib", "bureaucr", "payment", "compensation"]),
        ("additionality", ["additionality", "self-selection", "baseline"]),
        ("landscape context", ["landscape", "semi-natural", "habitat"]),
        ("pollinator resources", ["pollen", "nectar", "bumblebee", "nesting"]),
        ("methodology", ["model", "regression", "simulation", "method"]),
        ("limitations", ["limitation", "could not", "underestimated", "not considered"]),
    ]
    for label, keys in rules:
        if any(k in text for k in keys):
            return label
    return claim.get("claim_type") or "unknown"


def main():
    DOCS.mkdir(exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    sources = [dict(r) for r in conn.execute("SELECT * FROM sources ORDER BY source_id")]
    source_by_id = {s["source_id"]: s for s in sources}
    claims = [dict(r) for r in conn.execute("SELECT * FROM claims ORDER BY source_id, claim_id")]
    tag_rows = [dict(r) for r in conn.execute("SELECT claim_id, tag_type, tag FROM claim_tags ORDER BY claim_id, tag_type, tag")]
    tags_by_claim = defaultdict(list)
    for t in tag_rows:
        tags_by_claim[t["claim_id"]].append({"tag_type": t["tag_type"], "tag": t["tag"]})

    source_text_cache = {s["source_id"]: read_blob(s) for s in sources}
    claim_cards = []
    for c in claims:
        s = source_by_id.get(c["source_id"], {})
        text = source_text_cache.get(c["source_id"], "")
        start = c.get("char_start") or 0
        end = c.get("char_end") or start
        try:
            start = int(start); end = int(end)
        except Exception:
            start = end = 0
        context = ""
        if text and start >= 0:
            context = text[max(0, start - 450): min(len(text), end + 450)]
        tags = tags_by_claim[c["claim_id"]]
        topic = infer_topic(c, tags)
        claim_cards.append({
            "claim_id": c["claim_id"],
            "source_id": c["source_id"],
            "citation_hint": f"{s.get('authors','Unknown')} ({s.get('year','n.d.')})",
            "source_title": s.get("title", ""),
            "year": s.get("year", ""),
            "doi": s.get("doi", ""),
            "claim": c.get("claim", ""),
            "evidence": c.get("evidence", ""),
            "claim_representation": c.get("claim_representation", ""),
            "claim_type": c.get("claim_type", ""),
            "page": c.get("page", ""),
            "page_status": c.get("page_status", ""),
            "verification_status": c.get("verification_status", ""),
            "confidence": c.get("confidence", ""),
            "scope_note": c.get("scope_note", ""),
            "line_start": c.get("line_start"),
            "line_end": c.get("line_end"),
            "topic": topic,
            "tags": tags,
            "tag_text": " ".join(f"{t['tag_type']}:{t['tag']}" for t in tags),
            "context_snippet": context,
        })

    # Stats
    stats = {
        "source_count": len(sources),
        "claim_count": len(claim_cards),
        "status_counts": Counter(c["verification_status"] for c in claim_cards),
        "claim_type_counts": Counter(c["claim_type"] for c in claim_cards),
        "topic_counts": Counter(c["topic"] for c in claim_cards),
        "year_counts": Counter(str(s.get("year", "")) for s in sources if s.get("year")),
        "representation_counts": Counter(c["claim_representation"] for c in claim_cards),
    }
    stats = {k: dict(v) if isinstance(v, Counter) else v for k, v in stats.items()}

    # Network nodes and edges.
    nodes = []
    edges = []
    seen = set()

    def add_node(node_id, label, kind, **extra):
        if node_id in seen:
            return
        seen.add(node_id)
        nodes.append({"id": node_id, "label": label, "kind": kind, **extra})

    def add_edge(a, b, kind, weight=1):
        edges.append({"source": a, "target": b, "kind": kind, "weight": weight})

    for s in sources:
        sid = f"source:{s['source_id']}"
        add_node(sid, s["source_id"], "source", year=s.get("year"), title=s.get("title"), authors=s.get("authors"))
        if s.get("year"):
            yid = f"year:{s['year']}"
            add_node(yid, str(s["year"]), "year")
            add_edge(sid, yid, "published_in", 1)
        for field in ["disciplines", "geography", "methodology"]:
            for val in re.split(r"[;,|]", str(s.get(field, ""))):
                val = val.strip()
                if not val:
                    continue
                nid = f"{field}:{val.lower()}"
                add_node(nid, val, field.rstrip("s"))
                add_edge(sid, nid, field, 1)

    for c in claim_cards:
        cid = f"claim:{c['claim_id']}"
        add_node(cid, c["claim_id"], "claim", topic=c["topic"], status=c["verification_status"], claim_type=c["claim_type"])
        add_edge(f"source:{c['source_id']}", cid, "has_claim", 1)
        topic_id = f"topic:{c['topic'].lower()}"
        add_node(topic_id, c["topic"], "topic")
        add_edge(cid, topic_id, "about", 2)
        type_id = f"claim_type:{c['claim_type']}"
        add_node(type_id, c["claim_type"], "claim_type")
        add_edge(cid, type_id, "is_type", 1)
        for t in c["tags"]:
            if t["tag_type"] in ["construct", "rq"]:
                nid = f"{t['tag_type']}:{t['tag'].lower()}"
                add_node(nid, t["tag"], t["tag_type"])
                add_edge(cid, nid, f"tag:{t['tag_type']}", 1)

    # Paper-paper similarity by shared topic/tag metadata.
    source_topics = defaultdict(set)
    source_tags = defaultdict(set)
    for c in claim_cards:
        source_topics[c["source_id"]].add(c["topic"])
        for t in c["tags"]:
            if t["tag_type"] in ["construct", "rq", "discipline", "geography", "methodology"]:
                source_tags[c["source_id"]].add(f"{t['tag_type']}:{t['tag'].lower()}")
    paper_edges = []
    ids = [s["source_id"] for s in sources]
    for i,a in enumerate(ids):
        for b in ids[i+1:]:
            A = source_topics[a] | source_tags[a]
            B = source_topics[b] | source_tags[b]
            sim = len(A & B) / max(1, len(A | B))
            if sim > 0:
                paper_edges.append({"source": a, "target": b, "similarity": round(sim, 3), "shared": sorted(A & B)[:20]})

    # Chapter briefs, if present.
    briefs = []
    for path in sorted((ROOT / "exports").glob("chapter_brief_*.json")):
        try:
            briefs.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            pass

    payload = {
        "generated_at": __import__("datetime").datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "notes": "Static export. Full source texts are intentionally not included; only metadata, claims, evidence excerpts and bounded context snippets are exported.",
        "sources": sources,
        "claims": claim_cards,
        "stats": stats,
        "network": {"nodes": nodes, "edges": edges, "paper_edges": paper_edges},
        "chapter_briefs": briefs,
    }
    EXPORT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(EXPORT.relative_to(ROOT))
    print(f"sources={len(sources)} claims={len(claim_cards)} nodes={len(nodes)} edges={len(edges)} briefs={len(briefs)}")


if __name__ == "__main__":
    main()
