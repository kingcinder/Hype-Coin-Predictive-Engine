# Project state

Tracks the reviewed fix list on `fix/functional-pass`. Each row records the
finding, the commit, and the evidence behind it.

| Finding | Status | Commits | Evidence |
|---|---|---|---|
| F2: launcher and health probes sent no bearer token to `/health`, so they always got 401 and the GUI never opened | Fixed | `3cbaed9`, `2e7af15` | [verified: live gated engine on the review-branch gate (`origin/review-fixes-20260910`), port 8100. Unauthenticated, wrong-token and correct-token requests return 401, 401, 200. The probe functions from `start-engine.sh` and `packaging/serpent` returned OK for a token from `.env` and from the environment, and FAIL for a wrong or missing token.] |
