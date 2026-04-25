import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from team.context import TeamContext


def test_add_and_get_results():
    ctx = TeamContext("团队A", "做一件事")
    ctx.add_node_result("n1", "成员A", "结果1")
    ctx.add_node_result("n2", "成员B", "结果2")

    member_b_context = ctx.get_context_for_member("成员B")
    assert "做一件事" in member_b_context
    assert "结果1" in member_b_context
    assert "结果2" in member_b_context


def test_messages_routing():
    ctx = TeamContext("团队A", "任务")
    ctx.add_message("成员A", "成员B", "请补充接口定义")
    ctx.add_message("成员C", "成员A", "接口已更新")

    member_b = ctx.get_context_for_member("成员B")
    assert "成员A" in member_b
    assert "请补充接口定义" in member_b

    member_a = ctx.get_context_for_member("成员A")
    assert "成员C" in member_a
    assert "接口已更新" in member_a
    assert "请补充接口定义" not in member_a


def test_leader_feedback():
    ctx = TeamContext("团队A", "任务")
    ctx.set_leader_feedback("需要修改模块划分")

    member_context = ctx.get_context_for_member("成员A")
    assert "需要修改模块划分" in member_context


def test_iteration_display():
    ctx = TeamContext("团队A", "任务", max_iterations=3)
    ctx.iteration = 2

    member_context = ctx.get_context_for_member("成员A")
    assert "第 2 轮" in member_context


def test_get_member_results():
    ctx = TeamContext("团队A", "任务")
    ctx.add_node_result("n1", "成员A", "结果1")
    ctx.add_node_result("n2", "成员A", "结果2")
    ctx.add_node_result("n3", "成员B", "结果3")

    assert ctx.get_member_results("成员A") == ["结果1", "结果2"]
    assert ctx.get_member_results("成员B") == ["结果3"]
    assert ctx.get_member_results("成员C") == []


def test_get_summary():
    ctx = TeamContext("团队A", "做大事")
    ctx.iteration = 1
    ctx.add_node_result("step1", "成员A", "产出A")

    summary = ctx.get_summary()
    assert "第 1 轮" in summary
    assert "step1" in summary
    assert "产出A" in summary
