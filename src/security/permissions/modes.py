from enum import Enum


class PermissionMode(Enum):
    DEFAULT = "default"   # 写操作/执行前需确认(每次询问)
    SMART = "smart"       # 仅危险/高危操作需确认(必要时询问)
    AUTO = "auto"         # 允许一切(沙箱/容器内使用, 完全访问)
    PLAN = "plan"         # 禁止所有写操作(只读模式)
