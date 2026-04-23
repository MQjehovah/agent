import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger("agent.autonomous.goal")


@dataclass
class PlanStep:
    plan_id: str
    task_description: str
    order: int
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    requires_confirmation: bool = False
    status: str = "pending"
    result: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "plan_id": self.plan_id,
            "task_description": self.task_description,
            "order": self.order,
            "requires_confirmation": self.requires_confirmation,
            "status": self.status,
            "result": self.result,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PlanStep":
        return cls(
            id=data.get("id", uuid.uuid4().hex[:12]),
            plan_id=data["plan_id"],
            task_description=data["task_description"],
            order=data["order"],
            requires_confirmation=data.get("requires_confirmation", False),
            status=data.get("status", "pending"),
            result=data.get("result"),
        )


@dataclass
class Plan:
    goal_id: str
    steps: list[PlanStep] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "draft"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "goal_id": self.goal_id,
            "steps": [s.to_dict() for s in self.steps],
            "status": self.status,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Plan":
        steps = [PlanStep.from_dict(s) for s in data.get("steps", [])]
        return cls(
            id=data.get("id", uuid.uuid4().hex[:12]),
            goal_id=data["goal_id"],
            steps=steps,
            status=data.get("status", "draft"),
            created_at=data.get("created_at", datetime.now().isoformat()),
        )


@dataclass
class Goal:
    title: str
    description: str = ""
    source: str = "user"
    priority: int = 3
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "pending"
    plan: Plan | None = None
    retry_count: int = 0
    max_retries: int = 3
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str | None = None
    completed_at: str | None = None


class GoalManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS autonomous_goals (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    source TEXT DEFAULT 'user',
                    status TEXT DEFAULT 'pending',
                    priority INTEGER DEFAULT 3,
                    plan_json TEXT,
                    retry_count INTEGER DEFAULT 0,
                    max_retries INTEGER DEFAULT 3,
                    created_at TEXT,
                    updated_at TEXT,
                    completed_at TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_goals_status ON autonomous_goals(status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_goals_priority ON autonomous_goals(priority DESC)"
            )
            conn.commit()

    def create_goal(
        self,
        title: str,
        description: str = "",
        source: str = "user",
        priority: int = 3,
    ) -> Goal:
        goal = Goal(
            title=title,
            description=description,
            source=source,
            priority=priority,
        )
        now = datetime.now().isoformat()
        goal.created_at = now
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO autonomous_goals
                    (id, title, description, source, status, priority,
                     retry_count, max_retries, created_at, updated_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    goal.id,
                    goal.title,
                    goal.description,
                    goal.source,
                    goal.status,
                    goal.priority,
                    goal.retry_count,
                    goal.max_retries,
                    goal.created_at,
                    goal.updated_at,
                    goal.completed_at,
                ),
            )
            conn.commit()
        return goal

    def get_goal(self, goal_id: str) -> Goal | None:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM autonomous_goals WHERE id = ?", (goal_id,)
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_goal(row)

    def get_goal_by_index(self, index: int) -> Goal | None:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM autonomous_goals ORDER BY created_at ASC"
            )
            rows = cursor.fetchall()
        if index < 1 or index > len(rows):
            return None
        return self._row_to_goal(rows[index - 1])

    def update_status(self, goal_id: str, status: str):
        now = datetime.now().isoformat()
        completed_at = now if status == "completed" else None
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE autonomous_goals
                SET status = ?, updated_at = ?, completed_at = COALESCE(?, completed_at)
                WHERE id = ?
                """,
                (status, now, completed_at, goal_id),
            )
            conn.commit()

    def increment_retry(self, goal_id: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE autonomous_goals
                SET retry_count = retry_count + 1, updated_at = ?
                WHERE id = ?
                """,
                (datetime.now().isoformat(), goal_id),
            )
            conn.commit()
            cursor = conn.execute(
                "SELECT retry_count FROM autonomous_goals WHERE id = ?",
                (goal_id,),
            )
            row = cursor.fetchone()
        return row[0] if row else 0

    def save_plan(self, goal_id: str, plan: Plan):
        plan_json = json.dumps(plan.to_dict(), ensure_ascii=False)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE autonomous_goals
                SET plan_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (plan_json, datetime.now().isoformat(), goal_id),
            )
            conn.commit()

    def list_goals(
        self, status: str | None = None, limit: int = 100
    ) -> list[Goal]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if status:
                cursor = conn.execute(
                    """
                    SELECT * FROM autonomous_goals
                    WHERE status = ?
                    ORDER BY priority DESC, created_at ASC
                    LIMIT ?
                    """,
                    (status, limit),
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT * FROM autonomous_goals
                    ORDER BY priority DESC, created_at ASC
                    LIMIT ?
                    """,
                    (limit,),
                )
            rows = cursor.fetchall()
        return [self._row_to_goal(r) for r in rows]

    def _row_to_goal(self, row: sqlite3.Row) -> Goal:
        plan = None
        plan_json = row["plan_json"]
        if plan_json:
            plan = Plan.from_dict(json.loads(plan_json))
        return Goal(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            source=row["source"],
            status=row["status"],
            priority=row["priority"],
            plan=plan,
            retry_count=row["retry_count"],
            max_retries=row["max_retries"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )
