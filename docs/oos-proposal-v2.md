1. P1 is retrospective and exploratory only; no confirmatory claim.
2. P2 is prospective only.
3. Scoring code and config are immutable during P2: each prediction record stores code_sha and config_sha256; any change starts a new P2.
4. Prediction records are append-only (prediction_records).
5. Calibration is frozen at P2 start (save the band→confidence map to reports/calibration-snapshot-<UTC date>.json and put its name in calibration_snapshot_id); no refit on evaluation rows.
6. Labels are timestamp-bounded (label_version = v2_bounded).
7. Evaluation is asset-aware (bootstrap over assets).
8. Minimum sample before any verdict: 300 resolved assets AND 100 BLACK predictions.
9. Observation period: starts at the first frozen prediction record, runs at least 28 days, no early stopping.
10. No recalibration or threshold change on evaluation rows.
11. Verdict is inconclusive if sample minimums are unmet or any CI overlaps its baseline; inconclusive means no deployment.
12. Deployment needs a separate human authorization: the human writes APPROVED: <date> <initials> in docs/deployment-authorization.md; no code ever reads that file.
13. Red-team coverage: write the heading ## Red-team attacks (22) and amendments (6) and under it exactly one line: TODO(human): paste the red-team list here. The coder must not invent entries.

## Red-team attacks (22) and amendments (6)

TODO(human): paste the red-team list here. The coder must not invent entries.
