"""目标触发按需图像识别（ImageDescriber + WeChatBot._enrich_target_images）单测。"""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from src.bot.group_config import GroupConfig
from src.bot.wechat_bot import WeChatBot
from src.models.base import ChatMessage, SenderType
from src.perception.image_describer import (
    ImageDescriber,
    compress_image_to_jpeg,
    extract_visual_knowledge_section,
    select_target_images,
)
from src.reply.generator import ReplyGenerator
from src.tools import get_registry, register_builtin_tools

_REG = get_registry()
register_builtin_tools(_REG)
_REAL_KB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "game_knowledge.md"


def _png_bytes(size=(1600, 900), color=(240, 120, 100)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", size, color + (255,)).save(buf, format="PNG")
    return buf.getvalue()


def _img_msg(
    sender: str = "Alice",
    *,
    wxid: str = "wxid_alice",
    chat_name: str = "弓箭2战神公会",
    media_url: str = "/media/img1",
    server_id: str = "srv1",
    create_time: int = 1000,
    image_description: str = "",
) -> ChatMessage:
    return ChatMessage(
        text="[图片，内容未解析]",
        sender=sender,
        sender_wxid=wxid,
        sender_type=SenderType.OTHER,
        chat_name=chat_name,
        chatroom_id="room@chatroom",
        message_type="image",
        media_url=media_url,
        server_id=server_id,
        create_time=create_time,
        image_description=image_description,
    )


def _txt_msg(
    text: str,
    sender: str = "Alice",
    *,
    wxid: str = "wxid_alice",
    chat_name: str = "弓箭2战神公会",
    is_at_me: bool = False,
    create_time: int = 1010,
    sender_type: SenderType = SenderType.OTHER,
    quoted_text: str = "",
) -> ChatMessage:
    return ChatMessage(
        text=text,
        sender=sender,
        sender_wxid=wxid,
        sender_type=sender_type,
        chat_name=chat_name,
        chatroom_id="room@chatroom",
        message_type="text",
        is_at_me=is_at_me,
        create_time=create_time,
        quoted_text=quoted_text,
    )


def _mock_openai_client(content: str):
    client = Mock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )
    return client


def _bot_stub(group_config: GroupConfig, describer: ImageDescriber, reader):
    bot = WeChatBot.__new__(WeChatBot)
    bot.group_config = group_config
    bot.image_describer = describer
    bot.perception = SimpleNamespace(client=SimpleNamespace(read_bytes=reader))
    bot.generator = ReplyGenerator(llm_client=None, memory_engine=None, tool_registry=_REG)
    return bot


def test_extract_visual_knowledge_and_compress_image():
    text = _REAL_KB_PATH.read_text(encoding="utf-8")
    sec = extract_visual_knowledge_section(text)
    assert "品阶与边框颜色" in sec
    assert "常见数值单位后缀" in sec
    assert "识图防幻觉守则" in sec
    assert "基础玩法与公会日常" not in sec

    raw = _png_bytes((1600, 900))
    comp, mime = compress_image_to_jpeg(raw, max_side=1024)
    assert mime == "image/jpeg"
    with Image.open(io.BytesIO(comp)) as out:
        assert max(out.size) == 1024
        assert out.mode == "RGB"


def test_chime_in_never_triggers_image_recognition(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ENABLE_IMAGE_UNDERSTANDING", "1")
    cfg_path = tmp_path / "group_config.json"
    cfg_path.write_text(json.dumps({"groups": {"弓箭2战神公会": {"image_understanding": True}}}), encoding="utf-8")

    vlm_client = _mock_openai_client('{"description":"红装出货","text":"神铸"}')
    reader = Mock(return_value=_png_bytes((400, 400)))
    describer = ImageDescriber(client=vlm_client, kb_path=_REAL_KB_PATH)
    bot = _bot_stub(GroupConfig(cfg_path), describer, reader)

    # 群友晒图 + 吐槽，没有 @Bot、没有提 Bot、没有引用、没有 followup（纯主动插话场景）
    img = _img_msg("Alice", create_time=1000)
    txt = _txt_msg("今天运气爆棚出了个好东西", "Alice", create_time=1005)
    count = bot._enrich_target_images("弓箭2战神公会", [img, txt], [img, txt], is_group=True)

    assert count == 0
    reader.assert_not_called()
    vlm_client.chat.completions.create.assert_not_called()
    assert img.image_description == ""


def test_target_image_in_batch_and_recent_history_and_ignores_other_sender(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ENABLE_IMAGE_UNDERSTANDING", "1")
    cfg_path = tmp_path / "group_config.json"
    cfg_path.write_text(json.dumps({"groups": {"弓箭2战神公会": {"image_understanding": True}}}), encoding="utf-8")

    vlm_client = _mock_openai_client('{"description":"抽卡结算页出了红色S武器","text":"龙骑长矛 12.5M"}')
    reader = Mock(return_value=_png_bytes((400, 400)))
    describer = ImageDescriber(client=vlm_client, kb_path=_REAL_KB_PATH)
    bot = _bot_stub(GroupConfig(cfg_path), describer, reader)

    # Alice 先发图（30 秒前已在历史），Bob 也发了一张无关图片，然后 Alice @Bot 问
    alice_old_img = _img_msg("Alice", wxid="wxid_alice", media_url="/media/a1", server_id="a1", create_time=980)
    bob_img = _img_msg("Bob", wxid="wxid_bob", media_url="/media/b1", server_id="b1", create_time=995)
    alice_at = _txt_msg("@小糯米 你看这个厉害吗", "Alice", wxid="wxid_alice", is_at_me=True, create_time=1010)

    all_msgs = [alice_old_img, bob_img, alice_at]
    to_reply = [bob_img, alice_at]
    count = bot._enrich_target_images("弓箭2战神公会", to_reply, all_msgs, is_group=True)

    assert count == 1
    reader.assert_called_once_with("/media/a1")
    assert alice_old_img.image_description == "抽卡结算页出了红色S武器"
    assert alice_old_img.image_text == "龙骑长矛 12.5M"
    # Bob 不是 target，他的图绝不被识别
    assert bob_img.image_description == ""

    # 同一张图再次触发时命中缓存，不重复调 VLM
    alice_old_img.image_description = ""
    count2 = bot._enrich_target_images("弓箭2战神公会", [alice_at], all_msgs, is_group=True)
    assert count2 == 1
    assert reader.call_count == 1
    assert vlm_client.chat.completions.create.call_count == 1


def test_target_followup_triggers_image_recognition(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ENABLE_IMAGE_UNDERSTANDING", "1")
    monkeypatch.setenv("BOT_NAME", "小糯米")
    monkeypatch.setenv("BOT_AT_KEYWORD", "小糯米")
    cfg_path = tmp_path / "group_config.json"
    cfg_path.write_text(json.dumps({"groups": {"弓箭2战神公会": {"image_understanding": True}}}), encoding="utf-8")

    vlm_client = _mock_openai_client('{"description":"魔物得分结算图","text":"150.00t"}')
    reader = Mock(return_value=_png_bytes((300, 300)))
    describer = ImageDescriber(client=vlm_client, kb_path=_REAL_KB_PATH)
    bot = _bot_stub(GroupConfig(cfg_path), describer, reader)

    # Bot 刚回复过 Alice，Alice 进入 followup 窗口后直接发图
    prev_alice = _txt_msg("@小糯米 我刚打完魔物", "Alice", is_at_me=True, create_time=995)
    bot.generator.followup.record("弓箭2战神公会", [prev_alice], ["@Alice 打得不错呀"])
    self_msg = _txt_msg("@Alice 打得不错呀", "自己", sender_type=SenderType.SELF, create_time=1000)
    alice_img = _img_msg("Alice", media_url="/media/f1", server_id="f1", create_time=1010)

    count = bot._enrich_target_images("弓箭2战神公会", [alice_img], [prev_alice, self_msg, alice_img], is_group=True)
    assert count == 1
    assert alice_img.image_description == "魔物得分结算图"


def test_caps_at_max_two_images_and_respects_group_switch_and_error_fallback(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ENABLE_IMAGE_UNDERSTANDING", "1")
    cfg_path = tmp_path / "group_config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "groups": {
                    "弓箭2战神公会": {"image_understanding": True},
                    "戰神管理群": {"image_understanding": False},
                }
            }
        ),
        encoding="utf-8",
    )

    # 1) 连发 4 张图只取最新 2 张
    imgs = [
        _img_msg("Alice", media_url=f"/media/{i}", server_id=f"s{i}", create_time=1000 + i)
        for i in range(4)
    ]
    at_msg = _txt_msg("@小糯米 帮我看看", "Alice", is_at_me=True, create_time=1010)
    picked = select_target_images(imgs + [at_msg], imgs + [at_msg], [at_msg], max_images=2)
    assert [m.server_id for m in picked] == ["s2", "s3"]

    # 2) 管理群关闭开关 -> 0 调用
    vlm_client = _mock_openai_client('{"description":"test","text":""}')
    reader = Mock(return_value=_png_bytes((300, 300)))
    bot = _bot_stub(GroupConfig(cfg_path), ImageDescriber(client=vlm_client), reader)
    assert bot._enrich_target_images("戰神管理群", [imgs[0], at_msg], [imgs[0], at_msg], is_group=True) == 0
    reader.assert_not_called()

    # 3) 拉图 404 异常静默降级不抛异常
    failing_reader = Mock(side_effect=RuntimeError("404 not found"))
    bot_fail = _bot_stub(GroupConfig(cfg_path), ImageDescriber(client=vlm_client), failing_reader)
    assert bot_fail._enrich_target_images("弓箭2战神公会", [imgs[0], at_msg], [imgs[0], at_msg], is_group=True) == 0
    assert imgs[0].image_description == ""


def test_format_message_line_drops_tracememo_placeholder_when_described():
    # 纯画面图片（image_text 为空）不应把 "[图片，内容未解析]" 当成图上文字拼进 prompt
    pure_visual = _img_msg("Alice", image_description="一只橘猫在桌上打哈欠")
    pure_visual.image_text = ""
    line = ReplyGenerator._format_message_line(pure_visual, enable_timestamps=False)
    assert line == "Alice：[图片] 一只橘猫在桌上打哈欠"
    assert "内容未解析" not in line

    # 带图上文字的图片正常展示图上文字
    with_text = _img_msg("Alice", image_description="魔物得分结算图")
    with_text.image_text = "150.00t"
    line2 = ReplyGenerator._format_message_line(with_text, enable_timestamps=False)
    assert line2 == "Alice：[图片] 魔物得分结算图（图上文字：150.00t）"

    # 未解析图片仍保留占位符
    unresolved = _img_msg("Alice", image_description="")
    line3 = ReplyGenerator._format_message_line(unresolved, enable_timestamps=False)
    assert "内容未解析" in line3

    # 默认单张超时已收紧为 12 秒
    assert ImageDescriber()._timeout == 12.0


def test_chime_in_image_enricher_triggers_regen_and_respects_cooldown(monkeypatch):
    """主动插话决定要回后，按需识图并把图片描述二次投喂给主模型；冷却中或首轮沉默时不调用。"""
    monkeypatch.setenv("CHIME_IN_COOLDOWN_MINUTES", "10")

    class _SeqLLM:
        def __init__(self, responses):
            self.responses = list(responses)
            self.seen_user_prompts = []

        def chat(self, messages, tools=None, **kwargs):
            self.seen_user_prompts.append(messages[-1]["content"])
            return self.responses.pop(0)

    # 1) 首轮决定插话 -> 触发识图 -> 二次投喂带图片描述的 prompt -> 返回看懂图后的新回复
    img = _img_msg("既梦", server_id="img_1", chat_name="弓箭2战神公会")
    text = ChatMessage(text="亏麻了", sender="既梦", sender_type=SenderType.OTHER, chat_name="弓箭2战神公会")
    llm = _SeqLLM([
        '{"replies": ["@既梦 怎么啦"], "chime_in": true}',
        '{"replies": ["@既梦 刷了十次都没出红 确实心疼"], "chime_in": true}',
    ])
    gen = ReplyGenerator(llm_client=llm)
    gen._started_at = 0
    gen.enable_skills = False
    gen.enable_self_refine = False
    gen.enable_react_tools = False
    gen.enable_chime_in = True

    enricher_calls = []

    def _enricher(chat_name, target_msgs, unreplied, all_msgs):
        enricher_calls.append((chat_name, [m.sender for m in target_msgs]))
        img.image_description = "附魔十连全是紫词条没有出红"
        img.image_text = "附魔消耗 1000"
        return 1

    replies = gen.generate(
        [img, text],
        [img, text],
        is_group=True,
        chime_in_image_enricher=_enricher,
    )
    assert len(enricher_calls) == 1
    assert len(llm.seen_user_prompts) == 2
    assert "内容未解析" in llm.seen_user_prompts[0]
    assert "附魔十连全是紫词条没有出红" in llm.seen_user_prompts[1]
    assert replies and "出红" in replies[0]

    # 2) 冷却期内另一位群友发图触发主动插话：直接跳过识图，绝不浪费 VLM
    enricher_calls.clear()
    img2 = _img_msg("许愿大王", server_id="img_2", chat_name="弓箭2战神公会")
    text2 = ChatMessage(text="又歪了", sender="许愿大王", sender_type=SenderType.OTHER, chat_name="弓箭2战神公会")
    llm.responses = ['{"replies": ["@许愿大王 抱抱"], "chime_in": true}']
    replies2 = gen.generate(
        [img2, text2],
        [img2, text2],
        is_group=True,
        chime_in_image_enricher=_enricher,
    )
    assert replies2 == []
    assert enricher_calls == []

    # 3) 看懂图后模型主动撤回（返回空 replies）：不记录冷却
    gen.chime_in_last.clear()
    img3 = _img_msg("既梦", server_id="img_3", chat_name="弓箭2战神公会")
    text3 = ChatMessage(text="搞定", sender="既梦", sender_type=SenderType.OTHER, chat_name="弓箭2战神公会")
    llm_abort = _SeqLLM([
        '{"replies": ["@既梦 厉害呀"], "chime_in": true}',
        '{"replies": []}',
    ])
    gen2 = ReplyGenerator(llm_client=llm_abort)
    gen2._started_at = 0
    gen2.enable_skills = False
    gen2.enable_self_refine = False
    gen2.enable_react_tools = False
    gen2.enable_chime_in = True
    replies3 = gen2.generate(
        [img3, text3],
        [img3, text3],
        is_group=True,
        chime_in_image_enricher=lambda c, t, u, a: (setattr(img3, "image_description", "工作表格截图"), 1)[1],
    )
    assert replies3 == []
    assert "弓箭2战神公会" not in gen2.chime_in_last

    # 4) 目标消息全是 @别人：不调用识图
    at_other = ChatMessage(text="@宝哥 带刷", sender="既梦", sender_type=SenderType.OTHER, chat_name="弓箭2战神公会")
    enricher_spy = Mock(return_value=1)
    llm_at = _SeqLLM(['{"replies": ["@既梦 冲呀"], "chime_in": true}'])
    gen3 = ReplyGenerator(llm_client=llm_at)
    gen3._started_at = 0
    gen3.enable_skills = False
    gen3.enable_self_refine = False
    gen3.enable_react_tools = False
    gen3.enable_chime_in = True
    assert gen3.generate([at_other], [at_other], is_group=True, chime_in_image_enricher=enricher_spy) == []
    enricher_spy.assert_not_called()


def test_bot_enrich_chime_in_images_respects_group_switch(tmp_path):
    cfg_path = tmp_path / "group_config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "groups": {
                    "弓箭2战神公会": {"image_understanding": True},
                    "戰神管理群": {"image_understanding": False},
                }
            }
        ),
        encoding="utf-8",
    )
    vlm_client = _mock_openai_client('{"description":"附魔出红界面","text":"暴击+12%"}')
    reader = Mock(return_value=_png_bytes((300, 300)))
    bot = _bot_stub(GroupConfig(cfg_path), ImageDescriber(client=vlm_client), reader)

    img_off = _img_msg("既梦", server_id="off1", chat_name="戰神管理群")
    assert bot._enrich_chime_in_images("戰神管理群", [img_off], [img_off], [img_off]) == 0
    reader.assert_not_called()

    img_on = _img_msg("既梦", server_id="on1", chat_name="弓箭2战神公会")
    assert bot._enrich_chime_in_images("弓箭2战神公会", [img_on], [img_on], [img_on]) == 1
    assert img_on.image_description == "附魔出红界面"
    assert img_on.image_text == "暴击+12%"


def test_chime_in_image_failure_drops_short_text_and_supports_spaced_nickname(monkeypatch, caplog):
    """识图失败时对「未解析图+极短词」丢弃插话并记日志；长文保留；含空格昵称精准定位目标。"""
    import logging

    monkeypatch.setenv("CHIME_IN_COOLDOWN_MINUTES", "10")

    class _SeqLLM:
        def __init__(self, responses):
            self.responses = list(responses)

        def chat(self, messages, tools=None, **kwargs):
            return self.responses.pop(0)

    # 1) 识图失败 (enriched=0) + 极短情绪词 -> 丢弃主动插话且不记冷却，记录 warning
    img_short = _img_msg("既梦", server_id="s_fail", chat_name="弓箭2战神公会")
    text_short = ChatMessage(text="亏麻了", sender="既梦", sender_type=SenderType.OTHER, chat_name="弓箭2战神公会")
    gen = ReplyGenerator(llm_client=_SeqLLM(['{"replies": ["@既梦 怎么啦"], "chime_in": true}']))
    gen._started_at = 0
    gen.enable_skills = False
    gen.enable_self_refine = False
    gen.enable_react_tools = False
    gen.enable_chime_in = True

    with caplog.at_level(logging.WARNING, logger="src.reply.generator"):
        res = gen.generate(
            [img_short, text_short],
            [img_short, text_short],
            is_group=True,
            chime_in_image_enricher=lambda c, t, u, a: 0,
        )
    assert res == []
    assert gen.last_chime_in_dropped is True
    assert "弓箭2战神公会" not in gen.chime_in_last
    assert any("识图未补全" in rec.message for rec in caplog.records)
    assert any("丢弃主动插话" in rec.message for rec in caplog.records)

    # 2) 识图失败 (enriched=0) + 文字信息充足 -> 保留原回复
    img_long = _img_msg("既梦", server_id="l_fail", chat_name="弓箭2战神公会")
    text_long = ChatMessage(
        text="刚才连抽十次终于凑齐三混装备太爽了",
        sender="既梦",
        sender_type=SenderType.OTHER,
        chat_name="弓箭2战神公会",
    )
    gen2 = ReplyGenerator(llm_client=_SeqLLM(['{"replies": ["@既梦 恭喜凑齐呀"], "chime_in": true}']))
    gen2._started_at = 0
    gen2.enable_skills = False
    gen2.enable_self_refine = False
    gen2.enable_react_tools = False
    gen2.enable_chime_in = True
    res2 = gen2.generate(
        [img_long, text_long],
        [img_long, text_long],
        is_group=True,
        chime_in_image_enricher=lambda c, t, u, a: 0,
    )
    assert res2 and "恭喜凑齐呀" in res2[0]

    # 3) 含空格昵称精准定位目标 + 记录 chime_in_image_enriched trace 事件
    img_a = _img_msg("Alice Bob", server_id="sp_1", chat_name="弓箭2战神公会")
    txt_a = ChatMessage(text="出货了", sender="Alice Bob", sender_type=SenderType.OTHER, chat_name="弓箭2战神公会")
    img_b = _img_msg("路人甲", server_id="sp_2", chat_name="弓箭2战神公会")
    seen_targets = []

    def _enrich_spaced(chat_name, target_msgs, unreplied, all_msgs):
        seen_targets.extend(m.sender for m in target_msgs)
        img_a.image_description = "神话武器出货界面"
        return 1

    gen3 = ReplyGenerator(
        llm_client=_SeqLLM([
            '{"replies": ["@Alice Bob 运气不错"], "chime_in": true}',
            '{"replies": ["@Alice Bob 神话武器直接出了 好欧"], "chime_in": true}',
        ])
    )
    gen3._started_at = 0
    gen3.enable_skills = False
    gen3.enable_self_refine = False
    gen3.enable_react_tools = False
    gen3.enable_chime_in = True
    res3 = gen3.generate(
        [img_a, txt_a, img_b],
        [img_a, txt_a, img_b],
        is_group=True,
        chime_in_image_enricher=_enrich_spaced,
    )
    assert set(seen_targets) == {"Alice Bob"}
    assert res3 and "神话武器" in res3[0]
    assert any(t.get("type") == "chime_in_image_enriched" for t in gen3.last_generation_trace)
