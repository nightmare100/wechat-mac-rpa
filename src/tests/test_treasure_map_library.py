"""Treasure map library decisions and filesystem behavior."""
import hashlib
from pathlib import Path

import numpy as np
import pytest

from src.treasure_map.library import TreasureMapLibrary
from src.treasure_map.models import FeatureSet, Similarity, TreasureMapError

ZERO = Similarity(0, 0, 0.0, 0, 0.0)
STRONG = Similarity(120, 70, 0.8, 18, 70.0)


class FakeExtractor:
    def extract(self, path: Path, cache_dir: Path | None = None) -> FeatureSet:
        source = Path(path)
        return FeatureSet(
            source=source,
            sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            points=np.empty((0, 2), dtype=np.float32),
            descriptors=np.empty((0, 128), dtype=np.float32),
            shape=(100, 100),
        )


class FakeComparator:
    def __init__(self, scores=None):
        self.scores = scores or {}
        self.calls = []

    def compare(self, query: FeatureSet, reference: FeatureSet) -> Similarity:
        self.calls.append((query.source.name, reference.source.name))
        return self.scores.get(reference.source.name, ZERO)


def _image(path: Path, content: bytes | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if content is not None else path.name.encode())
    return path


def _library(tmp_path: Path, scores=None, references=("a.png",)):
    library_dir = tmp_path / "library"
    for name in references:
        _image(library_dir / name)
    comparator = FakeComparator(scores)
    library = TreasureMapLibrary(
        library_dir,
        cache_dir=tmp_path / "cache",
        extractor=FakeExtractor(),
        comparator=comparator,
    )
    query = _image(tmp_path / "query.png", b"query-image")
    return library, comparator, query, library_dir


def test_match_scans_supported_images_recursively_and_returns_best(tmp_path):
    scores = {
        "a.png": Similarity(100, 60, 0.8, 15, 48.0),
        "b.jpeg": STRONG,
        "c.webp": Similarity(80, 50, 0.75, 14, 37.5),
    }
    library, comparator, query, library_dir = _library(
        tmp_path,
        scores,
        references=("nested/a.png", "b.jpeg", "deep/c.webp", "ignored.gif", "notes.txt"),
    )

    result = library.match(query)

    assert result.status == "found"
    assert result.path == library_dir / "b.jpeg"
    assert result.similarity == STRONG
    assert result.runner_up == scores["a.png"]
    compared = {name for _, name in comparator.calls}
    assert compared == {"a.png", "b.jpeg", "c.webp"}


def test_empty_library_returns_not_found(tmp_path):
    library, _, query, _ = _library(tmp_path, references=())

    result = library.match(query)

    assert result.status == "not_found"
    assert result.path is None
    assert result.similarity is None
    assert result.runner_up is None


@pytest.mark.parametrize("similarity", [
    Similarity(50, 11, 0.8, 10, 40.0),
    Similarity(80, 40, 0.5, 1, 40.0),
    Similarity(100, 40, 0.19, 10, 40.0),
    Similarity(50, 20, 0.5, 10, 25.0),
])
def test_match_rejects_each_absolute_threshold(tmp_path, similarity):
    library, _, query, _ = _library(tmp_path, {"a.png": similarity})

    result = library.match(query)

    assert result.status == "not_found"
    assert result.path.name == "a.png"
    assert result.similarity == similarity


def test_match_rejects_close_medium_candidates(tmp_path):
    best = Similarity(80, 40, 0.5, 4, 40.0)
    second = Similarity(76, 38, 0.5, 4, 38.0)
    library, _, query, _ = _library(
        tmp_path, {"a.png": best, "b.png": second}, references=("a.png", "b.png")
    )

    result = library.match(query)

    assert result.status == "not_found"
    assert result.path.name == "a.png"
    assert result.similarity == best
    assert result.runner_up == second


def test_match_accepts_close_strong_candidates(tmp_path):
    best = Similarity(100, 50, 0.8, 12, 70.0)
    second = Similarity(98, 49, 0.795, 12, 69.0)
    library, _, query, _ = _library(
        tmp_path, {"a.png": best, "b.png": second}, references=("a.png", "b.png")
    )

    result = library.match(query)

    assert result.status == "found"
    assert result.path.name == "a.png"
    assert result.runner_up == second


def test_match_accepts_clear_medium_candidate(tmp_path):
    best = Similarity(80, 40, 0.5, 4, 40.0)
    second = Similarity(60, 30, 0.5, 4, 30.0)
    library, _, query, _ = _library(
        tmp_path, {"a.png": best, "b.png": second}, references=("a.png", "b.png")
    )

    assert library.match(query).status == "found"


def test_exact_duplicate_skips_comparison_and_does_not_write(tmp_path):
    library, comparator, query, library_dir = _library(tmp_path)
    existing = library_dir / "a.png"
    query.write_bytes(existing.read_bytes())

    result = library.add(query)

    assert result.status == "duplicate"
    assert result.path == existing
    assert comparator.calls == []
    assert not (library_dir / "added").exists()


def test_layout_duplicate_does_not_write(tmp_path):
    duplicate = Similarity(120, 60, 0.5, 12, 40.0)
    library, _, query, library_dir = _library(tmp_path, {"a.png": duplicate})

    result = library.add(query)

    assert result.status == "duplicate"
    assert result.path == library_dir / "a.png"
    assert result.similarity == duplicate
    assert not (library_dir / "added").exists()


def test_partial_match_is_not_enough_for_full_duplicate(tmp_path):
    partial = Similarity(120, 60, 0.5, 12, 30.0)
    library, _, query, library_dir = _library(tmp_path, {"a.png": partial})

    result = library.add(query)

    assert result.status == "added"
    assert result.path.parent == library_dir / "added"
    assert result.path.read_bytes() == query.read_bytes()


def test_new_image_uses_content_hash_and_preserves_bytes(tmp_path):
    library, _, query, library_dir = _library(tmp_path, references=())
    content = b"new-jpeg-bytes"
    query = query.with_suffix(".jpg")
    query.write_bytes(content)

    result = library.add(query)

    expected_name = f"{hashlib.sha256(content).hexdigest()}.jpg"
    assert result.status == "added"
    assert result.path == library_dir / "added" / expected_name
    assert result.path.read_bytes() == content


def test_add_rejects_suffix_that_library_does_not_scan(tmp_path):
    library, _, query, library_dir = _library(tmp_path, references=())
    query = query.with_suffix(".gif")
    query.write_bytes(b"gif-bytes")

    with pytest.raises(TreasureMapError, match="格式"):
        library.add(query)

    assert not (library_dir / "added").exists()


def test_atomic_replace_failure_leaves_no_file_or_temporary_file(tmp_path, monkeypatch):
    library, _, query, library_dir = _library(tmp_path, references=())

    def fail_replace(source, target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("src.treasure_map.library.os.replace", fail_replace)
    with pytest.raises(TreasureMapError, match="写入"):
        library.add(query)

    added = library_dir / "added"
    assert not any(added.iterdir())
