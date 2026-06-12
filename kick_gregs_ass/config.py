"""
Central configuration. Everything tunable lives here so Greg never edits logic.

The only thing you must get right when you swap in the real CSV is FILTER_FIELDS /
WILDCARD_TOKENS below — they have to match your actual column names and the literal
"match everyone" tokens your content team uses.
"""

# --- AWS / Bedrock -----------------------------------------------------------
# Stack runs in us-west-2. In us-west-2 Embed v4 is only reachable via the
# cross-region inference profile, NOT the bare model id. The bare id works in
# us-east-1. Rerank 3.5 uses a direct foundation-model ARN.
AWS_REGION = "us-west-2"
EMBED_MODEL_ID = "us.cohere.embed-v4:0"          # us-east-1 alt: "cohere.embed-v4:0"
RERANK_MODEL_ARN = f"arn:aws:bedrock:{AWS_REGION}::foundation-model/cohere.rerank-v3-5:0"

# Embed v4 default output dimension is 1536 (matches prod). We don't pass an
# explicit dimension param so there's nothing to get wrong; default is 1536.
EMBED_DIM = 1536

# --- Qdrant ------------------------------------------------------------------
QDRANT_URL = "http://localhost:6333"
COLLECTION = "faq_corpus"
DENSE_VECTOR = "dense"
SPARSE_VECTOR = "bm25"
BM25_MODEL = "Qdrant/bm25"   # fastembed; tokenizes locally, no API call

# --- Retrieval shape ---------------------------------------------------------
# Two-stage funnel kept on purpose: hybrid retrieves CANDIDATE_N, rerank cuts to
# TOP_K. Don't set CANDIDATE_N to the whole corpus or you erase the candidate-
# generation stage that exists in prod. Both overridable per request.
CANDIDATE_N = 20
TOP_K = 5

# --- CSV schema --------------------------------------------------------------
# Last column is content; everything else is metadata. Multi-value cells are
# pipe-delimited, e.g.  L4|L5|All Job Levels
CONTENT_COLUMN = "text"
ID_COLUMN = "nodeId"
MULTIVALUE_DELIMITER = "|"

# Metadata columns usable as hard filters, mapped to the literal wildcard token
# that means "applies to everyone" for that field. A fragment matches a user's
# value if the field CONTAINS that value OR CONTAINS the wildcard token.
WILDCARD_TOKENS = {
    "system_job-level":      "All Job Levels",
    "system_location-type":  "All Location Types",
    "system_employee-class": "All Employee Classes",
    "system_line-of-business": "All LOBs",
    "geography":             "Global",
}
FILTER_FIELDS = list(WILDCARD_TOKENS.keys())

# Fragments must be PUBLISHED to surface, mirroring prod. Set to None to disable.
# Experiment corpus is entirely DRAFT/UNPUBLISHED (mock data mimicking prod), so
# the gate is disabled here — otherwise nothing would ever surface.
STATUS_FIELD = "status"
STATUS_REQUIRED = None
