"""人格化对照基线：合成场景、标签，以及和生产组装是否一致。不调用模型。"""
from pathlib import Path
import xml.etree.ElementTree as ET

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


def test_self_refine_prompts_are_persona_agnostic_and_valid_xml():
    feedback = Path("prompts/feedback.md").read_text(encoding="utf-8")
    iterate = Path("prompts/iterate.md").read_text(encoding="utf-8")

    assert ET.fromstring(feedback).tag == "reply_audit"
    assert ET.fromstring(iterate).tag == "reply_revision"
    assert "林岚" not in feedback
    assert "林岚" not in iterate
    assert "仅含 replies" not in feedback
    assert "最多 3 条回复" not in iterate


def test_skills_inherit_persona_and_drop_service_proof_rules():
    for name in (
        "group_banter",
        "casual_chat",
        "handling_praise",
        "value_investing",
        "handling_vent",
        "receiving_share",
    ):
        text = Path(f"skills/{name}/SKILL.md").read_text(encoding="utf-8")
        assert ET.fromstring(text).tag == "skill", name
        assert "示例用户甲" not in text, name
        for banned in ("发红包", "应直接反击", "证明看到了内容", "确认自己被听见"):
            assert banned not in text, (name, banned)


def test_search_memory_tool_description_is_need_based():
    class _DummyMemory:
        def search_keyword(self, query, max_chars=3000):
            return ""

    gen = ReplyGenerator(llm_client=None, memory_engine=_DummyMemory())
    schema = next(t for t in gen.tool_registry.to_openai_schemas() if t["function"]["name"] == "search_memory")
    desc = schema["function"]["description"]
    assert "示例用户甲" not in desc
    assert "都应该先调用 search_memory" not in desc
    assert "不要仅因随口提到人名或熟人就调用" in desc
