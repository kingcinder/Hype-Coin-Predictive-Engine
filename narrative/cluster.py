from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.time import ensure_utc, utc_now
from narrative.embed import MinhashEmbedder
from storage import models
from storage.repository import stable_hash


def cluster_mentions(
    session: Session,
    *,
    decision_ts: datetime,
    embedder: MinhashEmbedder,
    threshold: float,
) -> int:
    """Assign every unclustered mention to a narrative cluster.

    Greedy single-link against existing cluster seeds: a mention joins the most
    similar cluster above ``threshold``, otherwise it seeds a new cluster. Returns
    the number of mentions clustered.
    """
    decision_ts = ensure_utc(decision_ts or utc_now())
    mentions = [
        mention
        for mention in session.scalars(
            select(models.SocialMention).where(models.SocialMention.observed_at <= decision_ts)
        ).all()
        if not (mention.metrics_json or {}).get("cluster_key")
    ]
    if not mentions:
        return 0

    clusters = {
        row.cluster_key: row.seed_topic
        for row in session.scalars(select(models.NarrativeCluster)).all()
    }
    seed_signatures = {key: embedder.embed(topic) for key, topic in clusters.items()}
    clustered = 0
    for mention in mentions:
        signature = embedder.embed(mention.topic or "")
        best_key: str | None = None
        best_similarity = 0.0
        for key, seed_signature in seed_signatures.items():
            similarity = embedder.similarity(signature, seed_signature)
            if similarity > best_similarity:
                best_similarity = similarity
                best_key = key
        if best_key is not None and best_similarity >= threshold:
            cluster_key = best_key
        else:
            cluster_key = "cl:" + stable_hash(mention.topic or "untitled")[:16]
            row = models.NarrativeCluster(
                cluster_key=cluster_key,
                seed_topic=(mention.topic or "untitled")[:256],
                mention_count=0,
                first_seen_at=ensure_utc(mention.ts),
                last_seen_at=ensure_utc(mention.ts),
            )
            session.add(row)
            session.flush()
            seed_signatures[cluster_key] = signature
            clusters[cluster_key] = (mention.topic or "untitled")[:256]
        metrics = dict(mention.metrics_json or {})
        metrics["cluster_key"] = cluster_key
        mention.metrics_json = metrics
        cluster_row = session.scalar(
            select(models.NarrativeCluster).where(
                models.NarrativeCluster.cluster_key == cluster_key
            )
        )
        if cluster_row:
            cluster_row.mention_count = int(cluster_row.mention_count or 0) + 1
            cluster_row.last_seen_at = max(
                ensure_utc(cluster_row.last_seen_at), ensure_utc(mention.ts)
            )
            cluster_row.updated_at = utc_now()
        clustered += 1
    return clustered
