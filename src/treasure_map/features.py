"""Image normalization and persistent SIFT features."""
import hashlib
import os
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

from .models import FeatureSet, TreasureMapError

_CACHE_VERSION = "v4"
_NORMALIZED_WIDTH = 828
_TOP_RATIO = 0.24
_BOTTOM_RATIO = 0.82
_BANNER_HEIGHT = 55


class FeatureExtractor:
    def __init__(self):
        self._sift = cv2.SIFT_create(nfeatures=2500, contrastThreshold=0.02)

    def extract(self, path: Path, cache_dir: Path | None = None) -> FeatureSet:
        source = Path(path)
        digest = self._sha256(source)
        cache_path = cache_dir / f"{_CACHE_VERSION}-{digest}.npz" if cache_dir else None
        if cache_path and cache_path.exists():
            cached = self._load_cache(cache_path)
            if cached is not None:
                points, descriptors, shape = cached
                return FeatureSet(source, digest, points, descriptors, shape)
        try:
            with Image.open(source) as opened:
                image = ImageOps.exif_transpose(opened).convert("RGB")
                image.load()
                pixels = np.asarray(image)
            points, descriptors, shape = self._compute(pixels)
        except (OSError, ValueError, SyntaxError, cv2.error, Image.DecompressionBombError) as exc:
            raise TreasureMapError(f"无法读取藏宝图图片：{source}") from exc
        if cache_path:
            self._save_cache(cache_path, points, descriptors, shape)
        return FeatureSet(source, digest, points, descriptors, shape)

    def _compute(self, pixels: np.ndarray) -> tuple[np.ndarray, np.ndarray, tuple[int, int]]:
        height, width = pixels.shape[:2]
        top = int(height * _TOP_RATIO)
        bottom = int(height * _BOTTOM_RATIO)
        if width < 2 or bottom - top < 2:
            raise ValueError("image is too small")
        roi = pixels[top:bottom]
        scale = _NORMALIZED_WIDTH / width
        resized = cv2.resize(
            roi,
            (_NORMALIZED_WIDTH, max(2, round(roi.shape[0] * scale))),
            interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC,
        )
        gray = cv2.cvtColor(resized, cv2.COLOR_RGB2GRAY)
        feature_mask = self._feature_mask(resized)
        keypoints, descriptors = self._sift.detectAndCompute(gray, feature_mask)
        keypoints, descriptors = self._filter_covered_keypoints(keypoints, descriptors, resized)
        points = np.array([point.pt for point in keypoints], dtype=np.float32).reshape(-1, 2)
        if descriptors is None:
            descriptors = np.empty((0, 128), dtype=np.float32)
        return points, np.asarray(descriptors, dtype=np.float32), gray.shape

    @staticmethod
    def _filter_covered_keypoints(keypoints, descriptors, pixels: np.ndarray):
        if descriptors is None:
            return keypoints, descriptors
        hsv = cv2.cvtColor(pixels, cv2.COLOR_RGB2HSV)
        brown = cv2.inRange(hsv, (5, 100, 140), (15, 210, 235))
        purple = cv2.inRange(hsv, (120, 80, 150), (160, 255, 255))
        covered = ((brown | purple) > 0).astype(np.uint8)
        integral = cv2.integral(covered)
        kept = []
        for index, keypoint in enumerate(keypoints):
            x, y = (round(value) for value in keypoint.pt)
            x0, x1 = max(0, x - 10), min(pixels.shape[1], x + 11)
            y0, y1 = max(0, y - 10), min(pixels.shape[0], y + 11)
            total = integral[y1, x1] - integral[y0, x1] - integral[y1, x0] + integral[y0, x0]
            if total / ((x1 - x0) * (y1 - y0)) < 0.45:
                kept.append(index)
        return [keypoints[index] for index in kept], descriptors[kept]

    @staticmethod
    def _feature_mask(pixels: np.ndarray) -> np.ndarray:
        hsv = cv2.cvtColor(pixels, cv2.COLOR_RGB2HSV)
        brown_tiles = cv2.inRange(hsv, (5, 100, 140), (15, 210, 235))
        purple_tiles = cv2.inRange(hsv, (120, 80, 150), (160, 255, 255))
        tiles = cv2.bitwise_or(brown_tiles, purple_tiles)
        contours = cv2.findContours(tiles, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]
        feature_mask = np.full(pixels.shape[:2], 255, dtype=np.uint8)
        feature_mask[:_BANNER_HEIGHT] = 0
        for contour in contours:
            x, y, width, height = cv2.boundingRect(contour)
            if not (40 <= width <= 95 and 40 <= height <= 110 and 0.65 <= width / height <= 1.4):
                continue
            if cv2.contourArea(contour) / (width * height) < 0.65:
                continue
            cv2.rectangle(feature_mask, (x - 3, y - 3), (x + width + 2, y + height + 2), 0, -1)
        return feature_mask

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise TreasureMapError(f"无法读取藏宝图图片：{path}") from exc
        return digest.hexdigest()

    @staticmethod
    def _load_cache(path: Path):
        try:
            with np.load(path, allow_pickle=False) as data:
                points = np.asarray(data["points"], dtype=np.float32)
                descriptors = np.asarray(data["descriptors"], dtype=np.float32)
                shape = tuple(int(value) for value in data["shape"])
            if points.ndim != 2 or points.shape[1] != 2 or descriptors.ndim != 2:
                return None
            if descriptors.shape[1] != 128 or len(points) != len(descriptors) or len(shape) != 2:
                return None
            return points, descriptors, shape
        except (OSError, ValueError, KeyError, EOFError):
            return None

    @staticmethod
    def _save_cache(path: Path, points: np.ndarray, descriptors: np.ndarray, shape: tuple[int, int]) -> None:
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
                temporary = Path(handle.name)
                np.savez_compressed(handle, points=points, descriptors=descriptors, shape=np.array(shape))
            os.replace(temporary, path)
        except OSError as exc:
            raise TreasureMapError(f"无法写入藏宝图特征缓存：{path}") from exc
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
