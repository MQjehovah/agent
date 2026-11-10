"""DWS 统一权限模式：read/write/destructive × default/smart/auto/plan。"""

import pytest

from security.permissions.checker import PermissionChecker
from security.permissions.modes import PermissionMode
from security.permissions.rules import PermissionConfig


def _checker(mode):
    return PermissionChecker(PermissionConfig(mode=mode))


def _dws(mode):
    return {"command": "calendar event list", "mode": mode}


def test_classify_dws_modes():
    c = _checker(PermissionMode.DEFAULT)
    assert c.classify_access("dws", _dws("read")) == "read"
    assert c.classify_access("dws", _dws("write")) == "write"
    assert c.classify_access("dws", _dws("destructive")) == "write"
    assert c.classify_access("dws_schema", {"path": "calendar"}) == "read"


def test_default_asks_for_write_and_destructive():
    c = _checker(PermissionMode.DEFAULT)
    assert c.check("dws", _dws("read")).allowed
    assert c.check("dws", _dws("read")).reason == ""
    assert c.check("dws", _dws("write")).reason == "需要用户确认"
    assert c.check("dws", _dws("destructive")).reason == "需要用户确认"


def test_smart_only_asks_destructive():
    c = _checker(PermissionMode.SMART)
    assert c.check("dws", _dws("write")).reason == ""
    assert c.check("dws", _dws("destructive")).reason == "需要用户确认"


def test_auto_allows_everything():
    c = _checker(PermissionMode.AUTO)
    for mode in ("read", "write", "destructive"):
        result = c.check("dws", _dws(mode))
        assert result.allowed
        assert result.reason == ""


def test_plan_denies_write():
    c = _checker(PermissionMode.PLAN)
    assert c.check("dws", _dws("read")).allowed
    assert not c.check("dws", _dws("write")).allowed
    assert not c.check("dws", _dws("destructive")).allowed


def test_unknown_mode_is_conservative():
    c = _checker(PermissionMode.DEFAULT)
    assert c.classify_access("dws", {"mode": "weird"}) == "write"
    assert c.check("dws", {"mode": "weird"}).reason == "需要用户确认"
