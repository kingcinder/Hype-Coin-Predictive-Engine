"""Data Lake Manager — signal scoring, label densification, webhook alerts.

The data lake manager sits between raw ingestion and the feature/score
pipeline, determining what's actionable versus noise. It:

1. **Signal scoring**: Scores each incoming data point for actionability
   based on novelty, cross-source corroboration, temporal relevance, and
   magnitude of change.

2. **Label densification**: Accelerates forecast training by generating
   labels at regular hourly intervals between sparse market snapshots,
   using linear interpolation for prices.

3. **Webhook alerts**: Dispatches real-time notifications to configurable
   HTTP endpoints, Telegram bots, and Discord webhooks when high-signal
   events are detected.

4. **Confidence dashboard**: Provides API endpoints and GUI views for
   monitoring label generation progress, scoring formula breakdown, and
   feature importance.
"""
