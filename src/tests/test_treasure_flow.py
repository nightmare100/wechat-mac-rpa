"""藏宝图指令按人分开接收接下来的 3 张图片。"""
from pathlib import Path

from src.bot.treasure_flow import TreasureFlow
from src.models.base import ActionResult, ChatMessage, SenderType
from src.treasure_map.models import AddResult, MatchResult


class Library:
    def __init__(self, match_result=None, add_result=None):
        self.calls = []
        self.match_result = match_result or MatchResult("found", Path("images/found.png"), None)
        self.add_result = add_result or AddResult("added", Path("images/added/new.png"))

    def match(self, path):
        self.calls.append(("match", Path(path).name))
        return self.match_result

    def add(self, path):
        self.calls.append(("add", Path(path).name))
        return self.add_result


def message(local_id, text="", sender="甲", wxid="wx_a", kind="text", created=None):
    return ChatMessage(
        text=text, sender=sender, sender_type=SenderType.OTHER, chat_name="群",
        chatroom_id="room@chatroom", sender_wxid=wxid, message_type=kind,
        create_time=created if created is not None else local_id,
        local_id=local_id, server_id=str(local_id),
    )


def harness(tmp_path, library):
    sent = []
    marked = []

    def fetch(msg):
        path = tmp_path / f"{msg.local_id}.png"
        path.write_bytes(b"png")
        return path

    def send_text(chat, text):
        sent.append(("text", chat, text))
        return ActionResult(success=True, sent_text=text)

    def send_image(chat, path):
        sent.append(("image", chat, str(path)))
        return ActionResult(success=True)

    def mark(chat, msg, reply):
        marked.append((msg.local_id, reply))

    flow = TreasureFlow(library, fetch, send_text, send_image, mark)
    return flow, sent, marked


def test_match_takes_the_next_three_images_and_stops(tmp_path):
    library = Library()
    flow, sent, marked = harness(tmp_path, library)
    messages = [message(1, "藏宝图鉴别")] + [message(i, kind="image") for i in range(2, 6)]

    assert flow.handle("群", messages[:4])
    assert flow.handle("群", messages[4:]) is False
    assert library.calls == [("match", "2.png"), ("match", "3.png"), ("match", "4.png")]
    assert [item[0] for item in sent] == ["text", "image", "image", "image"]
    assert sent[0][2] == "收到，请发送接下来的 3 张藏宝图"
    assert [item[0] for item in marked] == [1, 2, 3, 4]


def test_two_users_keep_separate_counts(tmp_path):
    library = Library()
    flow, sent, marked = harness(tmp_path, library)
    flow.handle("群", [
        message(1, "藏宝图鉴别", sender="甲", wxid="wx_a"),
        message(2, "藏宝图添加", sender="乙", wxid="wx_b"),
        message(3, kind="image", sender="乙", wxid="wx_b"),
        message(4, kind="image", sender="甲", wxid="wx_a"),
    ])
    assert library.calls == [("add", "3.png"), ("match", "4.png")]
    assert flow.sessions[("room@chatroom", "wx_a")].remaining == 2
    assert flow.sessions[("room@chatroom", "wx_b")].remaining == 2
    assert flow.sessions[("room@chatroom", "wx_a")].mode == "match"
    assert flow.sessions[("room@chatroom", "wx_b")].mode == "add"


def test_images_before_the_command_and_stickers_do_not_count(tmp_path):
    library = Library()
    flow, _, marked = harness(tmp_path, library)
    assert flow.handle("群", [
        message(1, kind="image"),
        message(2, kind="sticker"),
        message(3, "@囡囡 藏宝图鉴别"),
        message(4, kind="sticker"),
        message(5, kind="image"),
    ])
    assert library.calls == [("match", "5.png")]
    assert [item[0] for item in marked] == [3, 5]
    assert flow.sessions[("room@chatroom", "wx_a")].remaining == 2


def test_a_new_command_restarts_only_that_user(tmp_path):
    library = Library()
    flow, _, _ = harness(tmp_path, library)
    flow.handle("群", [message(1, "藏宝图鉴别"), message(2, kind="image")])
    flow.handle("群", [message(3, "藏宝图鉴别"), message(4, kind="image")])
    assert library.calls == [("match", "2.png"), ("match", "4.png")]
    assert flow.sessions[("room@chatroom", "wx_a")].remaining == 2


def test_unmatched_result_sends_the_note_and_closest_image(tmp_path):
    library = Library(MatchResult("not_found", Path("images/closest.png"), None))
    flow, sent, marked = harness(tmp_path, library)
    flow.handle("群", [message(1, "藏宝图鉴别"), message(2, kind="image")])
    assert sent[1] == ("text", "群", "最相似，未匹配成功")
    assert sent[2][0] == "image" and sent[2][2].endswith("closest.png")
    assert marked[1][1] == "最相似，未匹配成功"


def test_add_reports_duplicate_without_writing_a_reply_image(tmp_path):
    library = Library(add_result=AddResult("duplicate", Path("images/old.png")))
    flow, sent, _ = harness(tmp_path, library)
    flow.handle("群", [message(1, "藏宝图添加"), message(2, kind="image")])
    assert library.calls == [("add", "2.png")]
    assert [item[0] for item in sent] == ["text", "text"]
    assert sent[1][2] == "已重复"


def test_failed_send_keeps_the_image_for_the_next_turn(tmp_path):
    library = Library()
    flow, sent, marked = harness(tmp_path, library)
    flow.send_image = lambda chat, path: ActionResult(success=False, error="发送失败")
    flow.handle("群", [message(1, "藏宝图鉴别"), message(2, kind="image")])
    assert marked == [(1, "收到，请发送接下来的 3 张藏宝图")]
    assert flow.sessions[("room@chatroom", "wx_a")].remaining == 3
    flow.send_image = lambda chat, path: ActionResult(success=True)
    flow.handle("群", [message(2, kind="image")])
    assert library.calls == [("match", "2.png"), ("match", "2.png")]
    assert flow.sessions[("room@chatroom", "wx_a")].remaining == 2
