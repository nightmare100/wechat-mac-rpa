from src.utils.screenshot_store import ScreenshotStore


def _png(path, data):
    path.write_bytes(data)
    return str(path)


def test_identical_screenshot_not_saved_twice(tmp_path):
    store = ScreenshotStore(tmp_path / "shots")
    first = store.save(_png(tmp_path / "a.png", b"same"), "1")
    second = store.save(_png(tmp_path / "b.png", b"same"), "2")
    assert second == first
    assert len(list((tmp_path / "shots").iterdir())) == 1


def test_changed_screenshot_saved(tmp_path):
    store = ScreenshotStore(tmp_path / "shots")
    first = store.save(_png(tmp_path / "a.png", b"one"), "1")
    second = store.save(_png(tmp_path / "b.png", b"two"), "2")
    third = store.save(_png(tmp_path / "c.png", b"one"), "3")  # 只和上一张比，变回去要重新保存
    assert len({first, second, third}) == 3
    assert len(list((tmp_path / "shots").iterdir())) == 3


def test_resaves_when_previous_file_deleted(tmp_path):
    store = ScreenshotStore(tmp_path / "shots")
    first = store.save(_png(tmp_path / "a.png", b"same"), "1")
    (tmp_path / "shots" / first.rsplit("/", 1)[1]).unlink()
    second = store.save(_png(tmp_path / "b.png", b"same"), "2")
    assert second != first
    assert len(list((tmp_path / "shots").iterdir())) == 1
