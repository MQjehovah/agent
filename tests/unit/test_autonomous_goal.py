import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from autonomous.goal import Goal, GoalManager, Plan, PlanStep


def test_goal_creation():
    goal = Goal(title="测试目标", description="巡检所有设备")
    assert goal.status == "pending"
    assert goal.retry_count == 0
    assert goal.max_retries == 3
    assert goal.id
    assert goal.created_at


def test_plan_creation():
    plan = Plan(goal_id="g1")
    assert plan.status == "draft"
    assert plan.steps == []


def test_plan_step_creation():
    step = PlanStep(
        plan_id="p1",
        task_description="读取设备列表",
        order=1
    )
    assert step.status == "pending"
    assert not step.requires_confirmation


def test_goal_state_transitions():
    goal = Goal(title="test")
    assert goal.status == "pending"

    goal.status = "planning"
    goal.status = "executing"
    goal.status = "verifying"
    goal.status = "completed"


def test_goal_manager_create_and_get(tmp_path):
    db_path = str(tmp_path / "test.db")
    manager = GoalManager(db_path)

    goal = manager.create_goal(
        title="测试目标",
        description="巡检设备",
        source="dingtalk",
        priority=4
    )
    assert goal.id
    assert goal.status == "pending"

    fetched = manager.get_goal(goal.id)
    assert fetched is not None
    assert fetched.title == "测试目标"
    assert fetched.source == "dingtalk"


def test_goal_manager_update_status(tmp_path):
    db_path = str(tmp_path / "test.db")
    manager = GoalManager(db_path)

    goal = manager.create_goal(title="test", description="d", source="user")
    manager.update_status(goal.id, "planning")

    fetched = manager.get_goal(goal.id)
    assert fetched.status == "planning"


def test_goal_manager_list_pending(tmp_path):
    db_path = str(tmp_path / "test.db")
    manager = GoalManager(db_path)

    manager.create_goal(title="g1", description="d", source="user")
    manager.create_goal(title="g2", description="d", source="user")
    manager.create_goal(title="g3", description="d", source="user")
    manager.update_status(manager.get_goal_by_index(1).id, "completed")

    pending = manager.list_goals(status="pending")
    assert len(pending) == 2


def test_goal_manager_save_and_load_plan(tmp_path):
    db_path = str(tmp_path / "test.db")
    manager = GoalManager(db_path)

    goal = manager.create_goal(title="test", description="d", source="user")
    plan = Plan(
        goal_id=goal.id,
        steps=[
            PlanStep(plan_id="p1", task_description="步骤1", order=1),
            PlanStep(plan_id="p1", task_description="步骤2", order=2, requires_confirmation=True),
        ]
    )
    manager.save_plan(goal.id, plan)

    fetched = manager.get_goal(goal.id)
    assert fetched.plan is not None
    assert len(fetched.plan.steps) == 2
    assert fetched.plan.steps[1].requires_confirmation
