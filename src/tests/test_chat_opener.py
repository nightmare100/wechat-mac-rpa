from src.action.chat_opener import title_matches


def test_exact_and_member_count_suffix():
    assert title_matches("弓箭2战神公会（140）", "弓箭2战神公会")
    assert title_matches("弓箭2战神公会 (140)", "弓箭2战神公会")


def test_ocr_drops_first_or_last_char():
    assert title_matches("箭2战神公会（140）", "弓箭2战神公会")
    assert title_matches("弓箭2战神公", "弓箭2战神公会")


def test_similar_group_not_matched():
    assert not title_matches("弓箭3战神公会", "弓箭2战神公会")
    assert not title_matches("箭3战神公会", "弓箭2战神公会")
    assert not title_matches("2战神公会", "弓箭2战神公会")
    assert not title_matches("弓箭2战神公会二群", "弓箭2战神公会")


def test_short_names_require_exact():
    assert not title_matches("试群", "测试群")
    assert not title_matches("", "测试群")
