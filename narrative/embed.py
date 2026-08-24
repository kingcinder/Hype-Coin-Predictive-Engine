from __future__ import annotations

import hashlib
import re

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _shingles(text: str, size: int = 4) -> list[int]:
    joined = " ".join(_TOKEN_RE.findall(text.lower()))
    if len(joined) < size:
        return [int(hashlib.md5(joined.encode("utf-8")).hexdigest(), 16)]
    return [
        int(hashlib.md5(joined[index : index + size].encode("utf-8")).hexdigest(), 16)
        for index in range(len(joined) - size + 1)
    ]


class MinhashEmbedder:
    """Hand-built, offline, deterministic text signatures.

    No model download, no network, no license cost: character 4-gram shingles
    hashed into a fixed-size minhash signature. Jaccard similarity is estimated by
    the fraction of equal signature positions. Good enough to group shill-campaign
    texts that share vocabulary; an optional sentence-transformers embedder can
    replace it later without changing the clusterer's interface.
    """

    def __init__(self, sig_size: int = 64, seed: int = 42) -> None:
        self.sig_size = sig_size
        self.seeds = [seed + index for index in range(sig_size)]

    def embed(self, text: str) -> tuple[int, ...]:
        shingles = _shingles(text)
        if not shingles:
            shingles = [0]
        signature = []
        for seed in self.seeds:
            signature.append(min(hash_value ^ seed for hash_value in shingles))
        return tuple(signature)

    def similarity(self, left: tuple[int, ...], right: tuple[int, ...]) -> float:
        if len(left) != len(right) or not left:
            return 0.0
        matches = sum(1 for a, b in zip(left, right, strict=True) if a == b)
        return matches / len(left)
