from enum import Enum


class PermissionMode(Enum):
    DEFAULT = "default"   # 写操作/执行前需确认
    AUTO = "auto"         # 允许一切（沙箱/容器环境）
    PLAN = "plan"         # 禁止所有写操作（只读模式）
