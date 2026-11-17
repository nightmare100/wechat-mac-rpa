"""Descriptor matching and geometric verification."""
import cv2
import numpy as np

from .models import FeatureSet, Similarity

_RATIO = 0.8
_GRID_SIZE = 8
_MAX_HORIZONTAL_SHIFT = 25
_MIN_VERTICAL_SHIFT = -150
_MAX_VERTICAL_SHIFT = 100
_TRANSLATION_TOLERANCE = 6
_MAX_INLIERS_PER_REGION = 5


class SiftComparator:
    def __init__(self):
        self._matcher = cv2.BFMatcher(cv2.NORM_L2)

    def compare(self, query: FeatureSet, reference: FeatureSet) -> Similarity:
        if len(query.descriptors) < 2 or len(reference.descriptors) < 2:
            return self._empty(0)
        forward = self._good_matches(query.descriptors, reference.descriptors)
        backward = self._good_matches(reference.descriptors, query.descriptors)
        reverse = dict(backward)
        mutual = [(query_index, reference_index) for query_index, reference_index in forward
                  if reverse.get(reference_index) == query_index]
        good_count = len(mutual)
        if good_count < 3:
            return self._empty(good_count)
        source = np.float32([query.points[q] for q, _ in mutual])
        target = np.float32([reference.points[r] for _, r in mutual])
        deltas = target - source
        plausible = (
            (np.abs(deltas[:, 0]) <= _MAX_HORIZONTAL_SHIFT)
            & (deltas[:, 1] >= _MIN_VERTICAL_SHIFT)
            & (deltas[:, 1] <= _MAX_VERTICAL_SHIFT)
        )
        best_mask = None
        best_count = 0
        for delta in deltas[plausible]:
            mask = plausible & (np.linalg.norm(deltas - delta, axis=1) <= _TRANSLATION_TOLERANCE)
            count = int(np.count_nonzero(mask))
            if count > best_count:
                best_mask = mask
                best_count = count
        if best_mask is None:
            return self._empty(good_count)
        inliers = best_count
        inlier_ratio = inliers / good_count
        inlier_points = source[best_mask]
        height, width = query.shape
        columns = np.clip((inlier_points[:, 0] * _GRID_SIZE / width).astype(int), 0, _GRID_SIZE - 1)
        rows = np.clip((inlier_points[:, 1] * _GRID_SIZE / height).astype(int), 0, _GRID_SIZE - 1)
        cells = rows * _GRID_SIZE + columns
        coverage = len(np.unique(cells))
        _, counts = np.unique(cells, return_counts=True)
        spatial_inliers = int(np.minimum(counts, _MAX_INLIERS_PER_REGION).sum())
        return Similarity(good_count, inliers, inlier_ratio, coverage, spatial_inliers * inlier_ratio)

    def _good_matches(self, first: np.ndarray, second: np.ndarray) -> list[tuple[int, int]]:
        matches = self._matcher.knnMatch(first, second, k=2)
        return [(match.queryIdx, match.trainIdx) for pair in matches if len(pair) == 2
                for match, competitor in [pair] if match.distance < _RATIO * competitor.distance]

    @staticmethod
    def _empty(good_matches: int) -> Similarity:
        return Similarity(good_matches, 0, 0.0, 0, 0.0)
