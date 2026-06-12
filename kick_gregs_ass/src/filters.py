"""
Builds a Qdrant filter from a user's coordinates (level, location, role, ...).

The non-obvious rule: a fragment tagged "All Job Levels" (etc.) applies to
everyone, so for each requested attribute we accept fragments whose field
contains EITHER the user's value OR the field's wildcard token. Plain equality
would drop exactly the broadly-targeted fragments, which is most of the corpus.
"""
from qdrant_client import models
import config


def build_filter(user_attrs):
    """
    user_attrs: dict like {"system_job-level": "L5", "system_location-type": "Corporate"}
    Only keys present in config.FILTER_FIELDS are applied; unknown keys ignored.
    """
    must = []

    # Status gate (PUBLISHED), mirroring prod surface.
    if config.STATUS_REQUIRED is not None:
        must.append(
            models.FieldCondition(
                key=config.STATUS_FIELD,
                match=models.MatchValue(value=config.STATUS_REQUIRED),
            )
        )

    for field, value in (user_attrs or {}).items():
        if field not in config.FILTER_FIELDS or value is None:
            continue
        wildcard = config.WILDCARD_TOKENS[field]
        # field contains user's value OR contains the "everyone" wildcard
        must.append(
            models.Filter(
                should=[
                    models.FieldCondition(key=field, match=models.MatchValue(value=value)),
                    models.FieldCondition(key=field, match=models.MatchValue(value=wildcard)),
                ]
            )
        )

    return models.Filter(must=must) if must else None
