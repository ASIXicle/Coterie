"""score.py: apply trained heads to the memory server's stored embedding vectors.

A head answers one typed question (a choice among fixed options) with a linear layer over an item's
embedding vector. It decides only when it is confident enough AND the vector looks like its training
data; otherwise it returns decided=False and the caller escalates (to a person, an agent, a larger
model). train.py writes heads; this module reads them. It imports numpy and the standard library
only, so the memory server can load it without the training stack.

Files, per head, in the heads directory:
  <name>.head.json   question, classes, weights, threshold, collections, embedder fingerprint, metrics
  <name>.ref.npy     the training vectors (unit length, float16), for the out-of-distribution check
"""
import hashlib
import json
import math
import os

import numpy as np

FORMAT = "heads/1"
_NOT_MODEL = {"LICENSE", "LICENSE.txt", "NOTICE", "NOTICE.txt", "README.md"}


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(model_dir, truncate_dim, prompt_name, text_prefix=""):
    """Everything a stored vector depends on. Two vectors are comparable only if these are equal.

    model_sha256 covers every file of the model directory (weights, tokenizer, code, configs), so a
    re-download of the same revision matches and anything else does not. text_prefix is any text the
    storing code put in front of the document before embedding (a prompt name alone does not show it).
    """
    lines = []
    for root, dirs, files in os.walk(model_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if name.startswith(".") or name in _NOT_MODEL:
                continue
            path = os.path.join(root, name)
            lines.append(f"{os.path.relpath(path, model_dir)}\t{_sha256_file(path)}\n")
    return {
        "model_sha256": hashlib.sha256("".join(sorted(lines)).encode()).hexdigest(),
        "truncate_dim": int(truncate_dim),
        "prompt_name": str(prompt_name),
        "text_prefix": str(text_prefix),
    }


def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    return v / np.where(n == 0, 1, n)


class Head:
    def __init__(self, path):
        raw = open(path, "rb").read()
        spec = json.loads(raw)
        if spec.get("format") != FORMAT:
            raise ValueError(f"not a {FORMAT} head")
        self.name = spec["name"]
        self.classes = list(spec["classes"])
        self.coef = np.asarray(spec["coef"], dtype=np.float32)
        self.intercept = np.asarray(spec["intercept"], dtype=np.float32)
        self.threshold = spec["threshold"]  # None: the head never decides
        self.miss_class = spec.get("miss_class")  # an answer other than this one needs clear_threshold
        self.clear_threshold = spec.get("clear_threshold")  # None with a miss_class: other answers never decide
        self.collections = list(spec.get("collections", []))  # empty: every collection
        self.embedder = spec["embedder"]
        self.version = hashlib.sha256(raw).hexdigest()[:8]
        ood = spec["ood"]
        ref_path = os.path.join(os.path.dirname(os.path.abspath(path)), ood["ref"])
        if _sha256_file(ref_path) != ood["ref_sha256"]:
            raise ValueError(f"{ood['ref']} does not match its sha256 in the head file")
        self.ref = _unit(np.load(ref_path, allow_pickle=False))
        self.min_similarity = float(ood["min_similarity"])
        self._validate()

    def _validate(self):
        """A head that would score nonsense is refused at load, with a reason."""
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("name must be a non-empty string")
        if len(self.classes) < 2 or len(set(self.classes)) != len(self.classes) or \
                not all(isinstance(c, str) and c for c in self.classes):
            raise ValueError("classes must be two or more distinct non-empty strings")
        rows = 1 if len(self.classes) == 2 else len(self.classes)
        if self.coef.ndim != 2 or self.coef.shape[0] != rows:
            raise ValueError(f"coef must be {rows} row(s) of weights for {len(self.classes)} classes")
        if self.intercept.shape != (rows,):
            raise ValueError(f"intercept must hold {rows} value(s)")
        if self.ref.ndim != 2 or self.ref.shape[0] == 0 or self.ref.shape[1] != self.coef.shape[1]:
            raise ValueError("reference vectors must be a non-empty matrix as wide as the weights")
        if not (np.isfinite(self.coef).all() and np.isfinite(self.intercept).all() and np.isfinite(self.ref).all()):
            raise ValueError("weights and reference vectors must be finite")
        t = self.threshold
        if t is not None and (isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 < t <= 1):
            raise ValueError("threshold must be null or a number in (0, 1]")
        if self.miss_class is not None and self.miss_class not in self.classes:
            raise ValueError("miss_class must be one of the classes")
        c = self.clear_threshold
        if c is not None and (isinstance(c, bool) or not isinstance(c, (int, float)) or not 0 < c <= 1):
            raise ValueError("clear_threshold must be null or a number in (0, 1]")
        if not (math.isfinite(self.min_similarity) and -1 <= self.min_similarity <= 1):
            raise ValueError("min_similarity must be a finite number in [-1, 1]")
        if not all(isinstance(c, str) and c for c in self.collections):
            raise ValueError("collections must be a list of collection names")
        if not isinstance(self.embedder, dict):
            raise ValueError("embedder must be a fingerprint object")

    def probabilities(self, vector):
        z = self.coef @ _unit(vector) + self.intercept
        if len(self.classes) == 2:  # one row of weights: the probability of classes[1]
            p1 = 1.0 / (1.0 + np.exp(-z[0]))
            return np.array([1.0 - p1, p1])
        e = np.exp(z - z.max())
        return e / e.sum()

    def score(self, vector):
        """{answer, p, decided, similarity, v}. answer is the most likely class even when undecided."""
        p = self.probabilities(vector)
        i = int(p.argmax())
        similarity = float((self.ref @ _unit(vector)).max())
        decided = (self.threshold is not None and float(p[i]) >= self.threshold
                   and similarity >= self.min_similarity)
        if decided and self.miss_class is not None and self.classes[i] != self.miss_class:
            decided = self.clear_threshold is not None and float(p[i]) >= self.clear_threshold
        return {"answer": self.classes[i], "p": float(p[i]), "decided": bool(decided),
                "similarity": similarity, "v": self.version}


def load_heads(heads_dir, embedder_fingerprint, log=print):
    """Every usable head in heads_dir, by name. A head trained on other vectors is skipped, never applied."""
    heads = {}
    if not heads_dir or not os.path.isdir(heads_dir):
        return heads
    for name in sorted(os.listdir(heads_dir)):
        if not name.endswith(".head.json"):
            continue
        path = os.path.join(heads_dir, name)
        try:
            head = Head(path)
        except Exception as e:  # a head file must never stop the server from starting
            log(f"heads: skipped {name}: {type(e).__name__}: {e}")
            continue
        if head.embedder != embedder_fingerprint:
            log(f"heads: skipped {name}: trained on a different embedder fingerprint")
            continue
        if head.name in heads:
            log(f"heads: skipped {name}: a head named {head.name!r} is already loaded")
            continue
        heads[head.name] = head
    return heads
