"""Endpoint-agnostic audit of the bootstrap-history sha chain against the live bootstrap collection.

Read-only. Runnable by any uid with read access to BOOTSTRAP_HISTORY_PATH and chroma.sqlite3.
For every <entry>/<nnnn>-<sha16>.{md,json}:
  self   : sha256(md bytes) == sidecar.content_sha256
  link   : sidecar.superseded_by_sha256 == next record's content_sha256, or the live document's sha for the last record
  meta   : live metadata stored_sha256 == sha256(live document)   (empty field reported separately)
A record whose superseded_by matches neither the next record nor the live document is classified by whether
another record in the same entry shares its pre-image content_sha256:
  ABORTED-WRITE : no sibling shares the pre-image. History landed, upsert did not (cancellation between
                  _bootstrap_history_write and upsert). Nothing was lost.
  LOST-UPDATE   : a sibling shares the pre-image and landed. Two writers passed the optimistic check across the
                  embed await, both wrote history, the second upsert won, and the first caller was told it succeeded
                  with a sha for a post-image that is not live. Its edit is gone (review R1d, exercised 2026-09-09).
                  Definite only when the record carries landed_at (its upsert returned; deployed server writes `<base>.md.landed_at`). Without the marker the same
                  signature is also produced by an aborted call whose pre-image another writer later superseded; the
                  audit reports that as LOST-UPDATE? and a human decides. This is what the post-upsert landed_at
                  marker is for: it separates "returned success" from "died", which the chain alone cannot.
A record whose superseded_by matches the live document is LANDED even if the sidecar has no landed_at
(the marker is written after upsert returns; a cancellation in that window loses the marker, not the write).
Intent replay (review S11, 2026-09-09): when a sidecar carries `patches` ([{old_text,new_text}], as applied)
(deployed sidecar key `patch_set`; `patches` also honoured) the audit re-applies them to the recorded before-image (the .md) and checks the result hashes to
sidecar.superseded_by_sha256 and, if present, to sidecar.expected_after_sha256 (the caller's claim). All three
from disk, nothing typed at audit time. INTENT-MISMATCH = the server's record of what it applied does not
reproduce what it says it produced. Sidecars without `patches` (bootstrap_update writes) skip this rule.
First use 2026-09-09: found a v6 orphan on a shared state entry from the
cancelled 2026-09-08 retry. Exit status 1 on any defect so it can gate a test run."""
import os, sys, json, hashlib, sqlite3, glob
# LAYOUT: both derive from MEMORY_DATA_DIR (bootstrap history lives next to the store).
_DATA=os.environ.get("MEMORY_DATA_DIR","/var/lib/memory/chromadb")
H=os.path.join(os.path.dirname(os.path.abspath(_DATA)),"bootstrap-history")
DB=os.path.join(_DATA,"chroma.sqlite3")
sha=lambda t: hashlib.sha256(t.encode()).hexdigest()
live={}
c=sqlite3.connect(f"file:{DB}?mode=ro",uri=True)
for eid,k,v in c.execute("""select e.embedding_id, m.key, m.string_value from embedding_metadata m join embeddings e on e.id=m.id
  join segments s on s.id=e.segment_id join collections col on col.id=s.collection where col.name='bootstrap'
  and m.key in ('chroma:document','stored_sha256')"""):
    live.setdefault(eid,{})[k]=v
print(f"live bootstrap entries: {len(live)}")
defects=[]; aborted=[]; landed_marked = set()
lost=[]
cov={'records':0,'intent_evaluated':0,'no_patch_set':0,'landed_at_present':0,'landed_at_absent':0}
# Every entry's history dir, not just mem-*: state-<agent> and lane-tagged identity rows arrived with
# the 2026-09-15 flock-state-split and went unaudited until 2026-09-24.
for d in sorted(x for x in glob.glob(os.path.join(H,'*')) if os.path.isdir(x) and os.path.basename(x) != 'manifest-hashes'):
    eid=os.path.basename(d); recs=[]
    for j in sorted(glob.glob(d+'/*.json')):
        s=json.load(open(j)); recs.append((os.path.basename(j)[:-5], s, sha(open(j[:-5]+'.md').read())))
    live_sha = sha(live[eid]['chroma:document']) if eid in live else None
    meta_ok = (eid in live) and live[eid].get('stored_sha256','')==live_sha
    print(f"{eid}: {len(recs)} records; live meta == sha(doc): {meta_ok if eid in live else 'n/a (not live)'}"
          + ("" if eid not in live or live[eid].get('stored_sha256') else "  [meta stored_sha256 EMPTY]"))
    pre_count={}
    for _,s0,_ in recs: pre_count[s0['content_sha256']]=pre_count.get(s0['content_sha256'],0)+1
    landed_shas={s0['content_sha256'] for _,s0,_ in recs} | ({live_sha} if live_sha else set())
    for i,(base,s,docsha) in enumerate(recs):
        # landed_at: the deployed server writes it as a sibling file `<base>.md.landed_at` (S3 marker); a sidecar
        # field of the same name is also honoured. Marker on record N means the write that superseded N returned.
        if not s.get("landed_at"):
            mk=os.path.join(d,base+'.md.landed_at')
            if os.path.exists(mk):
                try: s["landed_at"]=open(mk).read().strip() or "present"
                except Exception: s["landed_at"]="present"
        cov['records']+=1
        cov['landed_at_present' if s.get('landed_at') else 'landed_at_absent']+=1
        ok_self = docsha==s['content_sha256']
        nxt = recs[i+1][1]['content_sha256'] if i+1<len(recs) else None
        sb = s['superseded_by_sha256']
        if sb==nxt: link="link:next"
        elif sb==live_sha: link="link:live" + ("" if s.get("landed_at") or i+1<len(recs) else " (no landed_at marker)")
        elif sb in landed_shas: link="link:landed-later"   # superseded_by is a later record's pre-image (out-of-order index)
        elif pre_count[s['content_sha256']]>1:
            # A sibling shares this pre-image. If this record's upsert returned (landed_at present) the caller was
            # told it succeeded and its post-image is not live: LOST-UPDATE. Without a marker the store cannot tell a
            # lost update from an aborted call whose pre-image someone else later superseded: report as ambiguous.
            if s.get("landed_at"): link="LOST-UPDATE -> "+sb[:8]; lost.append((eid,base,sb)); landed_marked.add(base)
            else: link="LOST-UPDATE? (no landed_at; or aborted-then-superseded) -> "+sb[:8]; lost.append((eid,base,sb))
        else: link="ABORTED-WRITE -> "+sb[:8]; aborted.append((eid,base,sb))
        if not ok_self: defects.append((eid,base,"SELF-MISMATCH"))
        intent=""
        pset = s.get("patch_set") if isinstance(s.get("patch_set"),list) else s.get("patches")   # deployed key is patch_set
        cov['intent_evaluated' if isinstance(pset,list) else 'no_patch_set']+=1
        if isinstance(pset,list):
            doc=open(os.path.join(d,base+'.md')).read(); ok_apply=True
            for pch in pset:
                o,n=pch.get("old_text",""),pch.get("new_text","")
                if doc.count(o)!=1: ok_apply=False; break
                doc=doc.replace(o,n,1)
            got=sha(doc) if ok_apply else None
            claim=s.get("expected_after_sha256")
            if got==sb and (claim in (None,"",got)): intent="  intent:ok"
            else: intent="  INTENT-MISMATCH"; defects.append((eid,base,"INTENT-MISMATCH"))
        print(f"    {base}  {'self:ok' if ok_self else 'SELF-MISMATCH'}  {link}{intent}  {s.get('author','?')}  {s.get('superseded_at','')[:19]}")
    if eid in live and not meta_ok and live[eid].get('stored_sha256'): defects.append((eid,'live','META-SHA-MISMATCH'))
nohist=[e for e in live if not os.path.isdir(os.path.join(H,e))]
empty=[e for e in live if not live[e].get('stored_sha256')]
print(f"\ncoverage: {cov['records']} records; intent rule evaluated on {cov['intent_evaluated']} (no patch_set: {cov['no_patch_set']}); "
      f"landed_at marker present {cov['landed_at_present']} / absent {cov['landed_at_absent']}  <- a rule that evaluated nothing reports nothing wrong")
print(f"live entries with no history dir: {len(nohist)} {nohist}")
print(f"live entries with empty metadata stored_sha256: {len(empty)} {empty}")
print(f"aborted writes (orphan records, nothing lost): {len(aborted)} {[(e,b) for e,b,_ in aborted]}")
# Two different findings were being printed under one heading that asserts destruction.
# With a landed_at marker the caller WAS told it succeeded and its post-image is not live:
# that is a lost update. Without one the store cannot tell a lost update from an aborted
# call whose pre-image someone else later superseded, and saying "destroyed" of an
# ambiguous record is the tool overclaiming -- which is the thing this tool exists to catch.
confirmed = [(e, b, sb) for e, b, sb in lost if b in landed_marked]
ambiguous = [(e, b, sb) for e, b, sb in lost if b not in landed_marked]
print(f"LOST UPDATES (landed_at present -- the caller was told it succeeded): "
      f"{len(confirmed)} {[(e,b) for e,b,_ in confirmed]}")
print(f"AMBIGUOUS (no landed_at -- lost update OR aborted-then-superseded, the store cannot say): "
      f"{len(ambiguous)} {[(e,b) for e,b,_ in ambiguous]}")
print(f"chain defects (self/meta mismatch): {len(defects)} {defects}")
# Ambiguity is reported, not failed on: an audit that cries wolf at every record the store
# cannot classify stops being read, and 22 of 35 records here carry no patch_set at all.
sys.exit(1 if (defects or aborted or confirmed) else 0)
