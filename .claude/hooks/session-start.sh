#!/bin/bash
# SessionStart 钩子:只在 Claude Code on the web(远程容器)里装开发依赖,让单测/对账脚本开箱即跑。
# 本机(安琳原机)不执行,避免动本地环境。幂等、非交互。
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(pwd)}"

export PIP_ROOT_USER_ACTION=ignore
python3 -m pip install -q --disable-pip-version-check -r requirements-dev.txt

# 让 `python3 -m pytest` 与直接 import qtrade_agent 都能找到 src(pyproject 已配 pythonpath,这里给非 pytest 场景兜底)
echo "export PYTHONPATH=\"${CLAUDE_PROJECT_DIR:-$(pwd)}/src:\${PYTHONPATH:-}\"" >> "${CLAUDE_ENV_FILE:-/dev/null}"

echo "session-start: dev deps ready ($(python3 -m pytest --version 2>&1 | head -1))"
