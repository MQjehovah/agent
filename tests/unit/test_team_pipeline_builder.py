"""团队流水线构建测试: 精简三节点 lite 模式。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from team.pipeline_builder import LITE_PIPELINE, allowed_pipeline_roles, build_pipeline  # noqa: E402


def _members(*names):
    return {n: {"name": n} for n in names}


def test_lite_pipeline_three_nodes_with_review_feedback():
    pipeline = build_pipeline("实现一个功能", _members("软件架构师", "代码工程师", "测试工程师"),
                              mode="lite")
    assert [s["stage"] for s in pipeline] == ["planning", "development", "review"]
    assert [s["role"] for s in pipeline] == ["软件架构师", "代码工程师", "测试工程师"]
    review = pipeline[-1]
    assert review["feedback_to"] == "development"
    assert review["max_loops"] == 3
    assert pipeline[0]["deps"] == []
    assert pipeline[1]["deps"] == ["planning"]
    assert pipeline[2]["deps"] == ["development"]


def test_lite_pipeline_drops_missing_members_and_cleans_deps():
    # 缺少审核角色 → 只留规划+开发, 且悬空依赖被清理
    pipeline = build_pipeline("x", _members("软件架构师", "代码工程师"), mode="lite")
    assert [s["stage"] for s in pipeline] == ["planning", "development"]
    assert all(d in {"planning", "development"} for s in pipeline for d in s["deps"])


def test_lite_pipeline_ignores_extra_members():
    # 目录里仍存在其它角色(安全/DevOps/...)时, lite 也不会把它们排进流水线
    pipeline = build_pipeline(
        "x",
        _members("软件架构师", "代码工程师", "测试工程师", "安全审查师", "DevOps工程师", "文档专员"),
        mode="lite",
    )
    assert [s["stage"] for s in pipeline] == ["planning", "development", "review"]


def test_lite_pipeline_constant_is_three_stages():
    assert len(LITE_PIPELINE) == 3


def test_allowed_pipeline_roles():
    assert allowed_pipeline_roles(
        {"pipeline_roles": ["软件架构师", "代码工程师", "测试工程师"]}
    ) == {"软件架构师", "代码工程师", "测试工程师"}
    assert allowed_pipeline_roles({}) is None
    assert allowed_pipeline_roles({"pipeline_roles": []}) is None
    assert allowed_pipeline_roles({"pipeline_roles": "软件架构师"}) is None


def test_normalize_stages_drops_unknown_roles_and_cleans_deps():
    from team.orchestrator import TeamOrchestrator

    orch = TeamOrchestrator("t", {}, {"软件架构师": {}, "代码工程师": {}}, None, None)
    stages = [
        {"stage": "planning", "role": "软件架构师", "deps": []},
        {"stage": "x", "role": "安全审查师", "deps": ["planning"]},
        {"stage": "dev", "role": "代码工程师", "deps": ["planning", "x"]},
    ]
    out = orch._normalize_stages(stages)
    assert [s["stage"] for s in out] == ["planning", "dev"]
    assert out[1]["deps"] == ["planning"]
    # 原流水线常量不被就地修改
    assert stages[2]["deps"] == ["planning", "x"]
