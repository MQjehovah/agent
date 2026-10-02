"""兼容垫片：运行分发逻辑已迁至 `agent.kernel`。

保留此模块以兼容既有导入与测试对 `agent.runner.dispatch` 的 monkeypatch；
`core.run` 运行期 `from agent.runner import dispatch` 仍能取到（被 patch 后的）分发函数。
"""
from agent.kernel import _make_plan_confirmer, _maybe_apply_plan, dispatch  # noqa: F401

__all__ = ["dispatch", "_maybe_apply_plan", "_make_plan_confirmer"]
