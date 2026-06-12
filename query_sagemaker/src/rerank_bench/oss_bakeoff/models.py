#!/usr/bin/env python3
"""models.py — open-weights reranker adapters (the bakeoff SPINE).

Each adapter exposes the same interface so the runner and the SageMaker handler
share ONE scoring code path (no drift between local quality and GPU latency):

    r = load("qwen3-0.6b")
    raws = r.score_pairs(query, docs)     # raw scores, doc order preserved
    r.kind         -> 'logit' | 'margin'  (how to squash to [0,1])
    r.max_context  -> int tokens

Scoring math is exactly what the smoke test validated (relevant >> irrelevant
on all three). torch / transformers / sentence-transformers are imported lazily.
"""
from __future__ import annotations

import time

_QWEN_PREFIX = ('<|im_start|>system\nJudge whether the Document meets the requirements '
                'based on the Query and the Instruct provided. Note that the answer can '
                'only be "yes" or "no".<|im_end|>\n<|im_start|>user\n')
_QWEN_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
_QWEN_INSTRUCT = "Given a user question, retrieve FAQ passages that answer it"


def _pick_device(device):
    import torch
    if device:
        return device
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def _from_pretrained(cls, repo, dtype, **kw):
    """Load a HF model handling the transformers version skew between local
    (5.x, `dtype=`) and the SageMaker DLC (4.51.3, only `torch_dtype=`). Tries the
    new kwarg first, falls back to the old one — so the SAME spine runs on either,
    removing the #1 GPU-deploy risk (the DLC's transformers predates `dtype=`)."""
    import torch
    td = getattr(torch, dtype) if isinstance(dtype, str) else dtype
    try:
        return cls.from_pretrained(repo, dtype=td, **kw)
    except TypeError:
        return cls.from_pretrained(repo, torch_dtype=td, **kw)


class EttinReranker:
    """sentence-transformers CrossEncoder -> raw seq-classification logit."""
    kind = "logit"

    # Truncation cap (tokens). The model supports 7999, but ModernBERT global
    # attention over a near-8K sequence x batch explodes the MPS buffer; the FAQ
    # corpus is almost entirely <900 tokens, so 2048 truncates only rare long
    # outliers (which Cohere truncates too). max_context reports the real limit.
    _TRUNC = 2048

    def __init__(self, size="1b", device=None, dtype="float16"):
        from sentence_transformers import CrossEncoder
        self.id = f"ettin-{size}"
        self.device = _pick_device(device)
        self._m = CrossEncoder(f"cross-encoder/ettin-reranker-{size}-v1",
                               device=self.device, max_length=self._TRUNC)
        self.max_context = getattr(self._m, "max_seq_length", None) or 7999

    def score_pairs(self, query, docs):
        if not docs:
            return []
        scores = self._m.predict([(query, d) for d in docs],
                                 show_progress_bar=False, batch_size=8)
        return [float(s) for s in scores]


class Qwen3Reranker:
    """Qwen3-Reranker causal-LM yes/no head -> raw margin = logit(yes) - logit(no)."""
    kind = "margin"

    def __init__(self, size="0.6B", device=None, dtype="float16"):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        self.id = f"qwen3-{size.lower()}"
        self.device = _pick_device(device)
        repo = f"Qwen/Qwen3-Reranker-{size}"
        self._tok = AutoTokenizer.from_pretrained(repo, padding_side="left")
        self._model = _from_pretrained(AutoModelForCausalLM, repo, dtype).to(self.device).eval()
        self._yes = self._tok.convert_tokens_to_ids("yes")
        self._no = self._tok.convert_tokens_to_ids("no")
        self.max_context = int(getattr(self._tok, "model_max_length", 131072))

    def score_pairs(self, query, docs):
        import torch
        # ONE doc per forward (batch=1, no padding) — the EXACT path the smoke test
        # validated. A batched, left-padded variant tripped a Qwen3 forward error on
        # the DLC (modeling_qwen3.py:505); single-sequence is the canonical, proven
        # usage and is plenty fast on the A10G. Cap at 4096 tokens (= Cohere; the
        # real 131072 window is reported via max_context, not exercised per doc here).
        cap = min(self.max_context, 4096)
        out = []
        for doc in docs:
            text = (_QWEN_PREFIX + f"<Instruct>: {_QWEN_INSTRUCT}\n<Query>: {query}\n<Document>: {doc}"
                    + _QWEN_SUFFIX)
            inp = self._tok(text, return_tensors="pt", truncation=True,
                            max_length=cap).to(self.device)
            with torch.no_grad():
                logits = self._model(**inp).logits[0, -1, :]
            out.append(float(logits[self._yes] - logits[self._no]))
        return out


class NemotronReranker:
    """nvidia/llama-nemotron-rerank-1b-v2 (trust_remote_code) -> raw relevance logit."""
    kind = "logit"

    def __init__(self, device=None, dtype="float16"):
        import torch
        from transformers import AutoModel, AutoModelForSequenceClassification, AutoTokenizer
        self.id = "nemotron-1b-v2"
        self.device = _pick_device(device)
        repo = "nvidia/llama-nemotron-rerank-1b-v2"
        self._tok = AutoTokenizer.from_pretrained(repo, trust_remote_code=True)
        try:
            self._model = _from_pretrained(
                AutoModelForSequenceClassification, repo, dtype, trust_remote_code=True
            ).to(self.device).eval()
        except Exception:
            self._model = _from_pretrained(
                AutoModel, repo, dtype, trust_remote_code=True
            ).to(self.device).eval()
        self.max_context = int(getattr(self._tok, "model_max_length", 4096))

    def score_pairs(self, query, docs):
        import torch
        out = []
        for doc in docs:
            text = f"question: {query} passage: {doc}"
            inp = self._tok(text, return_tensors="pt", truncation=True,
                            max_length=self.max_context).to(self.device)
            with torch.no_grad():
                res = self._model(**inp)
            logits = getattr(res, "logits", None)
            if logits is None:
                logits = res[0]
            out.append(float(logits.reshape(-1)[0]))
        return out


def load(model_id, device=None, dtype="float16"):
    if model_id == "ettin-1b":
        return EttinReranker("1b", device, dtype)
    if model_id == "qwen3-0.6b":
        return Qwen3Reranker("0.6B", device, dtype)
    if model_id == "qwen3-4b":
        return Qwen3Reranker("4B", device, dtype)
    if model_id == "nemotron-1b-v2":
        return NemotronReranker(device, dtype)
    raise ValueError(f"unknown model_id: {model_id}")


OSS_MODEL_IDS = ["ettin-1b", "qwen3-0.6b", "qwen3-4b", "nemotron-1b-v2"]


if __name__ == "__main__":
    import sys
    mid = sys.argv[1] if len(sys.argv) > 1 else "ettin-1b"
    q = "can I upgrade to business class and what does Amazon reimburse"
    docs = ["Amazon reimburses only the Lowest Logical Fare in economy; you pay the upgrade.",
            "Parental leave is available to all regular full-time employees."]
    t0 = time.time()
    r = load(mid)
    s = r.score_pairs(q, docs)
    print(f"{r.id} kind={r.kind} ctx={r.max_context} scores={[round(x,3) for x in s]} "
          f"order_ok={s[0] > s[1]} ({time.time()-t0:.1f}s)")
