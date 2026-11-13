# SPDX-License-Identifier: Apache-2.0

"""Start every test with the fork's model-provider settings unset.

``auto_ontology/tests/conftest.py`` loads the developer's ``.env`` into the
process, and a shell or CI job can export the same names, so a local
``EMBED_BATCH_SIZE=1`` or ``EMBED_EXTRA_PARAMS=none`` would otherwise change
what the upstream tests see. Unset is upstream's behaviour, so that is where
each test starts; a test that needs a setting sets it with ``monkeypatch``.
"""

from __future__ import annotations

import sys

import pytest

# Every setting the fork adds. Each is optional; unset means upstream behaviour.
_FORK_SETTINGS = (
    "EMBED_DIMENSIONS",
    "EMBED_BATCH_SIZE",
    "EMBED_MAX_CONCURRENT",
    "EMBED_EXTRA_PARAMS",
    "REASONING_CLIENT",
    "NON_REASONING_CLIENT",
    "DEFAULT_MODELS_CLIENT",
    "LLM_REASONING_EFFORT",
)


@pytest.fixture(autouse=True)
def _without_fork_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the fork settings, and reset what was resolved from them at import.

    The chat client kinds are read once, when ``llm_invoke`` is imported, which
    may have happened after a ``.env`` was loaded. They are re-resolved here
    from the cleaned environment. ``RERANK_ENDPOINT`` is upstream's setting
    (the fork only adds the ``disabled`` value) and is left alone.
    """
    for name in _FORK_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    llm_invoke = sys.modules.get("auto_ontology.utils.llm_invoke")
    if llm_invoke is not None:
        for attr, prefix in (
            ("_CLIENT_KIND", "REASONING"),
            ("_NON_REASONING_CLIENT_KIND", "NON_REASONING"),
        ):
            monkeypatch.setattr(
                llm_invoke, attr, llm_invoke._resolve_client_kind(prefix)
            )
