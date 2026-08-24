from __future__ import annotations

from enum import StrEnum


class ChainSlug(StrEnum):
    SOLANA = "solana"
    BASE = "base"
    ETHEREUM = "ethereum"


class RiskBand(StrEnum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    ORANGE = "ORANGE"
    RED = "RED"
    BLACK = "BLACK"


class SourceTier(StrEnum):
    CHAIN = "chain"
    VENUE = "venue"
    OFFICIAL = "official"
    EXPLORER = "explorer"
    SCANNER = "scanner"
    PUBLIC_METADATA = "public_metadata"
    SOCIAL = "social"
    ENRICHMENT = "enrichment"
    BROAD_CRAWL = "broad_crawl"
    RUMOR = "rumor"


class AssetStatus(StrEnum):
    RAW_DISCOVERY = "raw_discovery"
    EARLY_WATCH = "early_watch"
    VALIDATED_SPECULATIVE = "validated_speculative"
    LIQUID_MOMENTUM = "liquid_momentum"
    TOO_MATURE = "too_mature"
    HARD_REJECT = "hard_reject"
    UNKNOWN = "unknown"


class AlertState(StrEnum):
    OPEN = "open"
    ACKED = "acked"
    CLOSED = "closed"


class AlertType(StrEnum):
    NEW_INTERESTING_TOKEN = "new_interesting_token"
    RED_RISK_HYPE = "red_risk_hype"
    LIQUIDITY_DETERIORATION = "liquidity_deterioration"
    THESIS_INVALIDATED = "thesis_invalidated"
    OFFICIAL_CATALYST = "official_catalyst"
    IGNITION_DETECTED = "ignition_detected"
    LIQUIDITY_WITHDRAWAL = "liquidity_withdrawal_warning"
    SYNDICATE_RECIDIVISM = "syndicate_recidivism"
    PRELAUNCH_CANDIDATE = "prelaunch_candidate"
    UPCOMING_CATALYST = "upcoming_catalyst"
    LIFECYCLE_TRANSITION = "lifecycle_transition"


class IgnitionEventType(StrEnum):
    FIRST_LIQUIDITY_INJECTION = "first_liquidity_injection"
    SNIPER_BURST = "sniper_burst"
    LIQUIDITY_WITHDRAWAL = "liquidity_withdrawal"


class WalletRole(StrEnum):
    DEPLOYER = "deployer"
    FUNDER = "funder"
    SNIPER = "sniper"
    LP_REMOVER = "lp_remover"
    UNKNOWN = "unknown"


class LifecyclePhase(StrEnum):
    SEEDING = "seeding"
    IGNITION = "ignition"
    PARABOLIC = "parabolic"
    SATURATION = "saturation"
    COLLAPSE = "collapse"
    DEAD = "dead"
    RUGGED = "rugged"
    SURVIVOR = "survivor"
