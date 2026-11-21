"""Same-cell comparison ignores covered tiles and does not ask for a grid size."""
import cv2
import numpy as np
import pytest
from PIL import Image

from src.treasure_map.grid import Board, COLS, ROWS, _cover_pixels_match, board_for, covered_cells, read_rgb
from src.treasure_map.library import TreasureMapLibrary
from src.treasure_map.models import TreasureMapError


def _purple() -> tuple[int, int, int]:
    patch = cv2.cvtColor(np.uint8([[[140, 200, 200]]]), cv2.COLOR_HSV2RGB)
    return tuple(int(value) for value in patch[0, 0])


@pytest.fixture
def cover_dir(tmp_path):
    directory = tmp_path / "covers"
    directory.mkdir()
    Image.new("RGB", (40, 40), _purple()).save(directory / "purple.png")
    return directory


def _stamp(cell: np.ndarray, seed: int) -> None:
    cell[:] = (70, 42, 32)
    height, width = cell.shape[:2]
    cv2.rectangle(cell, (2, 2), (width - 3, height - 3), (20 + seed * 17 % 200, 30 + seed * 9 % 180, 40 + seed * 13 % 160), 2)
    cv2.line(cell, (4, 4 + seed % 7), (width - 5, height - 5 - seed % 5), (255, 220, 40), 2)
    cv2.circle(cell, (width // 2, height // 2), 3 + seed % 6, (40, 220, 255), -1)


def _render(path, patterns: dict[tuple[int, int], int], covered: set[tuple[int, int]]) -> None:
    image = np.full((886, 400, 3), (20, 16, 28), dtype=np.uint8)
    board = board_for(image)
    purple = _purple()
    for row in range(ROWS):
        for col in range(COLS):
            x0, y0, x1, y1 = board.cell_bounds(row, col)
            if (row, col) in covered:
                image[y0:y1, x0:x1] = purple
            else:
                _stamp(image[y0:y1, x0:x1], patterns[(row, col)])
    Image.fromarray(image).save(path)


def test_covered_cells_match_templates_on_a_ten_by_twelve_board(tmp_path, cover_dir):
    covered = {(0, 0), (0, 1), (3, 4), (11, 9)}
    patterns = {
        (row, col): row * COLS + col + 1
        for row in range(ROWS)
        for col in range(COLS)
        if (row, col) not in covered
    }
    path = tmp_path / "query.png"
    _render(path, patterns, covered)

    image = read_rgb(path)
    board = board_for(image)
    assert board.width > COLS and board.height > ROWS
    assert covered_cells(image, board, cover_dir) == frozenset(covered)


def test_match_uses_only_revealed_cells_at_the_same_position(tmp_path, cover_dir):
    revealed = [(row, col) for row in range(4) for col in range(5)]
    covered = {
        (row, col)
        for row in range(ROWS)
        for col in range(COLS)
        if (row, col) not in revealed
    }
    same = {cell: index + 1 for index, cell in enumerate(revealed)}
    shifted = {(row, (col + 1) % 5): pattern for (row, col), pattern in same.items() if row < 4}

    def complete(seed_map: dict[tuple[int, int], int], salt: int) -> dict[tuple[int, int], int]:
        return {
            (row, col): seed_map.get((row, col), 1000 + salt + row * COLS + col)
            for row in range(ROWS)
            for col in range(COLS)
        }

    library_dir = tmp_path / "library"
    library_dir.mkdir()
    _render(library_dir / "same.png", complete(same, 0), set())
    _render(library_dir / "shifted.png", complete(shifted, 50), set())
    query = tmp_path / "query.png"
    _render(query, same, covered)

    result = TreasureMapLibrary(library_dir, cover_dir=cover_dir).match(query)

    assert result.status == "found"
    assert result.path.name == "same.png"
    assert result.similarity.inliers == result.similarity.good_matches == len(revealed)


def test_add_rejects_one_covered_cell_before_duplicate_check(tmp_path, cover_dir):
    patterns = {(row, col): row * COLS + col for row in range(ROWS) for col in range(COLS)}
    library_dir = tmp_path / "library"
    library_dir.mkdir()
    covered = library_dir / "previously-added.png"
    _render(covered, patterns, {(10, 8)})
    library = TreasureMapLibrary(library_dir, cover_dir=cover_dir)

    with pytest.raises(TreasureMapError, match="该藏宝图未全开，不予收录"):
        library.add(covered)
    assert list(library_dir.iterdir()) == [covered]


def test_add_accepts_fully_revealed_map_and_still_checks_duplicates(tmp_path, cover_dir):
    patterns = {(row, col): row * COLS + col for row in range(ROWS) for col in range(COLS)}
    full = tmp_path / "full.png"
    _render(full, patterns, set())
    library = TreasureMapLibrary(tmp_path / "library", cover_dir=cover_dir)

    added = library.add(full)
    assert added.status == "added"
    assert added.path.read_bytes() == full.read_bytes()
    assert library.add(full).status == "duplicate"


def test_add_checks_near_identical_pixels_when_cell_features_miss(tmp_path, cover_dir, monkeypatch):
    patterns = {(row, col): row * COLS + col for row in range(ROWS) for col in range(COLS)}
    library_dir = tmp_path / "library"
    library_dir.mkdir()
    original = library_dir / "original.png"
    _render(original, patterns, set())
    query = tmp_path / "reencoded.png"
    pixels = np.clip(read_rgb(original).astype(np.int16) + 5, 0, 255).astype(np.uint8)
    Image.fromarray(pixels).save(query)
    library = TreasureMapLibrary(library_dir, cover_dir=cover_dir)
    monkeypatch.setattr(library._cells, "agreement", lambda *_: 110)

    result = library.add(query)

    assert result.status == "duplicate"
    assert result.path == original
    assert not (library_dir / "added").exists()

    # A different cell should prevent the pixel check from declaring a duplicate.
    x0, y0, x1, y1 = board_for(pixels).cell_bounds(3, 4)
    pixels[y0:y1, x0:x1] = 240
    Image.fromarray(pixels).save(query)
    assert library.add(query).status == "added"



def test_cover_threshold_is_strict():
    template = np.full((40, 40, 3), 180, dtype=np.uint8)
    cell = template.copy()
    cell[:4] = 0
    assert not _cover_pixels_match(cell, template)
    cell[3, 0] = template[3, 0]
    assert _cover_pixels_match(cell, template)


def test_cover_search_aligns_shifted_template_and_checks_pixel_positions(tmp_path):
    template = np.full((40, 40, 3), (105, 23, 155), dtype=np.uint8)
    template[:, 20:] = (155, 23, 105)
    Image.fromarray(template).save(tmp_path / "cover.png")
    board = Board(5, 5, 40 * COLS, 40 * ROWS)
    image = np.zeros((board.height + 10, board.width + 10, 3), dtype=np.uint8)
    image[9:49, 9:49] = template
    image[5:45, 85:125] = template[:, ::-1]
    assert covered_cells(image, board, tmp_path) == {(0, 0)}


def test_add_with_no_templates_does_not_write_to_library(tmp_path):
    full = tmp_path / "input.png"
    Image.new("RGB", (400, 886)).save(full)
    library_dir = tmp_path / "library"
    library = TreasureMapLibrary(library_dir, cover_dir=tmp_path / "missing-covers")

    with pytest.raises(TreasureMapError, match="缺少藏宝图封面模板"):
        library.add(full)
    assert not library_dir.exists()
