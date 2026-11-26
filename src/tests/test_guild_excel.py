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
    req = parse_command("请读取小明的图片，工作表：11月21日，最终输出：战报")
    assert req.filename == "战报.xlsx"
    assert req.window_minutes == 10

    req_with_time = parse_command("请读取希。的图片，工作表:一队，最终输出:公会情报11-23.xlsx，时间:30")
    assert req_with_time.nickname == "希。"
    assert req_with_time.sheet == "一队"
    assert req_with_time.filename == "公会情报11-23.xlsx"
    assert req_with_time.window_minutes == 30

    assert parse_command("请读取希。的图片，工作表:一队，最终输出:公会情报11-23.xlsx, 时间：60分钟").window_minutes == 60
    assert parse_command("请读取希。的图片，工作表:一队，最终输出:公会情报11-23.xlsx，时间:15分").window_minutes == 15

    with pytest.raises(GuildExcelError):
        parse_command("请读取小明的图片，工作表:今日,最终输出:../战报.xlsx")
    with pytest.raises(GuildExcelError, match="时间参数"):
        parse_command("请读取希。的图片，工作表:一队，最终输出:公会情报11-23.xlsx，时间:0")
    with pytest.raises(GuildExcelError, match="时间参数"):
        parse_command("请读取希。的图片，工作表:一队，最终输出:公会情报11-23.xlsx，时间:abc")
    assert parse_command("你能帮我读取小明的图片吗") is None


def test_selects_recent_images_from_one_sender_in_current_group():
    command = message(create_time=2200, local_id=10)
    first, last = message(create_time=1600), message(create_time=2200, local_id=9)
    nine_minutes_before = message(create_time=1660, local_id=2)
    older_twenty_min = message(create_time=1000, local_id=1)
    other = [message(create_time=1599), message(create_time=2201), message(chatroom_id="other"),
             message(sender="小红"), message(create_time=2200, local_id=11)]
    assert select_images([last, *other, nine_minutes_before, first], command, "小明") == [
        first, nine_minutes_before, last,
    ]
    # 自定义 20 分钟窗口时可包含 20 分钟前的图片
    assert select_images([older_twenty_min, last], command, "小明", window_minutes=20) == [
        older_twenty_min, last,
    ]
    with pytest.raises(GuildExcelError, match="重名"):
        select_images([first, message(sender_wxid="wx_b", create_time=1700, local_id=2)], command, "小明")
    with pytest.raises(GuildExcelError, match="不要折叠发送图片.*等待 30 秒"):
        select_images([], command, "小明")


def test_workbook_uses_scoped_images_and_skill_exporter(tmp_path, monkeypatch):
    import json
    from io import BytesIO
    from openpyxl import load_workbook
    from PIL import Image
    from src.bot import guild_excel

    if not (guild_excel._SKILL / "scripts/export.py").is_file():
        pytest.skip("local guild-monster-excel skill is not installed")

    png = BytesIO()
    Image.new("RGB", (1080, 2400)).save(png, format="PNG")
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
    perception.recent_group_messages.assert_called_once_with("abc@chatroom", 500)
    assert [call.args[0] for call in perception.client.read_bytes.call_args_list] == ["/media/one", "/media/two"]
    workbook = load_workbook(output, read_only=True)
    try:
        assert list(workbook["今日"].values) == [("名称", "战力", "魔物得分"), ("队员", "2.00m", "3.00b")]
    finally:
        workbook.close()


def test_workbook_rejects_thumbnail_images_before_calling_codex(tmp_path, monkeypatch):
    from io import BytesIO
    from PIL import Image
    from src.bot import guild_excel

    # 边界：拦截缩略图与过低清整图，放行高清横向局部裁图（如 1180×260）
    assert guild_excel._is_low_res_image(210, 467) is True
    assert guild_excel._is_low_res_image(300, 700) is True
    assert guild_excel._is_low_res_image(1080, 150) is True
    assert guild_excel._is_low_res_image(1180, 260) is False
    assert guild_excel._is_low_res_image(1180, 964) is False
    assert guild_excel._is_low_res_image(400, 600) is False

    fake_skill = tmp_path / "skill"
    (fake_skill / "scripts").mkdir(parents=True)
    (fake_skill / "SKILL.md").write_text("# mock", encoding="utf-8")
    (fake_skill / "scripts" / "export.py").write_text("", encoding="utf-8")

    hd_buf, thumb_buf = BytesIO(), BytesIO()
    Image.new("RGB", (1280, 2844)).save(hd_buf, format="PNG")
    Image.new("RGB", (210, 467)).save(thumb_buf, format="JPEG")
    blobs = {"/media/hd": hd_buf.getvalue(), "/media/thumb1": thumb_buf.getvalue(), "/media/thumb2": thumb_buf.getvalue()}

    perception = Mock()
    perception.recent_group_messages.return_value = [
        message(local_id=1, media_url="/media/hd"),
        message(local_id=2, media_url="/media/thumb1"),
        message(local_id=3, media_url="/media/thumb2"),
    ]
    perception.client.read_bytes.side_effect = lambda url, max_bytes=None: blobs[url]
    request = parse_command("请读取小明的图片，工作表:一队，最终输出:战报.xlsx")
    command = message(create_time=1100, local_id=10)

    run_codex = Mock()
    monkeypatch.setattr(guild_excel, "_run_codex", run_codex)

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    with pytest.raises(GuildExcelError, match=r"2/3 张图片分辨率过低.*210×467.*机器人微信端点击加载原图"):
        guild_excel.create_workbook(request, command, perception, work_dir, skill=fake_skill)
    run_codex.assert_not_called()


def test_roster_overlap_blocks_garbled_sheet_from_publishing_and_delivery(tmp_path):
    import hashlib
    from openpyxl import Workbook
    from src.bot.guild_excel import GuildExcelFlow
    from src.models.base import ActionResult

    outbox = tmp_path / "outbox"
    group_key = hashlib.sha256(b"abc@chatroom").hexdigest()
    group_dir = tmp_path / "workbooks" / group_key
    group_dir.mkdir(parents=True)

    baseline_names = [
        "害群之马", "破军丶斩月", "花间灼晚意", "落落快退游了",
        "饿狼1", "既梦", "承汐待潮", "逆流oc",
        "希芜丶", "调味凉白开", "顾念鸭", "小铄哥哥",
    ]
    wb_prev = Workbook()
    ws_prev = wb_prev.active
    ws_prev.title = "1队"
    ws_prev.append(("名称", "战力", "魔物得分"))
    for idx, name in enumerate(baseline_names):
        ws_prev.append((name, f"{20 - idx * 0.5:.2f}m", f"{100 - idx:.2f}t"))
    wb_prev.save(group_dir / "公会情报11-24.xlsx")
    wb_prev.close()

    batches = {
        "坏图": [
            "荒野之马", "派军、新月", "花间灼镜意", "潘森快退游了",
            "银狼1", "既梦", "承汐待潮", "逆流oc",
            "希芜、", "调味凉白开", "顾念鸭", "小铄哥哥",
        ],
        "好图": [
            "害群之马", "破军、斩月", "花间灼晚意", "落落快退游了",
            "饿狼1", "既梦", "承汐待潮", "逆流oc",
            "希芜、", "调味凉白开", "顾念鸭", "新成员甲",
        ],
    }

    def export(request, command, perception, directory):
        path = directory / request.filename
        book = Workbook()
        ws = book.active
        ws.title = request.sheet
        ws.append(("名称", "战力", "魔物得分"))
        for name in batches[request.nickname]:
            ws.append((name, "20.00m", "10.00t"))
        book.save(path)
        book.close()
        return path

    send_text = Mock()
    send_file = Mock(return_value=ActionResult(success=True))
    flow = GuildExcelFlow(Mock(), send_text, send_file, worker=export, outbox=outbox)
    try:
        # 1) 重合率仅 7/12 (58%)，拦截写入与发送，管理群收到告警
        bad_cmd = message(
            text="请读取坏图的图片，工作表:一队，最终输出:公会情报11-25.xlsx",
            message_type="text",
            chat_name="戰神管理群",
        )
        flow.handle("戰神管理群", bad_cmd)
        with pytest.raises(GuildExcelError, match=r"重合率仅 58%.*荒野之马"):
            flow.jobs[-1][1].result(timeout=5)
        flow.poll("戰神管理群")
        send_file.assert_not_called()
        assert not (group_dir / "公会情报11-25.xlsx").exists()
        assert any("重合率仅 58%" in call.args[1] for call in send_text.call_args_list)

        # 2) 重合率 11/12 (92%，含 丶/、 符号归一)，正常写入并发送
        good_cmd = message(
            text="请读取好图的图片，工作表:一队，最终输出:公会情报11-25.xlsx",
            message_type="text",
            chat_name="戰神管理群",
        )
        flow.handle("戰神管理群", good_cmd)
        flow.jobs[-1][1].result(timeout=5)
        flow.poll("戰神管理群")
        send_file.assert_called_once()
        assert (group_dir / "公会情报11-25.xlsx").is_file()
    finally:
        flow.executor.shutdown()


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


def test_parse_merge_command():
    from src.bot.guild_excel import parse_merge_command

    req_range = parse_merge_command("请汇总11-21-11-23的工会魔物情报")
    assert req_range.start_date == (None, 11, 21)
    assert req_range.end_date == (None, 11, 23)
    assert req_range.filename == "公会魔物情报汇总_11-21_11-23.xlsx"

    req_cn = parse_merge_command("请汇总11月21日-11月23日的公会魔物情报，最终输出:周报.xlsx")
    assert req_cn.start_date == (None, 11, 21)
    assert req_cn.end_date == (None, 11, 23)
    assert req_cn.filename == "周报.xlsx"

    req_dot = parse_merge_command("请整合11.21至11.23的工会魔物情报")
    assert req_dot.start_date == (None, 11, 21)
    assert req_dot.end_date == (None, 11, 23)

    assert parse_merge_command("请汇总公会情报").filename == "公会情报汇总.xlsx"
    assert parse_merge_command("请整合公会情报").filename == "公会情报汇总.xlsx"
    assert parse_merge_command("请汇总Excel，最终输出:11月战报").filename == "11月战报.xlsx"
    assert parse_merge_command("请汇总一下大家的意见") is None
    with pytest.raises(GuildExcelError):
        parse_merge_command("请汇总公会情报，最终输出:../bad.xlsx")
    with pytest.raises(GuildExcelError, match="起始日期不能晚于结束日期"):
        parse_merge_command("请汇总11-23-11-21的工会魔物情报")
    with pytest.raises(GuildExcelError, match="不合法"):
        parse_merge_command("请汇总11-31-11-32的工会魔物情报")


def test_merge_command_flow_consolidates_group_workbooks_and_normalizes_ocr_symbols(tmp_path):
    import hashlib
    from openpyxl import Workbook, load_workbook
    from src.bot import guild_excel
    from src.bot.guild_excel import GuildExcelFlow
    from src.models.base import ActionResult

    if not (guild_excel._SKILL / "scripts" / "merge.py").is_file():
        pytest.skip("local guild-monster-excel merge script is not installed")

    outbox = tmp_path / "outbox"
    group_key = hashlib.sha256(b"abc@chatroom").hexdigest()
    group_dir = tmp_path / "workbooks" / group_key
    group_dir.mkdir(parents=True)

    # 创建三天的每日战报（11-21、11-22、11-24），测试只汇总 11-21-11-22 时自动过滤 11-24
    wb1 = Workbook()
    ws1 = wb1.active
    ws1.title = "1队"
    ws1.append(("名称", "战力", "魔物得分"))
    ws1.append(("破军、斩月", "24.20m", "150.00t"))
    ws1.append(("Kevin ノ LA", "11.30m", None))
    ws2 = wb1.create_sheet("2队")
    ws2.append(("名称", "战力", "魔物得分"))
    ws2.append(("我是牛大宝", "27.00m", "100.00t"))
    wb1.save(group_dir / "公会情报11-21.xlsx")
    wb1.close()

    wb2 = Workbook()
    ws1b = wb2.active
    ws1b.title = "一队"
    ws1b.append(("名称", "战力", "魔物得分"))
    ws1b.append(("破军丶斩月", "24.50m", "200.00t"))
    ws1b.append(("Kevin 丿 LA", "11.50m", "500.00b"))
    ws3b = wb2.create_sheet("三队")
    ws3b.append(("名称", "战力", "魔物得分"))
    ws3b.append(("汗津津的猪", "10.50m", "1.50t"))
    wb2.save(group_dir / "公会情报11-22.xlsx")
    wb2.close()

    wb3 = Workbook()
    ws1c = wb3.active
    ws1c.title = "一队"
    ws1c.append(("名称", "战力", "魔物得分"))
    ws1c.append(("破军、斩月", "99.00m", "999.00t"))
    wb3.save(group_dir / "公会情报11-24.xlsx")
    wb3.close()

    send_text = Mock()
    send_file = Mock(return_value=ActionResult(success=True))
    flow = GuildExcelFlow(Mock(), send_text, send_file, outbox=outbox)
    try:
        cmd = message(text="请汇总11-21-11-22的工会魔物情报", message_type="text", chat_name="戰神管理群")
        assert flow.handle("戰神管理群", cmd) == "收到，正在汇总本群的公会 Excel。"
        flow.jobs[-1][1].result(timeout=10)
        flow.poll("戰神管理群")

        send_file.assert_called_once()
        target_chat, sent_path = send_file.call_args.args
        assert target_chat == "戰神管理群"
        assert sent_path.name == "公会魔物情报汇总_11-21_11-22.xlsx"
        # 汇总文件不写入 workbooks 目录，避免污染每日战报
        assert not (group_dir / "公会魔物情报汇总_11-21_11-22.xlsx").exists()

        wb = load_workbook(sent_path, read_only=True)
        try:
            assert wb.sheetnames == ["一队", "二队", "三队", "人员变动"]
            assert list(wb["一队"].values) == [
                ("名称", "战力", "魔物得分(11-21)", "魔物得分(11-22)", "魔物总得分"),
                ("破军丶斩月", "24.50m", "150.00t", "200.00t", "350.00t"),
                ("Kevin 丿 LA", "11.50m", None, "500.00b", "500.00b"),
            ]
            assert list(wb["二队"].values) == [
                ("名称", "战力", "魔物得分(11-21)", "魔物得分(11-22)", "魔物总得分"),
                ("我是牛大宝", "27.00m", "100.00t", None, "100.00t"),
            ]
            assert list(wb["三队"].values) == [
                ("名称", "战力", "魔物得分(11-21)", "魔物得分(11-22)", "魔物总得分"),
                ("汗津津的猪", "10.50m", None, "1.50t", "1.50t"),
            ]
        finally:
            wb.close()
        assert any("已汇总 2 份文件（11-21、11-22）" in call.args[1] for call in send_text.call_args_list)

        # 指定范围内无文件时友好报错
        empty_cmd = message(text="请汇总10-01-10-05的工会魔物情报", message_type="text", chat_name="戰神管理群")
        flow.handle("戰神管理群", empty_cmd)
        with pytest.raises(GuildExcelError, match="未找到 10-01 至 10-05"):
            flow.jobs[-1][1].result(timeout=10)
    finally:
        flow.executor.shutdown()


def test_target_message_parts_combines_without_immediate_repeat(tmp_path):
    import json
    from src.bot.group_config import GroupConfig
    from src.bot.guild_excel import GuildExcelFlow

    part1 = [f"开头{i}，" for i in range(10)]
    part2 = [f"加油{i}！" for i in range(10)]
    cfg_path = tmp_path / "group_config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "groups": {
                    "戰神管理群": {
                        "guild_excel_min_sheets": 3,
                        "guild_excel_target_chat": "弓箭2战神公会",
                        "guild_excel_target_message": "兜底文案",
                        "guild_excel_target_message_parts": [part1, part2],
                    },
                    "旧配置群": {
                        "guild_excel_target_message": "仅单句文案",
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    flow = GuildExcelFlow(Mock(), Mock(), Mock(), outbox=tmp_path / "outbox", group_config=GroupConfig(cfg_path))
    try:
        prev = None
        for _ in range(10):
            _, target_chat, msg = flow._delivery_policy("戰神管理群")
            assert target_chat == "弓箭2战神公会"
            assert any(msg.startswith(p) for p in part1)
            assert any(msg.endswith(p) for p in part2)
            assert msg != prev
            prev = msg
        # 旧配置仍向后兼容
        assert flow._delivery_policy("旧配置群")[2] == "仅单句文案"
    finally:
        flow.executor.shutdown()
