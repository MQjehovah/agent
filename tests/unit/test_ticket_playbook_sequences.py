"""工单剧本期望调用序列回归。"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

_SEQUENCES_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "config",
    "agents",
    "设备运维",
    "skills",
    "ticket-handling",
    "references",
    "playbook-call-sequences.json",
)

REQUIRED_PLAYBOOKS = {
    "playbook-crash",
    "playbook-offline",
    "playbook-locate",
    "playbook-dock",
    "playbook-resource",
    "playbook-evidence",
}


@pytest.fixture(scope="module")
def sequences():
    with open(_SEQUENCES_PATH, encoding="utf-8") as f:
        return json.load(f)


def test_all_playbooks_defined(sequences):
    assert set(sequences["playbooks"]) == REQUIRED_PLAYBOOKS


def test_failure_always_uses_evidence_collect(sequences):
    for name, pb in sequences["playbooks"].items():
        assert "device-evidence-collect" in pb["failure_skills"], name


def test_locate_forbids_control_side_effects(sequences):
    locate = sequences["playbooks"]["playbook-locate"]
    forbidden = set(locate["forbidden_tools"])
    assert "soft_restart" in forbidden
    assert "device_backward" in forbidden
    assert set(locate["allowed_tools"]).isdisjoint(forbidden)


def test_evidence_forbids_restart_and_backward(sequences):
    evidence = sequences["playbooks"]["playbook-evidence"]
    forbidden = set(evidence["forbidden_tools"])
    assert "soft_restart" in forbidden
    assert "device_backward" in forbidden
    assert "get_camera_image" in evidence["allowed_tools"]
    assert evidence.get("log_attachment_forbidden") is True
    assert set(evidence["allowed_tools"]).isdisjoint(forbidden)


def test_ticket_forbids_dingtalk(sequences):
    forbidden = set(sequences.get("ticket_forbidden_tools") or [])
    assert any(name.startswith("dingtalk_") for name in forbidden)
    for name, pb in sequences["playbooks"].items():
        allowed = set(pb.get("allowed_tools") or [])
        assert allowed.isdisjoint(forbidden), name
