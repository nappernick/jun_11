#!/usr/bin/env python3
"""Smoke test: confirm each open-weights reranker LOADS and SCORES on THIS env
(transformers 5.x, torch 2.12, MPS) before any harness or GPU spend is built
around it. Surfaces the trust_remote_code / license / v4->v5 gates NOW.

For each model family it prints: load time, max context length, and the raw
score on a clearly-RELEVANT vs clearly-IRRELEVANT (query, doc) pair. A working
reranker must score relevant >> irrelevant. Each family is isolated in try/except
so one gated/broken model never blocks the others.
"""
import sys
import time
import traceback

import torch

DEVICE = "mps" if torch.backends.mps.is_available() else ("cuda" if torch.cuda.is_available() else "cpu")

QUERY = "Can I upgrade to business class and what does Amazon reimburse?"
REL = ("Am I allowed to upgrade my fare class? Yes. Amazon reimburses only the "
       "Lowest Logical Fare in economy class. You must pay the difference between "
       "the policy-approved fare and the upgraded class out of pocket.")
IRREL = ("Parental leave is available to all regular full-time employees. Eligible "
         "employees may take paid leave following the birth or adoption of a child.")


def banner(name):
    print(f"\n{'='*70}\n{name}  (device={DEVICE})\n{'='*70}")


def smoke_ettin(size="1b"):
    banner(f"ETTIN cross-encoder/ettin-reranker-{size}-v1  [logit]")
    from sentence_transformers import CrossEncoder
    t0 = time.time()
    m = CrossEncoder(f"cross-encoder/ettin-reranker-{size}-v1", device=DEVICE)
    load = time.time() - t0
    maxlen = getattr(m, "max_length", None) or getattr(m.tokenizer, "model_max_length", None)
    t1 = time.time()
    scores = m.predict([(QUERY, REL), (QUERY, IRREL)])
    infer = time.time() - t1
    print(f"  loaded in {load:.1f}s  max_context={maxlen}")
    print(f"  rel={float(scores[0]):+.4f}   irrel={float(scores[1]):+.4f}   "
          f"separation={float(scores[0]) - float(scores[1]):+.4f}")
    print(f"  infer(2 pairs)={infer*1000:.0f}ms")
    return {"model": f"ettin-{size}", "max_context": maxlen, "rel": float(scores[0]),
            "irrel": float(scores[1]), "load_s": load}


def smoke_qwen3(size="0.6B"):
    banner(f"QWEN3 Qwen/Qwen3-Reranker-{size}  [yes/no margin]")
    from transformers import AutoModelForCausalLM, AutoTokenizer
    repo = f"Qwen/Qwen3-Reranker-{size}"
    t0 = time.time()
    tok = AutoTokenizer.from_pretrained(repo, padding_side="left")
    model = AutoModelForCausalLM.from_pretrained(repo, torch_dtype=torch.float16).to(DEVICE).eval()
    load = time.time() - t0
    yes_id = tok.convert_tokens_to_ids("yes")
    no_id = tok.convert_tokens_to_ids("no")
    maxlen = getattr(tok, "model_max_length", None)
    prefix = ('<|im_start|>system\nJudge whether the Document meets the requirements '
              'based on the Query and the Instruct provided. Note that the answer can '
              'only be "yes" or "no".<|im_end|>\n<|im_start|>user\n')
    suffix = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
    instr = "Given a user question, retrieve FAQ passages that answer it"

    def margin(doc):
        text = (prefix + f"<Instruct>: {instr}\n<Query>: {QUERY}\n<Document>: {doc}" + suffix)
        inp = tok(text, return_tensors="pt", truncation=True, max_length=8192).to(DEVICE)
        with torch.no_grad():
            logits = model(**inp).logits[0, -1, :]
        return float(logits[yes_id] - logits[no_id])

    t1 = time.time()
    rel, irrel = margin(REL), margin(IRREL)
    infer = time.time() - t1
    print(f"  loaded in {load:.1f}s  max_context={maxlen}  yes_id={yes_id} no_id={no_id}")
    print(f"  rel={rel:+.4f}   irrel={irrel:+.4f}   separation={rel - irrel:+.4f}")
    print(f"  infer(2 pairs)={infer*1000:.0f}ms")
    return {"model": f"qwen3-{size}", "max_context": maxlen, "rel": rel, "irrel": irrel, "load_s": load}


def smoke_nemotron():
    banner("NEMOTRON nvidia/llama-nemotron-rerank-1b-v2  [logit, trust_remote_code]")
    from transformers import AutoModel, AutoModelForSequenceClassification, AutoTokenizer
    repo = "nvidia/llama-nemotron-rerank-1b-v2"
    t0 = time.time()
    tok = AutoTokenizer.from_pretrained(repo, trust_remote_code=True)
    try:
        model = AutoModelForSequenceClassification.from_pretrained(
            repo, trust_remote_code=True, torch_dtype=torch.float16).to(DEVICE).eval()
    except Exception:
        model = AutoModel.from_pretrained(
            repo, trust_remote_code=True, torch_dtype=torch.float16).to(DEVICE).eval()
    load = time.time() - t0
    maxlen = getattr(tok, "model_max_length", None)

    def score(doc):
        text = f"question: {QUERY} passage: {doc}"
        inp = tok(text, return_tensors="pt", truncation=True, max_length=8192).to(DEVICE)
        with torch.no_grad():
            out = model(**inp)
        logits = getattr(out, "logits", None)
        if logits is None:
            logits = out[0]
        return float(logits.reshape(-1)[0])

    t1 = time.time()
    rel, irrel = score(REL), score(IRREL)
    infer = time.time() - t1
    print(f"  loaded in {load:.1f}s  max_context={maxlen}")
    print(f"  rel={rel:+.4f}   irrel={irrel:+.4f}   separation={rel - irrel:+.4f}")
    print(f"  infer(2 pairs)={infer*1000:.0f}ms")
    return {"model": "nemotron-1b-v2", "max_context": maxlen, "rel": rel, "irrel": irrel, "load_s": load}


SMOKES = {
    "ettin": lambda: smoke_ettin("1b"),
    "qwen3": lambda: smoke_qwen3("0.6B"),
    "nemotron": smoke_nemotron,
}

if __name__ == "__main__":
    which = sys.argv[1:] or list(SMOKES)
    results, gates = {}, {}
    for name in which:
        try:
            results[name] = SMOKES[name]()
        except Exception as e:  # noqa: BLE001 -- isolate per-model gate
            gates[name] = f"{type(e).__name__}: {e}"
            print(f"\n!!! {name} GATED/FAILED: {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\n{'='*70}\nSMOKE SUMMARY\n{'='*70}")
    for name in which:
        if name in results:
            r = results[name]
            ok = "OK " if r["rel"] > r["irrel"] else "BAD-ORDER"
            print(f"  {ok} {name:10s} sep={r['rel']-r['irrel']:+.3f} max_ctx={r['max_context']} load={r['load_s']:.0f}s")
        else:
            print(f"  GATE {name:10s} {gates[name][:90]}")
