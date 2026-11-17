"""Command-line adapter for the treasure map library."""
import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from .library import TreasureMapLibrary
from .models import AddResult, MatchResult, TreasureMapError

DEFAULT_LIBRARY_DIR = Path(__file__).resolve().parents[3] / "images"
DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "treasure_map"


def main(argv: Sequence[str] | None = None, library: TreasureMapLibrary | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        active_library = library or TreasureMapLibrary(args.library, args.cache_dir)
        if args.command == "match":
            result = active_library.match(args.image)
        else:
            result = active_library.add(args.image)
    except TreasureMapError as exc:
        print(f"藏宝图处理失败：{exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(_payload(result), ensure_ascii=False))
    else:
        print(_text(result))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="藏宝图布局匹配与去重收录")
    parser.add_argument("--library", type=Path, default=DEFAULT_LIBRARY_DIR, help="图库目录")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR, help="特征缓存目录")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("match", "查找最相似的藏宝图"), ("add", "去重后收录全开藏宝图")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("image", type=Path, help="输入图片路径")
        command.add_argument("--json", action="store_true", help="输出 JSON")
    return parser


def _payload(result: MatchResult | AddResult) -> dict:
    payload = {
        "status": result.status,
        "path": str(result.path) if result.path else None,
        "similarity": asdict(result.similarity) if result.similarity else None,
    }
    if isinstance(result, MatchResult):
        payload["runner_up"] = asdict(result.runner_up) if result.runner_up else None
    return payload


def _text(result: MatchResult | AddResult) -> str:
    if isinstance(result, AddResult):
        prefix = "已重复" if result.status == "duplicate" else "已加入"
        return f"{prefix}：{result.path}"
    if result.status == "not_found":
        return "找不到足够相似的藏宝图"
    output = f"找到最相似的藏宝图：{result.path}"
    if result.similarity:
        output += f"\n匹配特征：{result.similarity.inliers}，覆盖区域：{result.similarity.coverage}"
    return output
