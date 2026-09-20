"""env_guard 单元测试:生产拒绝弱值/空值,开发放行并告警。"""
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from utils.env_guard import WEAK_VALUES, is_production, require_secret


def test_production_rejects_weak_value(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        require_secret("JWT_SECRET", "change-me-in-production")


def test_production_rejects_empty_value(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(RuntimeError, match="AGENT_ADMIN_PASSWORD"):
        require_secret("AGENT_ADMIN_PASSWORD", "")


def test_production_rejects_trimmed_weak_value(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(RuntimeError, match="AGENT_ADMIN_PASSWORD"):
        require_secret("AGENT_ADMIN_PASSWORD", "  admin123  ")


def test_production_returns_strong_value(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    assert require_secret("JWT_SECRET", "a-very-long-random-secret") == "a-very-long-random-secret"


def test_development_allows_weak_value_and_warns(monkeypatch, caplog):
    monkeypatch.setenv("APP_ENV", "development")
    with caplog.at_level(logging.WARNING):
        assert require_secret("JWT_SECRET", "change-me-in-production") == "change-me-in-production"
    assert any("JWT_SECRET" in record.getMessage() for record in caplog.records)


def test_is_production_recognizes_names(monkeypatch):
    for value in ("production", "PROD", "Prod"):
        monkeypatch.setenv("APP_ENV", value)
        assert is_production() is True
    monkeypatch.setenv("APP_ENV", "development")
    assert is_production() is False
    monkeypatch.delenv("APP_ENV", raising=False)
    assert is_production() is False


def test_weak_values_contains_known_defaults():
    for weak in ("change-me-in-production", "xzyz2022!", "123456", "default-key", "admin123"):
        assert weak in WEAK_VALUES
