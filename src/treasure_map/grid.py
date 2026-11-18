"""10×12 board grid. Covered cells are marked and excluded from comparison."""
import hashlib
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

from .models import TreasureMapError

COLS = 10
ROWS = 12
CELL_MATCHES = 4
CELL_SHIFT = 12.0
STRONG_MATCH_RATIO = 1.0
DUPLICATE_RATIO = 0.95

# Measured from the phone screenshots and the 828×1792 library shots.
# The board is the same 10×12 layout; these rectangles place that grid on the playfield.
_LIBRARY_SIZE = (828, 1792)
_LIBRARY_ORIGIN = (37, 493)
_LIBRARY_PITCH = 75
_INPUT_EDGES = (0.057, 0.943, 0.300, 0.780)


@dataclass(frozen=True)
class Board:
    x: int
    y: int
    width: int
    height: int

    def cell_bounds(self, row: int, col: int) -> tuple[int, int, int, int]:
        x0 = self.x + int(col * self.width / COLS)
        x1 = self.x + int((col + 1) * self.width / COLS)
        y0 = self.y + int(row * self.height / ROWS)
        y1 = self.y + int((row + 1) * self.height / ROWS)
        return x0, y0, x1, y1


def board_for(image: np.ndarray) -> Board:
    height, width = image.shape[:2]
    if (width, height) == _LIBRARY_SIZE:
        left, top = _LIBRARY_ORIGIN
        return Board(left, top, COLS * _LIBRARY_PITCH, ROWS * _LIBRARY_PITCH)
    left, right, top, bottom = _INPUT_EDGES
    x0 = int(round(left * width))
    x1 = int(round(right * width))
    y0 = int(round(top * height))
    y1 = int(round(bottom * height))
    return Board(x0, y0, max(COLS, x1 - x0), max(ROWS, y1 - y0))


def read_rgb(path: Path) -> np.ndarray:
    try:
        with Image.open(path) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
            image.load()
            return np.asarray(image)
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise TreasureMapError(f"无法读取藏宝图图片：{path}") from exc


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise TreasureMapError(f"无法读取藏宝图图片：{path}") from exc
    return digest.hexdigest()


def covered_cells(image: np.ndarray, board: Board) -> frozenset[tuple[int, int]]:
    """Cells still hidden by a purple or brown unopened tile."""
    hsv = cv2.cvtColor(image, cv2.COLOR_RGB2HSV)
    purple = cv2.inRange(hsv, (120, 80, 150), (160, 255, 255))
    brown = cv2.inRange(hsv, (5, 100, 140), (15, 210, 235))
    covered = cv2.bitwise_or(purple, brown)
    hidden = []
    for row in range(ROWS):
        for col in range(COLS):
            x0, y0, x1, y1 = board.cell_bounds(row, col)
            patch = covered[y0:y1, x0:x1]
            if patch.size and (patch > 0).mean() >= 0.45:
                hidden.append((row, col))
    return frozenset(hidden)


class CellComparer:
    def __init__(self):
        self._sift = cv2.SIFT_create(nfeatures=80, contrastThreshold=0.04)
        self._matcher = cv2.BFMatcher(cv2.NORM_L2)

    def descriptors(self, image: np.ndarray, board: Board, cells: list[tuple[int, int]]) -> dict[tuple[int, int], tuple[np.ndarray, np.ndarray]]:
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        described = {}
        for row, col in cells:
            x0, y0, x1, y1 = board.cell_bounds(row, col)
            patch = cv2.resize(gray[y0:y1, x0:x1], (64, 64), interpolation=cv2.INTER_AREA)
            keypoints, descriptors = self._sift.detectAndCompute(patch, None)
            if not keypoints or descriptors is None:
                points = np.empty((0, 2), dtype=np.float32)
                descriptors = np.empty((0, 128), dtype=np.float32)
            else:
                points = np.float32([point.pt for point in keypoints])
                descriptors = np.asarray(descriptors, dtype=np.float32)
            described[(row, col)] = (points, descriptors)
        return described

    def agreement(
        self,
        query: dict[tuple[int, int], tuple[np.ndarray, np.ndarray]],
        reference: dict[tuple[int, int], tuple[np.ndarray, np.ndarray]],
    ) -> int:
        hits = 0
        for cell, query_features in query.items():
            reference_features = reference.get(cell)
            if reference_features is not None and self._same_place(*query_features, *reference_features):
                hits += 1
        return hits

    def _same_place(self, query_points, query_desc, reference_points, reference_desc) -> bool:
        if len(query_desc) < 2 or len(reference_desc) < 2:
            return False
        pairs = self._matcher.knnMatch(query_desc, reference_desc, k=2)
        matched = [
            pair[0] for pair in pairs
            if len(pair) == 2 and pair[0].distance < 0.75 * pair[1].distance
        ]
        if len(matched) < CELL_MATCHES:
            return False
        query_xy = query_points[[match.queryIdx for match in matched]]
        reference_xy = reference_points[[match.trainIdx for match in matched]]
        shifts = reference_xy - query_xy
        for shift in shifts:
            aligned = np.linalg.norm(shifts - shift, axis=1) <= CELL_SHIFT
            if int(np.count_nonzero(aligned)) >= CELL_MATCHES:
                return True
        return False
