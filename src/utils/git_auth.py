"""按环境变量为 git 注入 HTTPS 凭据(不落盘), 供 shell/terminal 克隆与推送。

背景: 内置 ``git`` 工具只做本地仓库操作, 克隆/推送靠 ``shell`` 跑 ``git clone``/
``git push``; 但容器内没有任何 git 凭据, 非交互 shell 会卡在账号密码提示。
本模块在启动时从环境变量读取 GitLab 凭据, 通过 ``GIT_CONFIG_*`` 环境配置把
``http(s)://<host>/`` 重写为带凭据的 HTTPS 地址(不写 ~/.gitconfig), 并关闭交互提示,
使 agent 能自动完成克隆与推送。

环境变量:
- ``GITLAB_TOKEN``        Personal Access Token(推荐, scope 需含 read_repository+write_repository)
- 或 ``GITLAB_USERNAME`` + ``GITLAB_PASSWORD``(或回退 ``IT_SYSTEM_PASSWORD``) 走账号口令
- ``GITLAB_URL``          默认 ``https://gitlab.xzrobot.com``
- ``GIT_GITLAB_USER``     git 认证用户名覆盖(默认 PAT 用 oauth2; 口令时用 GITLAB_USERNAME)
- ``GIT_COMMIT_NAME`` / ``GIT_COMMIT_EMAIL``  提交身份(默认「零号员工」/agent@xzrobot.com)
"""
import logging
import os
from urllib.parse import quote

logger = logging.getLogger("agent.git_auth")

DEFAULT_GITLAB_URL = "https://gitlab.xzrobot.com"
DEFAULT_COMMIT_NAME = "零号员工"
DEFAULT_COMMIT_EMAIL = "agent@xzrobot.com"


def _host_of(base_url: str) -> str:
    """取出 URL 的 host[:port] 部分(去掉 scheme 与尾部斜杠)。"""
    text = (base_url or "").strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    return text.strip().strip("/")


def _append_git_config(entries: list[tuple[str, str]]) -> None:
    """把配置项追加进 ``GIT_CONFIG_*`` 环境(git 会当作用环境提供的 config 读取)。"""
    try:
        count = int(os.environ.get("GIT_CONFIG_COUNT", "0") or "0")
    except (TypeError, ValueError):
        count = 0
    for key, value in entries:
        os.environ[f"GIT_CONFIG_KEY_{count}"] = key
        os.environ[f"GIT_CONFIG_VALUE_{count}"] = value
        count += 1
    os.environ["GIT_CONFIG_COUNT"] = str(count)


def configure_git_from_env() -> bool:
    """从环境变量装配 git HTTPS 凭据; 无凭据时返回 False(不改动任何配置)。"""
    token = (os.environ.get("GITLAB_TOKEN") or "").strip()
    username = (os.environ.get("GITLAB_USERNAME") or "").strip()
    password = (os.environ.get("GITLAB_PASSWORD")
                or os.environ.get("IT_SYSTEM_PASSWORD") or "").strip()

    if token:
        auth_user = (os.environ.get("GIT_GITLAB_USER") or "oauth2").strip() or "oauth2"
        auth_secret = token
        method = "PAT"
    elif username and password:
        auth_user = (os.environ.get("GIT_GITLAB_USER") or username).strip() or username
        auth_secret = password
        method = "口令"
    else:
        return False

    host = _host_of(os.environ.get("GITLAB_URL") or DEFAULT_GITLAB_URL)
    if not host:
        return False
    userinfo = f"{quote(auth_user, safe='')}:{quote(auth_secret, safe='')}"
    authed_base = f"https://{userinfo}@{host}/"

    entries: list[tuple[str, str]] = []
    # http 与 https 两种原始地址都重写到带凭据的 https(顺带把 http 升级为 https)
    for scheme in ("https", "http"):
        entries.append((f"url.{authed_base}.insteadOf", f"{scheme}://{host}/"))
    name = (os.environ.get("GIT_COMMIT_NAME") or DEFAULT_COMMIT_NAME).strip() or DEFAULT_COMMIT_NAME
    email = (os.environ.get("GIT_COMMIT_EMAIL") or DEFAULT_COMMIT_EMAIL).strip() or DEFAULT_COMMIT_EMAIL
    entries.append(("user.name", name))
    entries.append(("user.email", email))

    _append_git_config(entries)
    # 非交互: 凭据不可用时快速失败, 不卡在密码提示
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    logger.info(f"git HTTPS 凭据已注入(host={host}, user={auth_user}, 方式={method})")
    return True
