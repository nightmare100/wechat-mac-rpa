"""Treasure map feature extraction and geometric matching tests."""
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from src.treasure_map.features import FeatureExtractor
from src.treasure_map.matcher import SiftComparator
from src.treasure_map.models import FeatureSet, TreasureMapError


def _scene(seed: int = 7, size: tuple[int, int] = (828, 1792)) -> np.ndarray:
    width, height = size
    rng = np.random.default_rng(seed)
    image = np.full((height, width, 3), (88, 52, 40), dtype=np.uint8)
    positions = [
        (0.10, 0.32), (0.38, 0.31), (0.67, 0.34),
        (0.18, 0.45), (0.51, 0.47), (0.76, 0.51),
        (0.08, 0.60), (0.36, 0.63), (0.64, 0.65),
        (0.20, 0.73), (0.51, 0.72),
    ]
    for index, (x_ratio, y_ratio) in enumerate(positions):
        patch = rng.integers(0, 256, (72, 72, 3), dtype=np.uint8)
        cv2.rectangle(patch, (4, 4), (67, 67), (240, 210, 80), 5)
        cv2.circle(patch, (36, 36), 15 + index % 4, (30, 220, 250), 4)
        cv2.line(patch, (8, 60), (62, 10), (20, 30, 230), 3)
        x = int(width * x_ratio)
        y = int(height * y_ratio)
        image[y:y + 72, x:x + 72] = patch
    return image


def _write(path: Path, image: np.ndarray) -> Path:
    Image.fromarray(image).save(path)
    return path


def _blank_feature(path: Path):
    return FeatureExtractor().extract(_write(path, np.full((1792, 828, 3), 120, dtype=np.uint8)))


def test_dynamic_header_and_footer_do_not_change_features(tmp_path):
    first = _scene()
    second = first.copy()
    second[:400] = 255
    second[1500:] = 0
    extractor = FeatureExtractor()

    one = extractor.extract(_write(tmp_path / "one.png", first))
    two = extractor.extract(_write(tmp_path / "two.png", second))

    np.testing.assert_array_equal(one.points, two.points)
    np.testing.assert_array_equal(one.descriptors, two.descriptors)
    assert one.shape == two.shape


def test_same_layout_survives_scale_theme_and_occlusion(tmp_path):
    reference_image = _scene()
    query_image = np.array(Image.fromarray(reference_image).resize((900, 1950)))
    background = np.all(query_image == (88, 52, 40), axis=2)
    query_image[background] = (55, 25, 90)
    cv2.rectangle(query_image, (80, 840), (260, 1030), (55, 25, 90), -1)
    cv2.rectangle(query_image, (580, 1110), (780, 1280), (55, 25, 90), -1)
    different_image = _scene(seed=99)
    extractor = FeatureExtractor()
    query = extractor.extract(_write(tmp_path / "query.png", query_image))
    reference = extractor.extract(_write(tmp_path / "reference.png", reference_image))
    different = extractor.extract(_write(tmp_path / "different.png", different_image))
    comparator = SiftComparator()

    same_layout = comparator.compare(query, reference)
    different_layout = comparator.compare(query, different)

    assert same_layout.inliers >= 12
    assert same_layout.coverage >= 2
    assert same_layout.score > different_layout.score


def test_blank_images_have_no_similarity(tmp_path):
    blank = _blank_feature(tmp_path / "blank.png")
    other = _blank_feature(tmp_path / "other.png")

    similarity = SiftComparator().compare(blank, other)

    assert similarity.good_matches == 0
    assert similarity.inliers == 0
    assert similarity.inlier_ratio == 0
    assert similarity.coverage == 0
    assert similarity.score == 0


def test_invalid_image_raises_domain_error(tmp_path):
    path = tmp_path / "broken.png"
    path.write_bytes(b"not an image")

    with pytest.raises(TreasureMapError, match="broken.png"):
        FeatureExtractor().extract(path)


def test_covered_tiles_do_not_generate_features(tmp_path):
    image = np.full((1792, 828, 3), (88, 52, 40), dtype=np.uint8)
    for x, y, color in (
        (100, 550, (183, 108, 77)),
        (200, 550, (141, 72, 239)),
        (100, 700, (187, 115, 78)),
        (200, 700, (133, 68, 229)),
    ):
        cv2.rectangle(image, (x, y), (x + 70, y + 80), color, -1)
        cv2.rectangle(image, (x, y), (x + 70, y + 80), (150, 90, 65), 3)
        cv2.line(image, (x + 15, y + 40), (x + 35, y + 15), (190, 120, 90), 2)
        cv2.line(image, (x + 35, y + 15), (x + 55, y + 40), (190, 120, 90), 2)

    features = FeatureExtractor().extract(_write(tmp_path / "covered.png", image))

    assert len(features.points) == 0
    assert len(features.descriptors) == 0


def test_banner_above_board_does_not_generate_features(tmp_path):
    first = _scene()
    second = first.copy()
    second[430:480] = np.random.default_rng(123).integers(0, 256, (50, 828, 3), dtype=np.uint8)
    extractor = FeatureExtractor()

    one = extractor.extract(_write(tmp_path / "one-banner.png", first))
    two = extractor.extract(_write(tmp_path / "two-banner.png", second))

    assert np.all(two.points[:, 1] >= 55)
    similarity = SiftComparator().compare(two, one)
    assert similarity.inliers >= 12
    assert similarity.score > 0


def test_single_region_cannot_dominate_similarity_score(tmp_path, monkeypatch):
    points = np.array([(80 + index % 10, 80 + index // 10) for index in range(30)], dtype=np.float32)
    query = FeatureSet(
        tmp_path / "query.png", "query", points,
        np.zeros((30, 128), dtype=np.float32), (1000, 828),
    )
    reference = FeatureSet(
        tmp_path / "reference.png", "reference", points.copy(),
        np.zeros((30, 128), dtype=np.float32), (1000, 828),
    )
    comparator = SiftComparator()
    pairs = [(index, index) for index in range(30)]
    monkeypatch.setattr(comparator, "_good_matches", lambda first, second: pairs)

    similarity = comparator.compare(query, reference)

    assert similarity.inliers == 30
    assert similarity.coverage == 1
    assert similarity.score == 5


def test_affine_only_alignment_is_rejected(tmp_path, monkeypatch):
    points = np.array([(x, y) for y in (100, 200, 300) for x in (50, 150, 250)], dtype=np.float32)
    query = FeatureSet(
        tmp_path / "query.png", "query", points,
        np.zeros((9, 128), dtype=np.float32), (1000, 828),
    )
    reference = FeatureSet(
        tmp_path / "reference.png", "reference", points * 1.5 + (400, 400),
        np.zeros((9, 128), dtype=np.float32), (1000, 828),
    )
    comparator = SiftComparator()
    pairs = [(index, index) for index in range(9)]
    monkeypatch.setattr(comparator, "_good_matches", lambda first, second: pairs)

    similarity = comparator.compare(query, reference)

    assert similarity.good_matches == 9
    assert similarity.inliers == 0
    assert similarity.score == 0


def test_cache_reuses_content_and_invalidates_changed_file(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    source = _write(tmp_path / "source.png", _scene())
    extractor = FeatureExtractor()
    calls = 0
    original_compute = extractor._compute

    def counted_compute(image):
        nonlocal calls
        calls += 1
        return original_compute(image)

    monkeypatch.setattr(extractor, "_compute", counted_compute)
    first = extractor.extract(source, cache_dir)
    same_content_path = tmp_path / "copy.png"
    same_content_path.write_bytes(source.read_bytes())
    cached = extractor.extract(same_content_path, cache_dir)

    assert calls == 1
    assert cached.source == same_content_path
    np.testing.assert_array_equal(first.descriptors, cached.descriptors)
    assert len(list(cache_dir.glob("*.npz"))) == 1

    _write(source, _scene(seed=21))
    extractor.extract(source, cache_dir)

    assert calls == 2
    assert len(list(cache_dir.glob("*.npz"))) == 2
