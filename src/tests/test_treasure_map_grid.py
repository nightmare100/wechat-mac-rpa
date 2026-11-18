"""Same-cell comparison ignores covered tiles and does not ask for a grid size."""
import cv2
import numpy as np
from PIL import Image

from src.treasure_map.grid import COLS, ROWS, board_for, covered_cells, read_rgb
from src.treasure_map.library import TreasureMapLibrary


def _purple() -> tuple[int, int, int]:
    patch = cv2.cvtColor(np.uint8([[[140, 200, 200]]]), cv2.COLOR_HSV2RGB)
    return tuple(int(value) for value in patch[0, 0])


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


def test_covered_cells_follow_purple_tiles_on_a_ten_by_twelve_board(tmp_path):
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
    assert covered_cells(image, board) == frozenset(covered)


def test_match_uses_only_revealed_cells_at_the_same_position(tmp_path):
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

    result = TreasureMapLibrary(library_dir).match(query)

    assert result.status == "found"
    assert result.path.name == "same.png"
    assert result.similarity.inliers == result.similarity.good_matches == len(revealed)
