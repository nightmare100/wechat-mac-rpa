from dataclasses import replace
from unittest.mock import Mock

import pytest

from src.bot.guild_excel import GuildExcelError, parse_command, select_images
from src.models.base import ChatMessage, SenderType


def message(**fields):
    base = ChatMessage(text="", sender="小明", sender_type=SenderType.OTHER, chat_name="群",
                       chatroom_id="abc@chatroom", message_type="image", media_url="/media/img",
                       create_time=1000, local_id=1, sender_wxid="wx_a")
    return replace(base, **fields)


def test_command_is_exact_and_disallows_paths():
    assert parse_command("请读取小明的图片，工作表：11月21日，最终输出：战报").filename == "战报.xlsx"
    with pytest.raises(GuildExcelError):
        parse_command("请读取小明的图片，工作表:今日,最终输出:../战报.xlsx")
    assert parse_command("你能帮我读取小明的图片吗") is None


def test_selects_recent_images_from_one_sender_in_current_group():
    command = message(create_time=2200, local_id=10)
    first, last = message(create_time=1000), message(create_time=2200, local_id=9)
    nineteen_minutes_before = message(create_time=1060, local_id=2)
    other = [message(create_time=999), message(create_time=2201), message(chatroom_id="other"),
             message(sender="小红"), message(create_time=2200, local_id=11)]
    assert select_images([last, *other, nineteen_minutes_before, first], command, "小明") == [
        first, nineteen_minutes_before, last,
    ]
    with pytest.raises(GuildExcelError, match="重名"):
        select_images([first, message(sender_wxid="wx_b", local_id=2)], command, "小明")


def test_workbook_uses_scoped_images_and_skill_exporter(tmp_path, monkeypatch):
    import json
    from io import BytesIO
    from openpyxl import load_workbook
    from PIL import Image
    from src.bot import guild_excel

    if not (guild_excel._SKILL / "scripts/export.py").is_file():
        pytest.skip("local guild-monster-excel skill is not installed")

    png = BytesIO()
    Image.new("RGB", (30, 30)).save(png, format="PNG")
    perception = Mock()
    perception.recent_group_messages.return_value = [
        message(media_url="/media/one"), message(local_id=2, media_url="/media/two"),
        message(sender="其他群友", local_id=3, media_url="/media/other"),
    ]
    perception.client.read_bytes.return_value = png.getvalue()
    request = parse_command("请读取小明的图片，工作表:今日，最终输出:战报.xlsx")
    command = message(create_time=1100, local_id=10)

    def recognize(params, images, directory, skill):
        assert params == request
        assert [p.name for p in images] == ["image-01.png", "image-02.png"]
        records = directory / "recognition.json"
        records.write_text(json.dumps({
            "members": [{"name": "队员", "power": 2_000_000}],
            "scores": [{"name": "队员", "score": 3_000_000_000}],
        }), encoding="utf-8")
        return records

    monkeypatch.setattr(guild_excel, "_run_codex", recognize)
    output = guild_excel.create_workbook(request, command, perception, tmp_path)
    assert output.name == "战报.xlsx"
    perception.recent_group_messages.assert_called_once_with("abc@chatroom", -100)
    assert [call.args[0] for call in perception.client.read_bytes.call_args_list] == ["/media/one", "/media/two"]
    workbook = load_workbook(output, read_only=True)
    try:
        assert list(workbook["今日"].values) == [("名称", "战力", "魔物得分"), ("队员", "2.00m", "3.00b")]
    finally:
        workbook.close()


def test_background_job_only_delivers_to_origin_group(tmp_path):
    from src.bot.guild_excel import GuildExcelFlow
    from src.models.base import ActionResult

    sender = Mock(return_value=ActionResult(success=True))
    def worker(request, command, perception, directory):
        file = directory / request.filename
        file.write_bytes(b"exported workbook")
        return file

    outbox = tmp_path / "outbox"
    flow = GuildExcelFlow(Mock(), Mock(), sender, worker=worker, outbox=outbox)
    command = message(text="请读取小明的图片，工作表:今日，最终输出:工会情报11-21.xlsx",
                      message_type="text", create_time=1100)
    try:
        assert flow.handle("群", command) == "收到，正在识别图片并生成 Excel。"
        output = flow.jobs[0][1].result(timeout=5)
        flow.poll("别的群")
        sender.assert_not_called()
        flow.poll("群")
        sender.assert_called_once()
        chat_name, sent = sender.call_args.args
        assert chat_name == "群"
        assert sent.name == "工会情报11-21.xlsx"
        assert sent.parent.parent == outbox
        assert sent.read_bytes() == b"exported workbook"
        assert sent == output
    finally:
        flow.executor.shutdown()


def test_same_file_accumulates_queued_sheets_and_survives_restart(tmp_path):
    from openpyxl import Workbook, load_workbook
    from src.bot.guild_excel import GuildExcelFlow
    from src.models.base import ActionResult

    def export(request, command, perception, directory):
        path = directory / request.filename
        if path.exists():
            book = load_workbook(path)
        else:
            book = Workbook()
            book.remove(book.active)
        book.create_sheet(request.sheet)
        book.save(path)
        book.close()
        if request.sheet == "导出失败":
            raise GuildExcelError("模拟导出失败")
        return path

    def submit(flow, nickname, sheet, group_id="abc@chatroom", chat_name="群"):
        command = message(
            text=f"请读取{nickname}的图片，工作表:{sheet}，最终输出:工会情报.xlsx",
            message_type="text", chatroom_id=group_id, chat_name=chat_name,
        )
        return flow.handle(chat_name, command)

    def sheets(path):
        book = load_workbook(path, read_only=True)
        try:
            return book.sheetnames
        finally:
            book.close()

    def new_flow():
        return GuildExcelFlow(Mock(), Mock(), Mock(return_value=ActionResult(success=True)),
                              worker=export, outbox=tmp_path / "outbox")

    flow = new_flow()
    try:
        submit(flow, "小明", "1队")
        assert "排队" in submit(flow, "小红", "2队")
        first, second = [future.result(timeout=5) for _, future in flow.jobs]
        assert sheets(first) == ["1队"]
        assert sheets(second) == ["1队", "2队"]
    finally:
        flow.executor.shutdown()

    restarted = new_flow()
    try:
        submit(restarted, "小李", "导出失败")
        with pytest.raises(GuildExcelError, match="模拟导出失败"):
            restarted.jobs[0][1].result(timeout=5)
        submit(restarted, "小李", "3队")
        submit(restarted, "小赵", "其他公会", group_id="def@chatroom", chat_name="另一个群")
        third, other_group = [future.result(timeout=5) for _, future in restarted.jobs[1:]]
        assert sheets(third) == ["1队", "2队", "3队"]
        assert sheets(other_group) == ["其他公会"]
        assert sheets(first) == ["1队"]
    finally:
        restarted.executor.shutdown()


def test_waits_for_min_sheets_and_delivers_to_target_group(tmp_path):
    import json
    from openpyxl import Workbook, load_workbook
    from src.bot.group_config import GroupConfig
    from src.bot.guild_excel import GuildExcelFlow
    from src.models.base import ActionResult

    def export(request, command, perception, directory):
        path = directory / request.filename
        if path.exists():
            book = load_workbook(path)
        else:
            book = Workbook()
            book.remove(book.active)
        sheet = book.create_sheet(request.sheet)
        sheet.append(("名称", "战力", "魔物得分"))
        sheet.append(("甲", "10.00m", "1.00t"))
        sheet.append(("乙", "8.00m", None))
        book.save(path)
        book.close()
        return path

    cfg_path = tmp_path / "group_config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "groups": {
                    "戰神管理群": {
                        "guild_excel": True,
                        "guild_excel_min_sheets": 3,
                        "guild_excel_target_chat": "弓箭2战神公会",
                        "guild_excel_target_message": "大家辛苦啦，这是今日的魔物成绩单，请查收。大家要加油哦，我们都是战神！",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    send_text = Mock()
    send_file = Mock(return_value=ActionResult(success=True))
    flow = GuildExcelFlow(
        Mock(), send_text, send_file, worker=export, outbox=tmp_path / "outbox", group_config=GroupConfig(cfg_path)
    )
    try:
        # 写入 1队 → 2队 → 重跑覆盖 2队（不应生成 2队_2 凑满 3 张）→ 写入 3队
        for sheet_name in ("1队", "2队", "2队", "3队"):
            cmd = message(
                text=f"请读取小明的图片，工作表:{sheet_name}，最终输出:战报.xlsx",
                message_type="text",
                chat_name="戰神管理群",
            )
            flow.handle("戰神管理群", cmd)
            flow.jobs[-1][1].result(timeout=5)
            flow.poll("戰神管理群")

        # 重跑 2队 覆盖原表，只有写入 3队 真正凑齐 3 张 sheet 后才发往 弓箭2战神公会
        send_file.assert_called_once()
        target_chat, sent_path = send_file.call_args.args
        assert target_chat == "弓箭2战神公会"
        assert sent_path.name == "战报.xlsx"
        wb = load_workbook(sent_path, read_only=True)
        try:
            assert wb.sheetnames == ["1队", "2队", "3队"]
        finally:
            wb.close()

        texts = [call.args for call in send_text.call_args_list]
        assert ("戰神管理群", "已写入（1队，共2人/匹配1人），当前进度 1/3。") in texts
        assert ("戰神管理群", "已写入（2队，共2人/匹配1人），当前进度 2/3。") in texts
        assert ("弓箭2战神公会", "大家辛苦啦，这是今日的魔物成绩单，请查收。大家要加油哦，我们都是战神！") in texts
        assert ("戰神管理群", "已写入（3队，共2人/匹配1人），Excel 已发送到「弓箭2战神公会」。") in texts
    finally:
        flow.executor.shutdown()
