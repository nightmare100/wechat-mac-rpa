#!/bin/bash
# 创建/更新 Python 虚拟环境（.venv），重复执行即为更新依赖
# 用法: scripts/setup_env.sh [--dev]
#   --dev  额外安装测试与代码质量依赖（requirements-dev.txt）
# 可用 PYTHON 环境变量指定解释器，如 PYTHON=/path/to/python3.11 scripts/setup_env.sh

set -euo pipefail

cd "$(dirname "$0")/.."

# 未指定时依次尝试 PATH 中的 python3.11、uv 管理的 3.11
PYTHON="${PYTHON:-$(command -v python3.11 || uv python find --system 3.11 2>/dev/null || echo python3.11)}"
REQ="requirements.txt"
[ "${1:-}" = "--dev" ] && REQ="requirements-dev.txt"

# numpy==1.24.3 / scikit-learn==1.2.1 在 3.12+ 无预编译包，固定 3.11
if ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 11))' 2>/dev/null; then
    echo "❌ 需要 Python 3.11（当前 PYTHON=${PYTHON}）"
    echo "   安装: brew install python@3.11、pyenv install 3.11 或 uv python install 3.11"
    exit 1
fi

if [ ! -x .venv/bin/python ]; then
    echo "▶ 创建虚拟环境 .venv"
    "$PYTHON" -m venv .venv
fi

echo "▶ 安装/更新依赖: $REQ"
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install --upgrade -r "$REQ"

# 系统依赖检查（不自动安装）
if ! command -v cliclick >/dev/null && [ ! -x /opt/homebrew/bin/cliclick ]; then
    echo "⚠️  未找到 cliclick，点击操作会失败: brew install cliclick"
fi

echo "✅ 完成。运行: .venv/bin/python run_bot.py"
