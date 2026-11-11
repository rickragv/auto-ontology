<!-- SPDX-License-Identifier: Apache-2.0 -->

# This fork

This is a personal fork (`rickragv/auto-ontology`) of `NVIDIA/auto-ontology`. The root
`CLAUDE.md` is upstream's maintainer guide: it is unmodified and still applies. This file
adds the fork's own rules. Keep fork guidance here, not in `CLAUDE.md`, so rebases on
upstream stay clean.

## Current work

Make the stack run on any model provider (Ollama, Gemini, and others) through a LiteLLM
gateway. The full plan, with the design decision, the list of changes, the order of work
and the verification matrix, is in @docs/fork/PLAN.md.

- Read `docs/fork/PLAN.md` before changing code.
- Update its "Open questions" section as each one is answered.

## Fork rules

- **Never open a pull request against `NVIDIA/auto-ontology`.** They do not accept
  external PRs. `gh` is set to default to the fork; keep it that way and pass
  `--repo rickragv/auto-ontology` on any `gh pr` command. Never push to `upstream`.
- `main` mirrors upstream. Do not commit on it. Work on `litellm-gateway`; sync with
  `git fetch upstream` then `git rebase upstream/main`.
- Keep the diff small. One concern per commit. Prefer new files over edits to upstream
  files.
- Every new setting is optional. With none set, behaviour must match upstream.
- Commit or push only when the user asks. No `Co-Authored-By` trailers and no mention of
  Claude or AI assistance in commits, PRs or files.
- `.env` holds real secrets. Never print, log or commit its values.
- On Windows keep `core.autocrlf=false` for this clone.

## Working style

- The main session designs, orchestrates and reviews. Hand implementation and test
  writing to subagents (Opus for backend changes, Sonnet for tests and docs), with a
  brief that names the files and the check that proves the work.
- Do not report a change as done until its check has run. State what was run and what
  was not.
- Keep answers short: one line first, then a few short bullets.
- If the app's port is occupied, stop the process holding it and reuse the port.
