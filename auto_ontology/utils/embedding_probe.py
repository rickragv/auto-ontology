# SPDX-License-Identifier: Apache-2.0

"""One-text check of the embedding model's output size before a long job.

Compilation embeds each table's Terms after the LLM work for that table, inside
handlers that log a failed embed and carry on. A model whose output size is not
``EMBED_DIMENSIONS`` would therefore cost a full pass and leave Terms without
vectors. Calling :func:`check_embedding_model_size` first makes that pass fail
before it starts.
"""

from __future__ import annotations

from auto_ontology.utils.embedding import EmbedParams, embed_texts
from auto_ontology.vdb.dimensions import configured_embed_dimensions

_PROBE_TEXT = "embedding size check"


def check_embedding_model_size(params: EmbedParams) -> None:
    """Embed one short text so a model of the wrong size fails here.

    Does nothing, and sends nothing, when ``EMBED_DIMENSIONS`` is unset. When it
    is set, one request is sent; ``embed_texts`` raises
    :class:`~auto_ontology.vdb.dimensions.EmbeddingSizeMismatchError` if the
    returned vector has another length. Any other failure of that request is
    raised unchanged: the job could not have embedded anything either.
    """
    if configured_embed_dimensions() is None:
        return
    embed_texts([_PROBE_TEXT], params, "passage")
