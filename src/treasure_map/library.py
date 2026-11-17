"""Treasure map lookup, conservative decisions, and deduplicated storage."""
import os
import shutil
import tempfile
from pathlib import Path

from .features import FeatureExtractor
from .matcher import SiftComparator
from .models import AddResult, FeatureSet, MatchResult, Similarity, Thresholds, TreasureMapError

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


class TreasureMapLibrary:
    def __init__(
        self,
        library_dir: Path,
        cache_dir: Path | None = None,
        *,
        extractor=None,
        comparator=None,
        thresholds: Thresholds | None = None,
    ):
        self.library_dir = Path(library_dir)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.extractor = extractor or FeatureExtractor()
        self.comparator = comparator or SiftComparator()
        self.thresholds = thresholds or Thresholds()

    def match(self, image_path: Path) -> MatchResult:
        query = self.extractor.extract(Path(image_path), self.cache_dir)
        ranked = self._rank(query)
        eligible = [candidate for candidate in ranked if self._passes_match(candidate[1])]
        if not eligible:
            best, second = self._top_two(ranked)
            return MatchResult("not_found", None, best, second)
        best_path, best = eligible[0]
        second = eligible[1][1] if len(eligible) > 1 else None
        if best.score < self.thresholds.strong_score and second is not None:
            relative_gap = (best.score - second.score) / best.score
            if relative_gap < self.thresholds.ambiguity_ratio:
                return MatchResult("not_found", None, best, second)
        return MatchResult("found", best_path.source, best, second)

    def add(self, image_path: Path) -> AddResult:
        source = Path(image_path)
        if source.suffix.lower() not in _IMAGE_SUFFIXES:
            raise TreasureMapError(f"不支持的藏宝图图片格式：{source.suffix or '无扩展名'}")
        query = self.extractor.extract(source, self.cache_dir)
        references = list(self._reference_features())
        for reference in references:
            if reference.sha256 == query.sha256:
                return AddResult("duplicate", reference.source)
        ranked = self._rank(query, references)
        duplicates = [candidate for candidate in ranked if self._passes_duplicate(candidate[1])]
        if duplicates:
            reference, similarity = duplicates[0]
            return AddResult("duplicate", reference.source, similarity)
        target = self.library_dir / "added" / f"{query.sha256}{source.suffix.lower()}"
        self._copy_atomic(source, target)
        return AddResult("added", target)

    def _reference_features(self):
        if not self.library_dir.exists():
            return
        for path in sorted(self.library_dir.rglob("*")):
            if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES:
                yield self.extractor.extract(path, self.cache_dir)

    def _rank(self, query: FeatureSet, references=None):
        references = self._reference_features() if references is None else references
        ranked = [(reference, self.comparator.compare(query, reference)) for reference in references]
        return sorted(
            ranked,
            key=lambda candidate: (candidate[1].score, candidate[1].inliers, candidate[1].good_matches),
            reverse=True,
        )

    def _passes_match(self, similarity: Similarity) -> bool:
        return (
            similarity.inliers >= self.thresholds.min_inliers
            and similarity.coverage >= self.thresholds.min_coverage
            and similarity.inlier_ratio >= self.thresholds.min_inlier_ratio
            and similarity.score >= self.thresholds.min_score
        )

    def _passes_duplicate(self, similarity: Similarity) -> bool:
        return (
            similarity.inliers >= self.thresholds.duplicate_min_inliers
            and similarity.coverage >= self.thresholds.duplicate_min_coverage
            and similarity.inlier_ratio >= self.thresholds.duplicate_min_inlier_ratio
            and similarity.score >= self.thresholds.duplicate_min_score
        )

    @staticmethod
    def _top_two(ranked):
        if not ranked:
            return None, None
        best = ranked[0][1]
        second = ranked[1][1] if len(ranked) > 1 else None
        return best, second

    @staticmethod
    def _copy_atomic(source: Path, target: Path) -> None:
        temporary = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.", delete=False) as handle:
                temporary = Path(handle.name)
                with source.open("rb") as input_file:
                    shutil.copyfileobj(input_file, handle)
            os.replace(temporary, target)
        except OSError as exc:
            raise TreasureMapError(f"无法写入藏宝图图片：{target}") from exc
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
