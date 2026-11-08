from src.models.base import ChatMessage, SenderType
from src.reply.generator import FollowupTracker, _same_person


def _m(sender, text, quoted=""):
    return ChatMessage(text=text, sender=sender, sender_type=SenderType.OTHER, chat_name="g", quoted_text=quoted)


def _tracker(rounds=3, clock=None):
    clock = clock or [1000.0]
    return FollowupTracker(randint=lambda a, b: rounds, now=lambda: clock[0]), clock


def test_same_person_tolerates_one_ocr_char():
    assert _same_person("Mr乂朧月", "Mrㄨ朧月")
    assert not _same_person("相缺", "相见")  # 两字昵称不做模糊
    assert not _same_person("饿狼1", "许愿大王")


def test_at_opens_thread_and_followups_consume_rounds():
    t, _ = _tracker(rounds=2)
    t.record("g", [_m("相缺", "@『宵桜』囡囡 宝贝，叫哥哥")], ["@相缺 别逗啦"])
    assert t.candidates("g") == [("相缺", 2)]
    t.record("g", [_m("相缺", "那你叫我宝贝")], ["@相缺 别闹啦"])
    assert t.candidates("g") == [("相缺", 1)]
    t.record("g", [_m("相缺", "就叫一声嘛")], ["@相缺 不叫哦"])
    assert t.candidates("g") == []


def test_new_at_resets_rounds():
    t, _ = _tracker(rounds=2)
    t.record("g", [_m("相缺", "@囡囡 在吗")], ["@相缺 在哒"])
    t.record("g", [_m("相缺", "干嘛呢")], ["@相缺 发呆呢"])
    t.record("g", [_m("相缺", "@囡囡 又来了")], ["@相缺 来啦"])
    assert t.candidates("g") == [("相缺", 2)]


def test_quote_opens_thread_and_ocr_variant_counts_as_same_person():
    t, _ = _tracker(rounds=3)
    t.record("g", [_m("Mr乂朧月", "你说对么", quoted="『宵桜』囡囡: 来啦")], ["@Mr乂朧月 对哒"])
    t.record("g", [_m("Mrㄨ朧月", "真棒")], ["@Mrㄨ朧月 嘿嘿"])
    assert t.candidates("g") == [("Mr乂朧月", 2)]


def test_reply_without_at_uses_single_sender_and_unknown_ignored():
    t, _ = _tracker(rounds=3)
    t.record("g", [_m("饿狼1", "@囡囡 早")], ["早呀"])
    t.record("g", [_m("未知", "@囡囡 选一个")], ["@未知 选不出来啦"])
    assert t.candidates("g") == [("饿狼1", 3)]


def test_followup_without_open_thread_does_nothing_and_ttl_expires():
    t, clock = _tracker(rounds=3)
    t.record("g", [_m("相缺", "随便聊聊")], ["@相缺 嗯嗯"])
    assert t.candidates("g") == []
    t.record("g", [_m("相缺", "@囡囡 在吗")], ["@相缺 在哒"])
    clock[0] += 1801
    assert t.candidates("g") == []


def test_rounds_are_random_between_3_and_5():
    import random
    rng = random.Random(1)
    t = FollowupTracker(randint=rng.randint)
    seen = set()
    for i in range(200):
        t.record("g", [_m(f"人{i:03d}", "@囡囡 hi")], [f"@人{i:03d} 嗨"])
    seen = {r for _, r in t.candidates("g")}
    assert seen == {3, 4, 5}
