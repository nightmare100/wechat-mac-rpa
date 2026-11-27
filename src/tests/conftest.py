import os

import pytest


@pytest.fixture(autouse=True)
def _identity_env_defaults(monkeypatch):
    """data/persona.md 用 {{BOT_NAME}} 等占位符；测试环境不读 .env，未设置时给默认值，避免 _system_prompt 报错。"""
    for key, value in {"BOT_NAME": "『宵桜』囡囡", "BOT_AT_KEYWORD": "囡囡", "BUDDY_NAME": "朧月", "BUDDY_MATCH": "朧月,胧月"}.items():
        if not os.environ.get(key):
            monkeypatch.setenv(key, value)
    # 避免深夜跑单测时默认主动插话静默时段导致既有 chime-in 用例受当前墙钟时间影响
    if "CHIME_IN_QUIET_HOURS" not in os.environ:
        monkeypatch.setenv("CHIME_IN_QUIET_HOURS", "")


def pytest_addoption(parser):
    parser.addoption("--run-api", action="store_true", default=False, help="Run with real API calls")
    parser.addoption("--n-runs", type=int, default=3, help="Number of runs per case")
