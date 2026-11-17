"""Public values for treasure map matching."""
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np


class TreasureMapError(RuntimeError):
    """An image or treasure map library operation could not be completed."""


@dataclass(frozen=True)
class FeatureSet:
    source: Path
    sha256: str
    points: np.ndarray
    descriptors: np.ndarray
    shape: tuple[int, int]


@dataclass(frozen=True)
class Similarity:
    good_matches: int
    inliers: int
    inlier_ratio: float
    coverage: int
    score: float


@dataclass(frozen=True)
class Thresholds:
    min_inliers: int = 12
    min_coverage: int = 2
    min_inlier_ratio: float = 0.20
    min_score: float = 30.0
    strong_score: float = 60.0
    ambiguity_ratio: float = 0.15
    duplicate_min_inliers: int = 60
    duplicate_min_coverage: int = 12
    duplicate_min_inlier_ratio: float = 0.35
    duplicate_min_score: float = 40.0


@dataclass(frozen=True)
class MatchResult:
    status: Literal["found", "not_found"]
    path: Path | None
    similarity: Similarity | None
    runner_up: Similarity | None = None


@dataclass(frozen=True)
class AddResult:
    status: Literal["added", "duplicate"]
    path: Path
    similarity: Similarity | None = None
