"""敏感工具清单 — 群共享会话中命中清单工具的结果 = 敏感(集中读取一处)。

Phase2 语义：数字中台子代理查 ERP 库的 ``mysql_query`` MCP 三工具
(``list_tables`` / ``describe_table`` / ``execute_query``) 命中清单后，
其调用结果即视为敏感：群内不再群发最终文本，改由渠道层私聊送达提问者。

清单来源：config.json ``sensitive.tools``(settings 单例)，未配置时用本模块
默认清单。所有「工具名是否敏感」的判定统一走 :func:`is_sensitive_tool`。
"""

# 默认敏感工具清单：mysql_query MCP(数字中台子代理查 ERP rosiwit_erp_server)三工具。
DEFAULT_SENSITIVE_TOOLS: frozenset[str] = frozenset(
    {"list_tables", "describe_table", "execute_query"}
)

# settings 中的配置路径(列表)。
_SETTINGS_KEY = "sensitive.tools"


def get_sensitive_tools() -> frozenset[str]:
    """返回当前生效的敏感工具名集合。

    读取集中在 settings(config.json ``sensitive.tools``)；settings 未初始化
    或未配置该键时回退默认清单。不做进程级缓存：settings 为启动即建的单例，
    每次判定只是 dict 取值，成本可忽略且便于测试覆写。
    """
    try:
        from settings import get_settings
        cfg = get_settings().get(_SETTINGS_KEY)
    except Exception:
        cfg = None
    if cfg:
        return frozenset(str(t) for t in cfg)
    return DEFAULT_SENSITIVE_TOOLS


def is_sensitive_tool(name: str) -> bool:
    """工具名是否命中敏感清单(保守判定入口)。"""
    return name in get_sensitive_tools()
