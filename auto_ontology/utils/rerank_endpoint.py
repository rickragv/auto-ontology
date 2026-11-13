# SPDX-License-Identifier: Apache-2.0

"""``RERANK_ENDPOINT``, checked when it is set.

Every caller of :func:`~auto_ontology.utils.rerank.rerank_hits` treats a failed
request as "no ranking" and quietly takes its unranked order, so a value that
can never work, such as ``none`` or a URL without its scheme, would fail on
every call and never be reported. It is refused here instead, when
:mod:`auto_ontology.utils.rerank` is imported, which stops the app at startup.
"""

from __future__ import annotations

import os

from auto_ontology.utils.model_config import resolve

RERANK_ENDPOINT_ENV = "RERANK_ENDPOINT"

#: The value that switches rerank off (any case).
DISABLED = "disabled"


def resolve_rerank_endpoint() -> str:
    """The endpoint :func:`~auto_ontology.utils.rerank.get_rerank_kwargs` passes on.

    Unset or empty, it resolves exactly as upstream does: the shared
    ``DEFAULT_MODELS_ENDPOINT``, then the built-in default, neither of them
    checked here. A set value, surrounding whitespace ignored, must be an
    ``http://`` or ``https://`` URL (scheme in any case), returned without the
    whitespace, or ``disabled`` in any case, returned as ``disabled``. Raises
    ``ValueError`` for anything else, including a value of only whitespace.
    """
    raw = os.environ.get(RERANK_ENDPOINT_ENV, "")
    if not raw:
        return resolve("RERANK", "ENDPOINT")
    value = raw.strip()
    if value.lower() == DISABLED:
        return DISABLED
    if value.lower().startswith(("http://", "https://")):
        return value
    raise ValueError(
        f"{RERANK_ENDPOINT_ENV} must be an http:// or https:// URL or "
        f"'{DISABLED}' (any case), or unset for the default endpoint; got {raw!r}."
    )
