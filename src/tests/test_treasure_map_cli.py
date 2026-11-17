"""Command-line behavior for treasure map matching and adding."""
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from src.treasure_map import AddResult, MatchResult, Similarity, TreasureMapError
from src.treasure_map.cli import main

SIMILARITY = Similarity(120, 70, 0.8, 18, 56.0)


class FakeLibrary:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def match(self, image_path):
        if self.error:
            raise self.error
        return self.result

    def add(self, image_path):
        if self.error:
            raise self.error
        return self.result


def test_match_found_text_and_exit_code(capsys):
    path = Path("images/post/01.png")
    library = FakeLibrary(MatchResult("found", path, SIMILARITY))

    exit_code = main(["match", "query.png"], library=library)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "找到最相似的藏宝图" in output
    assert str(path) in output
    assert "70" in output


def test_match_not_found_text_and_exit_code(capsys):
    library = FakeLibrary(MatchResult("not_found", None, None))

    exit_code = main(["match", "query.png"], library=library)

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "找不到足够相似的藏宝图"


@pytest.mark.parametrize("status,prefix", [("duplicate", "已重复："), ("added", "已加入：")])
def test_add_text_and_exit_code(capsys, status, prefix):
    path = Path("images/added/map.png")
    library = FakeLibrary(AddResult(status, path))

    exit_code = main(["add", "query.png"], library=library)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert output.startswith(prefix)
    assert str(path) in output


def test_match_json_contains_structured_result(capsys):
    path = Path("images/post/01.png")
    result = MatchResult("found", path, SIMILARITY, runner_up=SIMILARITY)

    exit_code = main(["match", "query.png", "--json"], library=FakeLibrary(result))

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "found"
    assert payload["path"] == str(path)
    assert payload["similarity"] == asdict(SIMILARITY)
    assert payload["runner_up"] == asdict(SIMILARITY)


def test_not_found_json_uses_nulls(capsys):
    result = MatchResult("not_found", None, None)

    main(["match", "query.png", "--json"], library=FakeLibrary(result))

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"status": "not_found", "path": None, "similarity": None, "runner_up": None}


@pytest.mark.parametrize("status", ["duplicate", "added"])
def test_add_json_contains_status_path_and_similarity(capsys, status):
    path = Path("images/added/map.png")
    result = AddResult(status, path, SIMILARITY)

    main(["add", "query.png", "--json"], library=FakeLibrary(result))

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"status": status, "path": str(path), "similarity": asdict(SIMILARITY)}


def test_domain_error_is_concise_and_returns_two(capsys):
    library = FakeLibrary(error=TreasureMapError("坏图"))

    exit_code = main(["match", "query.png"], library=library)

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert "藏宝图处理失败：坏图" in captured.err
    assert "Traceback" not in captured.err


def test_public_api_exports_library_and_result_types():
    import src.treasure_map as api

    assert api.TreasureMapLibrary.__name__ == "TreasureMapLibrary"
    assert api.MatchResult is MatchResult
    assert api.AddResult is AddResult
    assert api.Similarity is Similarity
    assert api.TreasureMapError is TreasureMapError
