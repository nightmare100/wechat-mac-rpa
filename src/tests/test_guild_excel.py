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


def test_parse_edit_command_supports_power_score_units_and_validation():
    from src.bot.guild_excel import parse_edit_command

    req1 = parse_edit_command("请修改公会战报.xlsx 工作表:1队,把小明的战力改为27.16m")
    assert req1.filename == "公会战报.xlsx"
    assert req1.sheet == "1队"
    assert req1.nickname == "小明"
    assert req1.field == "power"
    assert req1.value == pytest.approx(27_160_000)

    req2 = parse_edit_command("请修改 公会战报 工作表：1队，把 战神小明 的 魔物分数 改为 861.39b")
    assert req2.filename == "公会战报.xlsx"
    assert req2.field == "score"
    assert req2.value == pytest.approx(861_390_000_000)

    req3 = parse_edit_command("请修改公会战报.xlsx,工作表:1队,把小明的魔物得分改为1.2万")
    assert req3.field == "score"
    assert req3.value == pytest.approx(12_000)

    req4 = parse_edit_command("请修改公会战报.xlsx 工作表:1队,把小明的魔物分数改为0")
    assert req4.value == 0.0

    req5 = parse_edit_command("请修改公会战报.xlsx 工作表:1队,把小明的魔物分数改为清空")
    assert req5.value is None

    assert parse_edit_command("请修改一下你的态度") is None

    with pytest.raises(GuildExcelError):
        parse_edit_command("请修改../bad.xlsx 工作表:1队,把小明的战力改为10m")
    with pytest.raises(GuildExcelError):
        parse_edit_command("请修改公会战报.xlsx 工作表:1队,把小明的战力改为abc")
    with pytest.raises(GuildExcelError):
        parse_edit_command("请修改公会战报.xlsx 工作表:1队,把小明的战力改为-5m")


def test_edit_workbook_fuzzy_match_and_resort(tmp_path):
    from openpyxl import Workbook, load_workbook
    from src.bot.guild_excel import edit_workbook_file, parse_edit_command

    path = tmp_path / "战报.xlsx"
    book = Workbook()
    ws = book.active
    ws.title = "1队"
    ws.append(("名称", "战力", "魔物得分"))
    ws.append(("战神_小明", "20.00m", "500.00b"))
    ws.append(("暗夜刺客", "18.00m", "300.00b"))
    ws.append(("星辰大海", "15.00m", None))
    ws.append(("战神_小红", "12.00m", "100.00b"))
    book.save(path)
    book.close()

    # 子串模糊匹配修改战力（不改变行序）
    sheet, name, old_val, new_val = edit_workbook_file(
        path, parse_edit_command("请修改战报.xlsx 工作表:1队,把刺客的战力改为2550万")
    )
    assert (sheet, name, old_val, new_val) == ("1队", "暗夜刺客", "18.00m", "25.50m")

    # 子序列模糊匹配修改魔物分数（从空改为 1.5t，自动排到第一行）
    sheet, name, old_val, new_val = edit_workbook_file(
        path, parse_edit_command("请修改战报.xlsx 工作表:1队,把星海的魔物分数改为1.5t")
    )
    assert (sheet, name, old_val, new_val) == ("1队", "星辰大海", "空", "1.50t")

    wb = load_workbook(path, read_only=True)
    try:
        assert list(wb["1队"].values) == [
            ("名称", "战力", "魔物得分"),
            ("星辰大海", "15.00m", "1.50t"),
            ("战神_小明", "20.00m", "500.00b"),
            ("暗夜刺客", "25.50m", "300.00b"),
            ("战神_小红", "12.00m", "100.00b"),
        ]
    finally:
        wb.close()

    # 模糊匹配命中多个成员时拒绝修改并列出候选
    with pytest.raises(GuildExcelError, match="战神_小明.*战神_小红"):
        edit_workbook_file(path, parse_edit_command("请修改战报.xlsx 工作表:1队,把战神的战力改为30m"))

    # 未匹配任何成员时报错
    with pytest.raises(GuildExcelError, match="未找到"):
        edit_workbook_file(path, parse_edit_command("请修改战报.xlsx 工作表:1队,把不存在的人的战力改为30m"))


def test_edit_command_flow_follows_delivery_policy(tmp_path):
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
        sheet.append(("战神_小明", "20.00m", "500.00b"))
        sheet.append(("战神_小红", "15.00m", "100.00b"))
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
                        "guild_excel_min_sheets": 2,
                        "guild_excel_target_chat": "弓箭2战神公会",
                        "guild_excel_target_message": "今日战报已更新！",
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
        # 1) 生成第 1 张工作表（进度 1/2，不发送文件）
        cmd1 = message(text="请读取小明的图片，工作表:1队，最终输出:战报.xlsx", message_type="text", chat_name="戰神管理群")
        flow.handle("戰神管理群", cmd1)
        flow.jobs[-1][1].result(timeout=5)
        flow.poll("戰神管理群")
        send_file.assert_not_called()

        # 2) 在仅 1 张表时修改小红魔物分数为 900b（沿用投递策略：进度 1/2，仍不发文件）
        edit1 = message(text="请修改战报.xlsx 工作表:1队,把小红的魔物分数改为900b", message_type="text", chat_name="戰神管理群")
        assert flow.handle("戰神管理群", edit1) == "收到，正在修改 Excel。"
        flow.jobs[-1][1].result(timeout=5)
        flow.poll("戰神管理群")
        send_file.assert_not_called()
        assert any("已修改（1队，已将「战神_小红」的魔物得分从 100.00b 改为 900.00b），当前进度 1/2。" in call.args[1] for call in send_text.call_args_list)

        # 3) 生成第 2 张工作表（凑满 2/2，发往目标群）
        cmd2 = message(text="请读取小明的图片，工作表:2队，最终输出:战报.xlsx", message_type="text", chat_name="戰神管理群")
        flow.handle("戰神管理群", cmd2)
        flow.jobs[-1][1].result(timeout=5)
        flow.poll("戰神管理群")
        assert send_file.call_count == 1

        # 4) 凑满后再修改 1队 战力，应直接按群策略重新投递到目标群
        edit2 = message(text="请修改战报.xlsx 工作表:1队,把小明的战力改为28.5m", message_type="text", chat_name="戰神管理群")
        flow.handle("戰神管理群", edit2)
        flow.jobs[-1][1].result(timeout=5)
        flow.poll("戰神管理群")
        assert send_file.call_count == 2
        target_chat, sent_path = send_file.call_args.args
        assert target_chat == "弓箭2战神公会"
        wb = load_workbook(sent_path, read_only=True)
        try:
            assert list(wb["1队"].values) == [
                ("名称", "战力", "魔物得分"),
                ("战神_小红", "15.00m", "900.00b"),
                ("战神_小明", "28.50m", "500.00b"),
            ]
        finally:
            wb.close()
    finally:
        flow.executor.shutdown()
