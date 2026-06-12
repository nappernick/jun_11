#!/usr/bin/env python3
"""cohere_adapters.py — commercial baselines, same score_pairs interface as models.py.

  Cohere35Reranker  : Bedrock cohere.rerank-v3-5:0 (alpha acct, on-demand, available now).
  CohereV4Reranker  : SageMaker cohere-rerank4-{pro,fast}-sandbox (nick-caia acct);
                      requires the endpoint to be InService (deployed in the AWS phase).

Both return relevanceScore in [0,1] -> kind 'unit' (norm == raw via squash clamp).
Cohere is listwise at the API but pointwise-equivalent for ranking; we request the
full pool with numberOfResults = len(docs) and reassemble doc-order scores.
"""
from __future__ import annotations

_COHERE_MAX_DOC_CHARS = 32000  # Bedrock/Cohere per-doc hard limit


class Cohere35Reranker:
    kind = "unit"
    max_context = 4096  # tokens (Cohere Rerank 3.5 doc context)

    def __init__(self, profile="alpha", region="us-west-2"):
        import boto3
        self.id = "cohere-3.5"
        self.device = "bedrock"
        self.model_arn = f"arn:aws:bedrock:{region}::foundation-model/cohere.rerank-v3-5:0"
        self._client = boto3.Session(profile_name=profile, region_name=region).client(
            "bedrock-agent-runtime")

    def score_pairs(self, query, docs):
        if not docs:
            return []
        texts = [((d or " ")[:_COHERE_MAX_DOC_CHARS]) or " " for d in docs]
        sources = [{"type": "INLINE",
                    "inlineDocumentSource": {"type": "TEXT", "textDocument": {"text": t}}}
                   for t in texts]
        resp = self._client.rerank(
            queries=[{"type": "TEXT", "textQuery": {"text": query}}],
            sources=sources,
            rerankingConfiguration={
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "numberOfResults": len(texts),
                    "modelConfiguration": {"modelArn": self.model_arn}}})
        # resp results are best-first with index into docs; reassemble doc-order scores
        out = [0.0] * len(docs)
        for r in resp["results"]:
            out[r["index"]] = float(r["relevanceScore"])
        return out


class CohereV4Reranker:
    kind = "unit"
    max_context = 4096  # placeholder; v4 supports longer — confirm from package card

    def __init__(self, variant="fast", profile="nick-caia", region="us-east-1"):
        import boto3
        import cohere
        self.id = f"cohere-v4-{variant}"
        self.device = "sagemaker"
        self.endpoint = f"cohere-rerank4-{variant}-sandbox"
        c = boto3.Session(profile_name=profile, region_name=region) \
            .get_credentials().get_frozen_credentials()
        kwargs = {"aws_region": region, "aws_access_key": c.access_key,
                  "aws_secret_key": c.secret_key}
        if c.token:
            kwargs["aws_session_token"] = c.token
        self._co = cohere.SagemakerClient(**kwargs)

    def score_pairs(self, query, docs):
        if not docs:
            return []
        texts = [((d or " ")[:_COHERE_MAX_DOC_CHARS]) or " " for d in docs]
        resp = self._co.rerank(model=self.endpoint, query=query, documents=texts,
                               top_n=len(texts))
        out = [0.0] * len(docs)
        for r in resp.results:
            out[r.index] = float(r.relevance_score)
        return out
