from src.models.base import ChatMessage, SenderType
from src.reply.generator import _apply_emoji_cooldown


def _self(text):
    return ChatMessage(text=text, sender="自己", sender_type=SenderType.SELF, chat_name="g")


def _other(text):
    return ChatMessage(text=text, sender="甲", sender_type=SenderType.OTHER, chat_name="g")


def test_strips_trailing_emoji_when_recent_self_has_emoji():
    history = [_self("@甲 嘿嘿 谢谢呀 🥰"), _other("@『宵桜』囡囡 哈哈")]
    assert _apply_emoji_cooldown(["@乙 哈哈 这图 😂"], history) == ["@乙 哈哈 这图"]


def test_keeps_emoji_after_three_plain_self_replies():
    history = [_self("@甲 好的 😊"), _self("在哒"), _self("嗨嗨"), _self("@所有人 提醒：做任务")]
    assert _apply_emoji_cooldown(["哈哈 😂"], history) == ["哈哈 😂"]


def test_cooldown_counts_only_last_three_self_messages():
    history = [_self("a 😊"), _self("b"), _self("c"), _other("x 😂")]
    assert _apply_emoji_cooldown(["d 🎉"], history) == ["d"]


def test_keeps_emoji_only_reply():
    history = [_self("嘿嘿 🥰")]
    assert _apply_emoji_cooldown(["@甲 🌹"], history) == ["@甲 🌹"]


def test_cooldown_applies_within_same_batch():
    assert _apply_emoji_cooldown(["哈哈 😂", "谢谢 🥰"], []) == ["哈哈 😂", "谢谢"]


def test_strips_zwj_and_variation_selector_sequences():
    history = [_self("好 👍")]
    assert _apply_emoji_cooldown(["没瓜 🤦‍♂️", "收到 ❤️"], history) == ["没瓜", "收到"]


from src.reply.generator import _apply_laugh_cooldown  # noqa: E402


def test_strips_laugh_opener_when_recent_self_starts_with_laugh():
    history = [_self("@甲 哈哈 别逗啦"), _other("@『宵桜』囡囡 哈哈")]
    assert _apply_laugh_cooldown(["@乙 嘿嘿 谢谢呀"], history) == ["@乙 谢谢呀"]


def test_keeps_laugh_after_three_self_replies_without_opener():
    history = [_self("@甲 哈哈 好"), _self("在哒"), _self("谢谢呀"), _self("@所有人 提醒：做任务")]
    assert _apply_laugh_cooldown(["@乙 哈哈 绕晕啦"], history) == ["@乙 哈哈 绕晕啦"]


def test_laugh_in_middle_does_not_count():
    history = [_self("@甲 笑死 哈哈")]
    assert _apply_laugh_cooldown(["@乙 哈哈 这图"], history) == ["@乙 哈哈 这图"]


def test_keeps_laugh_only_reply():
    history = [_self("嘿嘿 收到啦")]
    assert _apply_laugh_cooldown(["@甲 哈哈哈"], history) == ["@甲 哈哈哈"]


def test_laugh_cooldown_within_batch_and_punctuation():
    assert _apply_laugh_cooldown(["哈哈，好的", "嘿嘿！谢谢"], []) == ["哈哈，好的", "谢谢"]


from src.reply.generator import _apply_at_policy  # noqa: E402


def test_unknown_sender_at_always_removed():
    keep_all = lambda: 0.0  # noqa: E731
    assert _apply_at_policy(["@未知 哈哈 选不出来啦", "@群成员昵称 好哒"], rand=keep_all) == ["哈哈 选不出来啦", "好哒"]


def test_at_kept_when_random_below_rate():
    assert _apply_at_policy(["@相缺 谢谢呀"], keep_rate=0.25, rand=lambda: 0.1) == ["@相缺 谢谢呀"]


def test_at_removed_when_random_above_rate():
    assert _apply_at_policy(["@相缺 谢谢呀"], keep_rate=0.25, rand=lambda: 0.9) == ["谢谢呀"]


def test_at_only_reply_and_no_at_untouched():
    assert _apply_at_policy(["@相缺", "在哒"], rand=lambda: 0.9) == ["@相缺", "在哒"]


def test_at_keep_rate_roughly_quarter():
    import random as _r
    rng = _r.Random(0)
    out = _apply_at_policy(["@甲 好"] * 2000, rand=rng.random)
    kept = sum(o.startswith("@") for o in out) / 2000
    assert 0.2 < kept < 0.3
