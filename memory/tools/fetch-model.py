#!/usr/bin/env python3
"""fetch-model.py — download the memory server's embedding model at a pinned revision, verified.

    fetch-model.py --into /opt/memory/models/voyage-4-nano

Source: https://huggingface.co/voyageai/voyage-4-nano (Apache-2.0) at revision REVISION. Every file is
checked against the size and sha256 recorded below (the list was cross-checked 2026-09-25 against
upstream's own records: the LFS sha256 of the weights, the git blob id of every other file). The
download goes to <dir>.partial and is renamed into place only after every file verifies, so a failed
or tampered fetch never leaves a half-installed model. Then <dir>/.modeling.sha256 is written: the
memory unit's ExecStartPre checks it, because the model loads the custom code in
modeling_qwen3_bidirectional.py (trust_remote_code). The model directory is the last stdout line.

If <dir> already exists it is verified, not replaced: exit 0 if every file matches, 1 otherwise.
Equivalence receipt (2026-09-25): this revision, loaded as the server loads it, reproduced a live
stored vector bit-for-bit (cosine 1.000000000, max |diff| 0).

Never pass fix_mistral_regex=True when loading this model. transformers 4.57.3-4.99 warns that the
tokenizer has "an incorrect regex pattern" for every model saved by those versions, whatever the
tokenizer is. This model's tokenizer is not Mistral's, and the warning is a false positive. The flag swaps in Mistral's
pre-tokenizer: it changed token ids in 18 of 240 real samples (2026-10-01), so the vectors change
while the model fingerprint still matches, and they no longer match the store or the heads.
Stdlib only. Exit 0 = the model is in place and verified; anything else = it is not.
"""
import argparse
import hashlib
import os
import shutil
import sys
import urllib.request

REPO = "voyageai/voyage-4-nano"
REVISION = "67fabc9bef010dabc5f6024aa1b1b6b93410426f"
BASE = f"https://huggingface.co/{REPO}/resolve/{REVISION}"
MODELING = "modeling_qwen3_bidirectional.py"
FILES = {  # path: (size, sha256)
    "config.json": (950, "9a7c0235bf7da6706f14bebbe7ec94c2c6483c59317c5c1c18f7d144282ac9c9"),
    "config_sentence_transformers.json": (378, "4b0f3b9dea0ccb018b705084289d09ad1c4a02ddd277bf2e80bde9887539ef08"),
    "modules.json": (349, "84e40c8e006c9b1d6c122e02cba9b02458120b5fb0c87b746c41e0207cf642cf"),
    "sentence_bert_config.json": (59, "ae8658c7cf91db1a3ceee800af0f9bda2c7ad60a88b5c2b4d6d5cb0a4394c9c1"),
    "1_Pooling/config.json": (313, "2bc529695125f68de57d1fd347e3d2920b993bc635c5f4f09a45e130c102a989"),
    "tokenizer.json": (7031645, "c0382117ea329cdf097041132f6d735924b697924d6f6fc3945713e96ce87539"),
    "tokenizer_config.json": (7228, "58c4abbc36eeccd8b3c8453f262225e8f2803855c88790760b618f4cd9e43be9"),
    "vocab.json": (2776833, "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910"),
    "merges.txt": (1671839, "599bab54075088774b1733fde865d5bd747cbcc7a547c5bc12610e874e26f5e3"),
    MODELING: (3087, "f4340347ce92a764e6ce6dc76fb4e412a6ab876dd33e61a9ed4de7595c697728"),
    "LICENSE.txt": (11343, "6128fb091df68c86035ebf80fde97956e9126c47ad90c19631f34cd98afbfe6c"),
    "NOTICE.txt": (797, "f30cf8ced2476cbd4ddea495156f33deaf1c2245c5bed0ea46dab895c3aa59d5"),
    "model.safetensors": (692919112, "3dae0c63c81dcab79ac213af331940a1bf2b8a53ec8646be878552890291ad30"),
}


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(root):
    """[] if every pinned file is present with its size and hash, else the problems."""
    problems = []
    for rel, (size, digest) in FILES.items():
        p = os.path.join(root, rel)
        if not os.path.isfile(p):
            problems.append(f"missing {rel}")
        elif os.path.getsize(p) != size:
            problems.append(f"size {rel}: {os.path.getsize(p)} != {size}")
        elif sha256_of(p) != digest:
            problems.append(f"sha256 {rel} differs from the pin")
    return problems


def fetch(rel, dest, base, timeout):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with urllib.request.urlopen(f"{base}/{rel}", timeout=timeout) as r, open(dest, "wb") as out:
        shutil.copyfileobj(r, out, 1 << 20)


def write_pin(root):
    # sha256sum -c format, absolute path: what memory.service's ExecStartPre checks.
    with open(os.path.join(root, ".modeling.sha256"), "w") as f:
        f.write(f"{FILES[MODELING][1]}  {os.path.join(root, MODELING)}\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--into", required=True, help="the model directory to create (e.g. /opt/memory/models/voyage-4-nano)")
    ap.add_argument("--timeout", type=int, default=600, help="per-file download timeout, seconds")
    args = ap.parse_args(argv)
    root = os.path.abspath(args.into)
    if os.path.exists(root):
        problems = verify(root)
        if problems:
            print(f"fetch-model: {root} exists and does not match the pin; not replacing it:", file=sys.stderr)
            for p in problems:
                print(f"  {p}", file=sys.stderr)
            return 1
        write_pin(root)
        print(f"fetch-model: {root} already holds {REPO}@{REVISION[:12]}, verified", file=sys.stderr)
        print(root)
        return 0
    partial = root + ".partial"
    shutil.rmtree(partial, ignore_errors=True)
    try:
        for rel, (size, _) in FILES.items():
            print(f"fetch-model: {rel} ({size} bytes)", file=sys.stderr)
            fetch(rel, os.path.join(partial, rel), BASE, args.timeout)
        problems = verify(partial)
        if problems:
            raise RuntimeError("; ".join(problems))
        os.makedirs(os.path.dirname(root), exist_ok=True)
        os.rename(partial, root)
    except Exception as e:
        shutil.rmtree(partial, ignore_errors=True)
        print(f"fetch-model: FAILED, nothing installed: {e.__class__.__name__}: {e}", file=sys.stderr)
        return 1
    write_pin(root)
    print(f"fetch-model: {REPO}@{REVISION[:12]} verified and installed", file=sys.stderr)
    print(root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
