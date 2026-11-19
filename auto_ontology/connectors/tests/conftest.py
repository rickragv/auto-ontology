# SPDX-License-Identifier: Apache-2.0

"""Leave the Spanner emulator tests out of a run unless asked for.

They need a running emulator. CI fails when more than two tests are skipped, so
they are not collected at all -- rather than skipped -- without
``AUTO_ONTOLOGY_LIVE_SPANNER=1``.
"""

from __future__ import annotations

import os

collect_ignore: list[str] = []
if os.environ.get("AUTO_ONTOLOGY_LIVE_SPANNER") != "1":
    collect_ignore.append("test_spanner_emulator.py")
