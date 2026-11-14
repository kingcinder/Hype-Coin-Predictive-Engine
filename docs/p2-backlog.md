# P2 backlog — do not build yet

Do not start until STATE.md has an EDGE VERDICT of "scores beat baseline on all three" AND the paper scorecard shows beats_naive_baseline with at least 30 closed trades AND the human writes START P2.

- Sniper/MEV-bundle detection from the mempool (`mempool/`).
- EVM honeypot static analysis (hidden mint, blacklist/pausable, proxy, fee-on-transfer; flags block live buys).
- Funding-source and CEX-deposit wallet clustering.
- Forecast calibration (isotonic/Platt) only with ≥100 causally available `v2_bounded` rows; otherwise fallback thresholds and the UI says so.
