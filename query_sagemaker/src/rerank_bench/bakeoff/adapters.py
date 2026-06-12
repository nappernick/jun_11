"""bakeoff.adapters — open-weights reranker adapters implementing the
contract.Reranker protocol.

Design:
  CrossEncoderReranker wraps any sentence-transformers CrossEncoder checkpoint
  (Ettin family, and other cross-encoder/* seq-classification rerankers). It:
    - scores every (query, candidate) pair with ONE forward pass each
      (cost scales candidates x seq_len — the 4K+ token FAQ docs bite here),
    - treats the model-native score as a raw logit and maps it to [0,1] via
      normalize.squash(raw, "logit") (per-SCORE, never per-query),
    - sorts descending, returns top_k RankedDocs,
    - NEVER throws on a bad/empty doc — scores it floor (-1e4 raw -> ~0 norm),
      per the Reranker contract. Transport/load failures still propagate.

torch + sentence-transformers are imported lazily so importing this module
(e.g. for the harness contract) does not require the heavy deps unless an
actual open-model adapter is instantiated.
"""
from __future__ import annotations

from bakeoff.contract import Candidate, RankedDoc
from bakeoff.normalize import squash

# A raw score floor for docs we cannot score (empty / tokenization failure).
# sigmoid(-1e4) underflows cleanly to 0.0 via normalize.squash.
_BAD_DOC_RAW = -1e4


class CrossEncoderReranker:
    """Adapter for sentence-transformers CrossEncoder rerankers.

    Satisfies contract.Reranker: `.id` property + `.rerank(query, candidates, top_k)`.
    """

    def __init__(
        self,
        model_id: str,
        *,
        adapter_id: str | None = None,
        device: str | None = None,
        max_length: int | None = None,
        batch_size: int = 16,
        score_kind: str = "logit",
    ) -> None:
        self.model_id = model_id
        self._id = adapter_id or model_id
        self.batch_size = batch_size
        self.score_kind = score_kind

        import torch  # lazy
        from sentence_transformers import CrossEncoder  # lazy

        if device is None:
            if torch.backends.mps.is_available():
                device = "mps"
            elif torch.cuda.is_available():
                device = "cuda"
            else:
                device = "cpu"
        self.device = device

        kwargs: dict = {"device": device}
        if max_length is not None:
            kwargs["max_length"] = max_length
        self._model = CrossEncoder(model_id, **kwargs)

    @property
    def id(self) -> str:
        return self._id

    def rerank(self, query: str, candidates: list[Candidate], top_k: int) -> list[RankedDoc]:
        if not candidates:
            return []

        # Partition out un-scorable docs so the model never sees a bad input,
        # but they still receive a deterministic floor score (contract: no throw).
        pairs: list[tuple[str, str]] = []
        scorable_idx: list[int] = []
        raws: list[float] = [_BAD_DOC_RAW] * len(candidates)

        for i, cand in enumerate(candidates):
            text = cand.text if isinstance(cand.text, str) else ""
            if text.strip():
                pairs.append((query, text))
                scorable_idx.append(i)

        if pairs:
            scores = self._model.predict(
                pairs, batch_size=self.batch_size, show_progress_bar=False
            )
            for i, s in zip(scorable_idx, scores):
                raws[i] = float(s)

        order = sorted(range(len(candidates)), key=lambda i: (-raws[i], i))
        ranked: list[RankedDoc] = []
        for rank, i in enumerate(order[: top_k if top_k > 0 else len(order)]):
            ranked.append(
                RankedDoc(
                    node_id=candidates[i].node_id,
                    rank=rank,
                    raw_score=raws[i],
                    norm_score=squash(raws[i], self.score_kind),
                )
            )
        return ranked


def EttinReranker(size: str = "1b", *, device: str | None = None, **kw) -> CrossEncoderReranker:
    """Convenience constructor for the Ettin reranker family (Apache-2.0).

    size: one of '17m', '32m', '1b' (v1 checkpoints). Default '1b'.
    """
    repo = f"cross-encoder/ettin-reranker-{size}-v1"
    return CrossEncoderReranker(repo, adapter_id=f"ettin-reranker-{size}", device=device, **kw)


# Bedrock Cohere Rerank per-document hard limit (chars). Docs over this are
# truncated for scoring (the corpus has one ~42K outlier; real FAQs are <3K).
_COHERE_MAX_DOC_CHARS = 32000


class BedrockCohereReranker:
    """Adapter for Cohere Rerank on Bedrock (cohere.rerank-v3-5:0), the baseline.

    Cohere returns relevance scores already in [0,1] -> score_kind "unit", so
    raw_score == norm_score (squash clamps/identity). Satisfies contract.Reranker.
    Never throws on a bad doc (empty text replaced with a single space and it
    sorts to the bottom); transport errors propagate per contract.
    """

    def __init__(
        self,
        *,
        model_id: str = "cohere.rerank-v3-5:0",
        adapter_id: str = "cohere-rerank-3.5",
        profile: str = "alpha",
        region: str = "us-west-2",
    ) -> None:
        import boto3  # lazy

        self._id = adapter_id
        self.region = region
        self.model_arn = f"arn:aws:bedrock:{region}::foundation-model/{model_id}"
        sess = boto3.Session(profile_name=profile, region_name=region)
        self._client = sess.client("bedrock-agent-runtime")

    @property
    def id(self) -> str:
        return self._id

    def rerank(self, query: str, candidates: list[Candidate], top_k: int) -> list[RankedDoc]:
        if not candidates:
            return []
        texts = [((c.text or " ")[: _COHERE_MAX_DOC_CHARS]) or " " for c in candidates]
        sources = [
            {"type": "INLINE", "inlineDocumentSource": {"type": "TEXT", "textDocument": {"text": t}}}
            for t in texts
        ]
        n = top_k if top_k > 0 else len(candidates)
        resp = self._client.rerank(
            queries=[{"type": "TEXT", "textQuery": {"text": query}}],
            sources=sources,
            rerankingConfiguration={
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "numberOfResults": min(n, len(candidates)),
                    "modelConfiguration": {"modelArn": self.model_arn},
                },
            },
        )
        ranked: list[RankedDoc] = []
        for rank, r in enumerate(resp["results"]):
            i = r["index"]
            score = float(r["relevanceScore"])
            ranked.append(RankedDoc(
                node_id=candidates[i].node_id,
                rank=rank,
                raw_score=score,
                norm_score=squash(score, "unit"),
            ))
        return ranked
