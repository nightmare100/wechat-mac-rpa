"""人格化对照基线：合成场景、标签，以及和生产组装是否一致。不调用模型。"""
from pathlib import Path

from src.badcase.judge_worker import _JUDGE_PROMPT_TEMPLATE
from src.reply.generator import ReplyGenerator
from src.reply.persona_baseline import CASES, assembly_snapshot, natural_beats_assistant, problems
from src.tools import get_registry, register_builtin_tools

_REGISTRY = get_registry()
register_builtin_tools(_REGISTRY)


def test_labeled_natural_reply_beats_assistant_template():
    assert len(CASES) == 11
    for case in CASES:
        assert natural_beats_assistant(case), (case.case_id, problems(case, case.natural), problems(case, case.assistant))


def test_assembly_uses_current_guard_and_limits_examples():
    generator = ReplyGenerator(llm_client=None, tool_registry=_REGISTRY)
    persona = Path("data/persona.md").read_text(encoding="utf-8")
    snapshot = assembly_snapshot(generator, CASES[0], persona)

    assert snapshot["model_called"] is False
    assert snapshot["has_reply_guard"]
    assert snapshot["has_data_boundary"]
    assert 4 <= snapshot["example_count"] <= 8
    assert len(snapshot["persona_sha256"]) == 64


def test_judge_rubric_does_not_require_extra_performance():
    assert "错失展示" not in _JUDGE_PROMPT_TEMPLATE
    assert "空回复 → true（漏回复）" not in _JUDGE_PROMPT_TEMPLATE
    assert "守住角色" in _JUDGE_PROMPT_TEMPLATE
    assert "别再回" in _JUDGE_PROMPT_TEMPLATE
