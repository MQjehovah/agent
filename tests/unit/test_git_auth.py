"""git HTTPS 凭据注入(utils/git_auth)单测。

覆盖: PAT / 账号口令两条路径、http→https 重写与 host 解析、特殊字符转义、
无凭据不改动、追加到既有 GIT_CONFIG_*、提交身份默认与覆盖。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from utils.git_auth import configure_git_from_env  # noqa: E402


def _pairs() -> list[tuple[str, str]]:
    count = int(os.environ.get("GIT_CONFIG_COUNT", "0") or "0")
    return [
        (os.environ.get(f"GIT_CONFIG_KEY_{i}", ""), os.environ.get(f"GIT_CONFIG_VALUE_{i}", ""))
        for i in range(count)
    ]


def _clear_creds(monkeypatch) -> None:
    for key in ("GITLAB_TOKEN", "GITLAB_USERNAME", "GITLAB_PASSWORD",
                "IT_SYSTEM_PASSWORD", "GIT_GITLAB_USER",
                "GIT_COMMIT_NAME", "GIT_COMMIT_EMAIL", "GIT_TERMINAL_PROMPT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "0")


def test_configure_with_pat_rewrites_http_and_https(monkeypatch):
    _clear_creds(monkeypatch)
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-abc")
    monkeypatch.setenv("GITLAB_URL", "https://gitlab.xzrobot.com")

    assert configure_git_from_env() is True
    assert os.environ["GIT_TERMINAL_PROMPT"] == "0"
    pairs = _pairs()
    assert ("url.https://oauth2:glpat-abc@gitlab.xzrobot.com/.insteadOf",
            "https://gitlab.xzrobot.com/") in pairs
    assert ("url.https://oauth2:glpat-abc@gitlab.xzrobot.com/.insteadOf",
            "http://gitlab.xzrobot.com/") in pairs
    assert ("user.name", "零号员工") in pairs
    assert ("user.email", "agent@xzrobot.com") in pairs


def test_configure_with_username_password_encodes_special_chars(monkeypatch):
    _clear_creds(monkeypatch)
    monkeypatch.setenv("GITLAB_USERNAME", "s_software")
    monkeypatch.setenv("GITLAB_PASSWORD", "p@ss/word:1")
    monkeypatch.setenv("GITLAB_URL", "http://gitlab.xzrobot.com")

    assert configure_git_from_env() is True
    # 特殊字符 URL 编码; host 从 http 基址解析, 结果一律走 https
    expected_key = "url.https://s_software:p%40ss%2Fword%3A1@gitlab.xzrobot.com/.insteadOf"
    pairs = _pairs()
    assert (expected_key, "http://gitlab.xzrobot.com/") in pairs
    assert (expected_key, "https://gitlab.xzrobot.com/") in pairs


def test_configure_no_credentials_is_noop(monkeypatch):
    _clear_creds(monkeypatch)
    assert configure_git_from_env() is False
    assert os.environ.get("GIT_CONFIG_COUNT") == "0"
    assert "GIT_TERMINAL_PROMPT" not in os.environ


def test_configure_appends_to_existing_git_config(monkeypatch):
    _clear_creds(monkeypatch)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "existing")
    monkeypatch.setenv("GITLAB_TOKEN", "tok")

    assert configure_git_from_env() is True
    assert int(os.environ["GIT_CONFIG_COUNT"]) == 5  # 1 既有 + 2 insteadOf + user.name/email
    assert (os.environ["GIT_CONFIG_KEY_0"], os.environ["GIT_CONFIG_VALUE_0"]) == ("user.name", "existing")


def test_configure_custom_identity_and_git_user(monkeypatch):
    _clear_creds(monkeypatch)
    monkeypatch.setenv("GITLAB_TOKEN", "tok")
    monkeypatch.setenv("GIT_GITLAB_USER", "bot-user")
    monkeypatch.setenv("GIT_COMMIT_NAME", "机器人")
    monkeypatch.setenv("GIT_COMMIT_EMAIL", "bot@xzrobot.com")

    assert configure_git_from_env() is True
    pairs = _pairs()
    assert ("url.https://bot-user:tok@gitlab.xzrobot.com/.insteadOf",
            "https://gitlab.xzrobot.com/") in pairs
    assert ("user.name", "机器人") in pairs
    assert ("user.email", "bot@xzrobot.com") in pairs
