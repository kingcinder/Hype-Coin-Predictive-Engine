# Evidence required to demonstrate predictive edge

Do not claim an edge until all of these conditions hold for the predeclared
target and decision horizon:

- At least 30 eligible point-in-time samples in each reported metric cell,
  across enough independent assets and time periods for useful uncertainty
  intervals.
- A strictly later, out-of-sample evaluation window that was not used to choose
  features, thresholds, or calibration.
- Confidence intervals for the held-out effect beat an explicit baseline:
  base rate for classification, always-GREEN for RiskBand, or random ranking
  for ranking metrics, as applicable.
- The feature-leakage audit has no unresolved source-availability rows capable
  of explaining the measured association. Historical values that cannot be
  replayed from point-in-time sources remain unverified and cannot support the
  claim.

Passing the 30-sample minimum only makes a cell benchmarkable. It does not by
itself demonstrate utility. Forecast, hazard, and ensemble outputs remain
unvalidated until their held-out evidence meets all conditions above.
