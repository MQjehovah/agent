"""MCP env 占位符解析测试:真实凭证经进程环境注入子进程,配置文件只留 ${VAR}。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from mcps.manager import resolve_env_values


def test_resolve_env_values_expands_placeholder(monkeypatch):
    monkeypatch.setenv("SMTP_PASSWORD", "real-secret")
    resolved = resolve_env_values({
        "SMTP_PASSWORD": "${SMTP_PASSWORD}",
        "SMTP_HOST": "smtp.example.com",
    })
    assert resolved["SMTP_PASSWORD"] == "real-secret"
    assert resolved["SMTP_HOST"] == "smtp.example.com"


def test_resolve_env_values_drops_unset_placeholder(monkeypatch):
    monkeypatch.delenv("DEVICE_API_PASSWORD", raising=False)
    assert "DEVICE_API_PASSWORD" not in resolve_env_values({"DEVICE_API_PASSWORD": "${DEVICE_API_PASSWORD}"})


def test_resolve_env_values_ignores_non_string(monkeypatch):
    assert resolve_env_values({"PORT": 8080}) == {}
