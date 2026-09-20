"""mcp_server 侧复用 agent 的统一环境守卫。

mcp_server 与 agent/src 同仓库发布, 这里直接复用上层实现, 保证弱值清单与
`APP_ENV` 分级语义和 agent 主程序完全一致, 避免两处清单漂移。
"""
import os
import sys

_AGENT_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _AGENT_SRC not in sys.path:
    sys.path.insert(0, _AGENT_SRC)

from utils.env_guard import WEAK_VALUES, is_production, require_secret  # noqa: E402,F401
