"""按稳定人物卡 + 当前话题相关性筛选注入 prompt 的记忆（OPT-014）。"""

from __future__ import annotations

import re
from typing import Iterable, List, Tuple

_GREETING_OR_FILLER = {
    "在吗", "在不在", "在嘛", "你好", "你好呀", "嗨", "嗨嗨", "哈喽", "hello", "hi",
    "早", "早安", "早上好", "午安", "晚上好", "晚安", "睡了", "先睡了", "拜拜", "再见",
    "哈哈", "哈哈哈", "嘿嘿", "嘻嘻", "嗯", "嗯嗯", "哦", "哦哦", "好的", "好哒", "收到",
    "谢谢", "谢谢呀", "谢了", "么么哒", "辛苦了", "加油",
}

_STABLE_SECTION_KEYWORDS = (
    "权威纠正",
    "重要纠正",
    "补充信息",
    "基本信息",
    "与 bot 的关系",
    "与bot的关系",
    "交互风格",
    "群简介",
    "群定位",
    "群规则",
)

_SECTION_HEADER_RE = re.compile(r"^(?:##\s+(.+)|([^\s#\-•·：:][^：:]{0,16})：)\s*$")
_AT_PREFIX_RE = re.compile(r"@\S+")
_TOKEN_RE = re.compile(r"[a-z0-9]{2,}|[\u4e00-\u9fff]{2,}")


def _is_stable_section(title: str) -> bool:
    lowered = title.strip().lower()
    return any(keyword in lowered for keyword in _STABLE_SECTION_KEYWORDS)


def extract_topic_terms(query: str) -> set[str]:
    """从当前未读中提取有实质语义的话题词；纯问候/语气词返回空集。"""
    cleaned = _AT_PREFIX_RE.sub(" ", query or "").strip().lower()
    cleaned_no_punct = re.sub(r"[\s，。！？!?,.\-~～…、；;：:（）()「」\"']+", "", cleaned)
    if not cleaned_no_punct or cleaned_no_punct in _GREETING_OR_FILLER:
        return set()

    terms: set[str] = set()
    for token in _TOKEN_RE.findall(cleaned):
        if token in _GREETING_OR_FILLER:
            continue
        if re.fullmatch(r"[a-z0-9]{2,}", token):
            terms.add(token)
        else:
            # 中文词段按 2-gram 切分，避免整句匹配过严
            for i in range(len(token) - 1):
                bg = token[i : i + 2]
                if bg not in _GREETING_OR_FILLER:
                    terms.add(bg)
    return terms


def _split_sections(text: str) -> Tuple[List[str], List[Tuple[str, str, List[str]]]]:
    """把 wiki / brief 拆成 preamble + [(raw_header, title, body_lines)]。"""
    preamble: List[str] = []
    sections: List[Tuple[str, str, List[str]]] = []
    current: Tuple[str, str, List[str]] | None = None

    for line in text.splitlines():
        match = _SECTION_HEADER_RE.match(line.strip())
        if match:
            title = (match.group(1) or match.group(2) or "").strip()
            current = (line.rstrip(), title, [])
            sections.append(current)
        elif current is not None:
            current[2].append(line.rstrip())
        else:
            preamble.append(line.rstrip())
    return preamble, sections


def _line_overlap_score(line: str, terms: set[str]) -> int:
    if not terms:
        return 0
    lowered = line.lower()
    return sum(1 for term in terms if term in lowered)


def _trim_to_budget(lines: Iterable[str], max_chars: int) -> str:
    kept: List[str] = []
    total = 0
    for line in lines:
        sep = 1 if kept else 0
        if total + sep + len(line) > max_chars:
            break
        kept.append(line)
        total += sep + len(line)
    return "\n".join(kept).strip()


def select_relevant_memory(text: str, query: str, max_chars: int = 2000) -> str:
    """保留稳定人物/群卡片；历史与动态条目仅在命中当前话题时按预算召回。"""
    if not text or not text.strip():
        return ""
    preamble, sections = _split_sections(text)
    if not sections:
        return text.strip()[:max_chars]

    terms = extract_topic_terms(query)
    stable_lines: List[str] = [line for line in preamble if line.strip()]
    scored_topic_blocks: List[Tuple[int, int, str, List[str]]] = []

    for idx, (raw_header, title, body) in enumerate(sections):
        non_empty = [line for line in body if line.strip()]
        if not non_empty:
            continue
        if _is_stable_section(title):
            if stable_lines:
                stable_lines.append("")
            stable_lines.append(raw_header)
            stable_lines.extend(non_empty)
            continue

        if not terms:
            continue

        matched_lines: List[Tuple[int, int, str]] = []
        title_score = _line_overlap_score(title, terms)
        for line_idx, line in enumerate(non_empty):
            score = _line_overlap_score(line, terms) + title_score
            if score > 0:
                matched_lines.append((score, line_idx, line))
        if matched_lines:
            matched_lines.sort(key=lambda item: (-item[0], item[1]))
            best_score = matched_lines[0][0]
            ordered = [line for _, _, line in sorted(matched_lines, key=lambda item: item[1])]
            scored_topic_blocks.append((best_score, idx, raw_header, ordered))

    scored_topic_blocks.sort(key=lambda item: (-item[0], item[1]))

    combined = list(stable_lines)
    for _, _, raw_header, ordered_lines in scored_topic_blocks:
        if combined:
            combined.append("")
        combined.append(raw_header)
        combined.extend(ordered_lines)

    return _trim_to_budget(combined, max_chars)
