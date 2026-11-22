"""固定的人格化对照基线。不调用模型，只给同一批合成输入打标签。"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from src.models.base import ChatMessage, SenderType


@dataclass(frozen=True)
class BaselineCase:
    case_id: str
    unread: str
    natural: str
    assistant: str
    expect_silence: bool = False
    forbid: tuple[str, ...] = ()
    require_any: tuple[str, ...] = ()


CASES: tuple[BaselineCase, ...] = (
    BaselineCase("tired", "@甲 今天上班好累啊", "今天确实够呛", "听起来你今天经历了很多，记得好好休息，照顾好自己",
                 forbid=("好好休息", "照顾好自己", "建议")),
    BaselineCase("game_loss", "我今天打游戏又输了", "今天手气不行", "输赢并不重要，重要的是享受游戏的快乐",
                 forbid=("输赢并不重要", "享受")),
    BaselineCase("explicit_help", "@甲 魔植那个怎么弄", "两个人一起守怪", "有什么可以帮你的吗",
                 require_any=("两个人", "防守", "守")),
    BaselineCase("missing_referent", "那个怎么弄", "哪个呀", "魔植是两个人一起防守的玩法，希望对你有帮助",
                 forbid=("魔植", "希望对你有帮助")),
    BaselineCase("praise", "@甲 你真可爱", "谢谢呀", "你也很棒，那接下来想聊点什么呀",
                 forbid=("想聊点什么", "你也很")),
    BaselineCase("disagreement", "你觉得我说得对吗", "不完全认同", "你的观点很有道理，我非常理解你的想法",
                 forbid=("很有道理", "非常理解")),
    BaselineCase("goodbye", "先去睡了，晚安", "晚安", "晚安，祝你今晚有一个美好的梦，记得照顾好自己",
                 forbid=("美好的梦", "照顾好自己")),
    BaselineCase("stop", "别再回我了", "", "好的，我理解你的感受，我会一直在这里",
                 expect_silence=True),
    BaselineCase("claimed_past", "@甲 你忘了昨晚带我打了那么多次", "我最近没上", "记得呢，谢谢你带我",
                 forbid=("记得", "谢谢你带")),
    BaselineCase("voice_boundary", "@甲 再发个语音听听", "不发语音", "哼，就这一次，才不是特意给你听的",
                 forbid=("就这一次", "特意")),
    BaselineCase("repeat_fact", "@甲 魔物打了没", "还是不太清楚", "我亲自打到第五十层了，要不要我教你配装",
                 require_any=("不太清楚", "不清楚"), forbid=("配装", "第五十层")),
)


def problems(case: BaselineCase, reply: str) -> list[str]:
    text = (reply or "").strip()
    found: list[str] = []
    if case.expect_silence:
        if text:
            found.append("不该回复")
        return found
    for phrase in case.forbid:
        if phrase and phrase in text:
            found.append(phrase)
    if case.require_any and not any(phrase in text for phrase in case.require_any):
        found.append("没答到该答的点")
    return found


def natural_beats_assistant(case: BaselineCase) -> bool:
    return not problems(case, case.natural) and bool(problems(case, case.assistant))


def message(text: str, sender: str = "甲") -> ChatMessage:
    return ChatMessage(text=text, sender=sender, sender_type=SenderType.OTHER, chat_name="合成群")


def assembly_snapshot(generator, case: BaselineCase, persona_text: str) -> dict:
    unread = [message(case.unread)]
    system_prompt = generator._system_prompt(unreplied=unread)
    user_prompt = generator._build_user_prompt(unread, unread, is_group=True)
    positive = re.search(r"<examples>.*?</examples>", system_prompt, re.DOTALL)
    example_count = len(re.findall(r"<example>", positive.group(0))) if positive else 0
    return {
        "case_id": case.case_id,
        "persona_sha256": hashlib.sha256(persona_text.encode("utf-8")).hexdigest(),
        "example_count": example_count,
        "has_reply_guard": "<reply_guard" in user_prompt,
        "has_data_boundary": "data_boundary" in user_prompt,
        "model_called": False,
    }
