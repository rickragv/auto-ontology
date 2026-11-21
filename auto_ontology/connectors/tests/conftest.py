# SPDX-License-Identifier: Apache-2.0

"""Leave the live database tests out of a run unless asked for.

They need a running emulator or a real project. CI fails when more than two
tests are skipped, so they are not collected at all -- rather than skipped --
unless asked for:

* Spanner emulator: ``AUTO_ONTOLOGY_LIVE_SPANNER=1``.
* BigQuery: ``AUTO_ONTOLOGY_LIVE_BIGQUERY_PROJECT=<project>`` (real BigQuery
  with Application Default Credentials; add
  ``AUTO_ONTOLOGY_LIVE_BIGQUERY_EMULATOR=<host>:<port>`` for an emulator).
"""

from __future__ import annotations

import os

collect_ignore: list[str] = []
if os.environ.get("AUTO_ONTOLOGY_LIVE_SPANNER") != "1":
    collect_ignore.append("test_spanner_emulator.py")
if not os.environ.get("AUTO_ONTOLOGY_LIVE_BIGQUERY_PROJECT"):
    collect_ignore.append("test_bigquery_live.py")
