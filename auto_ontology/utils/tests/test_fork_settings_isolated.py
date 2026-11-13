# SPDX-License-Identifier: Apache-2.0

"""Each test starts from upstream behaviour, whatever the environment holds.

``auto_ontology/conftest.py`` removes the fork's settings before every test.
These pass trivially in a clean shell; they matter when the settings are
exported or loaded from a ``.env``, e.g.::

    EMBED_DIMENSIONS=7 EMBED_BATCH_SIZE=1 EMBED_MAX_CONCURRENT=1 \\
    EMBED_EXTRA_PARAMS=none REASONING_CLIENT=openai NON_REASONING_CLIENT=nvidia \\
    DEFAULT_MODELS_CLIENT=openai LLM_REASONING_EFFORT=none \\
    uv run pytest auto_ontology/utils/tests/test_fork_settings_isolated.py
"""

from __future__ import annotations

import os

from auto_ontology.utils import llm_invoke
from auto_ontology.utils.embedding import EmbedParams
from auto_ontology.vdb.dimensions import configured_embed_dimensions


def test_embedding_settings_start_unset() -> None:
    params = EmbedParams(model_name="m", embed_invoke_url="http://embed.invalid/v1")

    assert params.inference_batch_size == 32
    assert params.nim_http_max_concurrent == 32
    assert params.nvidia_fields is True
    assert configured_embed_dimensions() is None


def test_chat_settings_start_unset() -> None:
    """Including the client kinds ``llm_invoke`` resolved when it was imported."""
    assert llm_invoke._CLIENT_KIND == "auto"
    assert llm_invoke._NON_REASONING_CLIENT_KIND == "auto"
    assert "LLM_REASONING_EFFORT" not in os.environ
