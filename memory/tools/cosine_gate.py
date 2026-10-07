#!/usr/bin/env python3
"""cosine_gate.py -- do two venvs embed identically? The gate for a library-only change (2026-10-01;
venv-autopatch runs it before any change to the memory server's venv).

embed_fp (heads/score.py) covers the model files, truncate_dim, prompt and prefix: it cannot see a
torch / sentence-transformers / transformers change. A library bump that moved vectors would leave
every fingerprint matching while the store mixed old and new vectors and the heads scored drifted
ones. So a venv change serves a store only after this passes.

  sample  --out S.json [--store DIR] [--canaries FILE] [--before ISO] [--n-mem 40] [--n-news 10]
          Read-only: 40 memories + 10 news items stored before --before (default: now), chosen by
          sha256(id) so the same store gives the same sample on any run; plus every canary query, if
          the canaries file exists (a fresh install has none). S.json holds real
          memory text: write it under a scratch or root-only path, never into a repo.
  embed   --sample S.json --out V.npz [--model DIR]
          Load and encode exactly as server.py does (trust_remote_code, truncate_dim 1024, prompt_name
          "document" for stored text, "query" for queries), under whichever interpreter runs this.
  compare A.npz B.npz [--min 0.9999]
          Per-item cosine, A vs B. Exit 1 if any item is below --min.
"""
import argparse, datetime, hashlib, json, os, sqlite3, sys

# Defaults are the release layout's variables (docs/LAYOUT.md); a site passes its own paths.
MODEL = os.environ.get("MEMORY_EMBEDDING_MODEL", "/opt/memory/models/voyage-4-nano")
TRUNCATE_DIM = 1024  # server.py EMBEDDING_TRUNCATE_DIM


def sample(a):
    db = sqlite3.connect(f"file:{a.store}/chroma.sqlite3?mode=ro", uri=True)
    rows = db.execute("""
        select e.embedding_id, c.name, d.string_value, t.string_value
        from embeddings e
        join segments s on e.segment_id = s.id
        join collections c on s.collection = c.id
        join embedding_metadata d on d.id = e.id and d.key = 'chroma:document'
        left join embedding_metadata t on t.id = e.id and t.key = 'stored_at'
        where c.name in ('memories', 'news')""").fetchall()
    pick = {"memories": [], "news": []}
    for eid, coll, doc, ts in sorted(rows, key=lambda r: hashlib.sha256(r[0].encode()).hexdigest()):
        if not doc or not ts or ts >= a.before:
            continue
        want = a.n_mem if coll == "memories" else a.n_news
        if len(pick[coll]) < want:
            pick[coll].append({"id": eid, "collection": coll, "text": doc})
    queries = []
    if a.canaries and os.path.exists(a.canaries):
        import yaml
        canaries = yaml.safe_load(open(a.canaries)) or []
        queries = [c["query"] for c in (canaries.get("canaries") if isinstance(canaries, dict) else canaries) if c.get("query")]
    if not queries:  # nothing to embed with the query prompt otherwise, and that path changes too
        queries = [d["text"][:200] for d in (pick["memories"] + pick["news"])[:5]]
    docs = pick["memories"] + pick["news"]
    json.dump({"before": a.before, "docs": docs, "queries": queries}, open(a.out, "w"))
    if not docs:
        raise SystemExit(f"sample: no memories or news stored before {a.before} in {a.store}")
    lens = sorted(len(d["text"]) for d in docs)
    print(f"sample: {len(pick['memories'])} memories + {len(pick['news'])} news (chars min {lens[0]}, "
          f"median {lens[len(lens) // 2]}, max {lens[-1]}), {len(queries)} canary queries -> {a.out}")


def embed(a):
    import numpy as np, torch, transformers, sentence_transformers
    from sentence_transformers import SentenceTransformer
    s = json.load(open(a.sample))
    m = SentenceTransformer(a.model, trust_remote_code=True, truncate_dim=TRUNCATE_DIM)
    d = m.encode([x["text"] for x in s["docs"]], prompt_name="document", show_progress_bar=False)
    q = m.encode(s["queries"], prompt_name="query", show_progress_bar=False)
    ver = f"torch {torch.__version__}, transformers {transformers.__version__}, sentence-transformers {sentence_transformers.__version__}"
    np.savez(a.out, docs=d, queries=q, ids=np.array([x["id"] for x in s["docs"]]), versions=np.array(ver))
    print(f"embed: {d.shape[0]} docs + {q.shape[0]} queries, dim {d.shape[1]}, dtype {d.dtype} [{ver}] -> {a.out}")


def compare(a):
    import numpy as np
    A, B = np.load(a.a), np.load(a.b)
    assert (A["ids"] == B["ids"]).all(), "the two files embed different samples"
    print(f"A: {A['versions']}\nB: {B['versions']}")
    bad = 0
    for part in ("docs", "queries"):
        x, y = A[part].astype("float64"), B[part].astype("float64")
        cos = (x * y).sum(1) / (np.linalg.norm(x, axis=1) * np.linalg.norm(y, axis=1))
        n = int((cos < a.min).sum()); bad += n
        print(f"{part:8} n={len(cos):3}  cosine min {cos.min():.9f}  mean {cos.mean():.9f}  "
              f"max |diff| {np.abs(x - y).max():.2e}  below {a.min}: {n}")
    print("GATE PASS" if bad == 0 else f"GATE FAIL: {bad} items below {a.min}")
    return 0 if bad == 0 else 1


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("sample")
    s.add_argument("--out", required=True)
    s.add_argument("--store", default=os.environ.get("MEMORY_DATA_DIR", "/var/lib/memory/chromadb"))
    s.add_argument("--canaries", default=None)
    s.add_argument("--before", default=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))
    s.add_argument("--n-mem", type=int, default=40)
    s.add_argument("--n-news", type=int, default=10)
    e = sp.add_parser("embed")
    e.add_argument("--sample", required=True)
    e.add_argument("--out", required=True)
    e.add_argument("--model", default=MODEL)
    c = sp.add_parser("compare")
    c.add_argument("a"); c.add_argument("b")
    c.add_argument("--min", type=float, default=0.9999)
    a = p.parse_args()
    return {"sample": sample, "embed": embed, "compare": compare}[a.cmd](a) or 0


if __name__ == "__main__":
    sys.exit(main())
