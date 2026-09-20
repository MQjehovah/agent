"""环境分级的安全配置守卫。

生产(APP_ENV=production/prod)下,弱值或缺失的秘密必须让进程启动失败;
开发下放行并打印告警。跨仓库保持同样的语义与弱值清单。
"""
import logging
import os

WEAK_VALUES = {
    "change-me-in-production",
    "dev-secret-change-me-please-32-bytes-minimum",
    "default-secret",
    "default-key",
    "xzyz2022!",
    "admin123",
    "123456",
    "change-me",
    "gateway-secret",
    "agent-secret",
    "your-secret-key",
}

_PRODUCTION_NAMES = {"production", "prod"}

logger = logging.getLogger(__name__)


def is_production() -> bool:
    return os.environ.get("APP_ENV", "development").strip().lower() in _PRODUCTION_NAMES


def require_secret(name: str, value: str | None, weak_values: set[str] = WEAK_VALUES) -> str | None:
    """校验秘密类配置:生产拒绝弱值/空值,开发放行并告警。"""
    normalized = "" if value is None else str(value).strip()
    bad = (normalized == "") or (normalized in weak_values)
    if not bad:
        return value
    if is_production():
        raise RuntimeError(
            f"环境变量 {name} 未配置或仍为不安全的默认值,请参考 .env.example 设置"
        )
    logger.warning("%s 使用默认/弱值;生产环境(APP_ENV=production)将拒绝启动", name)
    return value
