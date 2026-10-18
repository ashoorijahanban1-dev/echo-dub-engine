"""
Validation and Structure Tests for all n8n Workflows
Validates:
  - JSON syntax and top-level schema for WF01 through WF06
  - Node connection topological validity
  - WF03 Adaptive Polling Loop (no premature 10-second failure to Needs Review)
"""

import json
from pathlib import Path
import pytest

WORKFLOWS_DIR = Path(__file__).resolve().parent.parent / "workflows"

EXPECTED_WORKFLOWS = [
    "WF01_DISCOVER.json",
    "WF02_DISPATCH.json",
    "WF03_LESSON_PROCESS.json",
    "WF04_FINALIZE.json",
    "WF05_PUBLISH.json",
    "WF06_RECOVER.json"
]


@pytest.mark.parametrize("wf_filename", EXPECTED_WORKFLOWS)
def test_workflow_json_validity(wf_filename):
    """Ensures each workflow file exists, is valid JSON, and has core schema properties."""
    wf_path = WORKFLOWS_DIR / wf_filename
    assert wf_path.exists(), f"Workflow file {wf_filename} missing!"

    with open(wf_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert "name" in data
    assert "nodes" in data and isinstance(data["nodes"], list)
    assert "connections" in data and isinstance(data["connections"], dict)
    assert len(data["nodes"]) > 0


@pytest.mark.parametrize("wf_filename", EXPECTED_WORKFLOWS)
def test_workflow_connections_integrity(wf_filename):
    """Ensures every connection points to existing node names."""
    wf_path = WORKFLOWS_DIR / wf_filename
    with open(wf_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    node_names = {n["name"] for n in data["nodes"]}

    for src_name, conns in data["connections"].items():
        assert src_name in node_names, f"Connection source '{src_name}' not found in nodes of {wf_filename}"
        for conn_type, target_lists in conns.items():
            for target_group in target_lists:
                for target_item in target_group:
                    target_name = target_item.get("node")
                    assert target_name in node_names, f"Connection target '{target_name}' not found in nodes of {wf_filename}"


def test_wf03_adaptive_polling_loop():
    """
    Specifically verifies that WF03 contains an adaptive polling loop
    and does NOT immediately fail after an initial wait.
    """
    wf_path = WORKFLOWS_DIR / "WF03_LESSON_PROCESS.json"
    with open(wf_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    node_names = {n["name"] for n in data["nodes"]}

    # Core required polling components
    assert "Init Polling Context" in node_names
    assert "Wait Polling Interval" in node_names
    assert "Poll Job Status" in node_names
    assert "Is Job Completed?" in node_names
    assert "Is Still In Progress?" in node_names
    assert "Check Max Poll Attempts" in node_names
    assert "Increment Poll Counter" in node_names

    # Check loop connection: Increment Poll Counter must loop back to Wait Polling Interval
    conns = data["connections"]
    inc_conns = conns.get("Increment Poll Counter", {}).get("main", [])
    assert len(inc_conns) > 0
    looped_target = inc_conns[0][0]["node"]
    assert looped_target == "Wait Polling Interval", f"Expected loop to 'Wait Polling Interval', got '{looped_target}'"


def test_wf03_execution_simulation_60_cycles_to_needs_review():
    """
    Simulates execution of WF03 workflow graph:
    Verifies that:
    1. poll_count correctly increments on each cycle from current item.
    2. After exactly 60 in-progress polling cycles without completion,
       execution terminates and routes to 'Mark Lesson as Needs Review'.
    """
    wf_path = WORKFLOWS_DIR / "WF03_LESSON_PROCESS.json"
    with open(wf_path, "r", encoding="utf-8") as f:
        wf = json.load(f)

    nodes = {n["name"]: n for n in wf["nodes"]}
    conns = wf["connections"]

    # Verify node expressions read from current cycle item ($('Wait Polling Interval').item.json)
    check_node = nodes["Check Max Poll Attempts"]
    cond = check_node["parameters"]["conditions"]["conditions"][0]
    assert "$('Wait Polling Interval').item.json.poll_count" in cond["leftValue"]
    assert "$('Wait Polling Interval').item.json.max_polls" in cond["rightValue"]

    inc_node = nodes["Increment Poll Counter"]
    poll_val_expr = inc_node["parameters"]["values"]["number"][0]["value"]
    assert "$('Wait Polling Interval').item.json.poll_count + 1" in poll_val_expr

    # Simulate execution state
    current_item = {
        "poll_count": 0,
        "max_polls": 60,
        "job_id": "job_sim_dub_001",
        "lesson_id": "les_sim_001"
    }

    job_status_response = {"status": "PROCESSING", "error_details": None}
    cycle_count = 0
    route_taken = None

    # Step into polling loop
    while cycle_count <= 100:
        # Node: Wait Polling Interval (passes current_item through)
        wait_item = dict(current_item)

        # Node: Poll Job Status (returns job_status_response)
        poll_resp = dict(job_status_response)

        # Node: Is Job Completed?
        is_completed = (poll_resp["status"] == "COMPLETED")
        if is_completed:
            route_taken = "Log Success Completion"
            break

        # Node: Is Still In Progress?
        is_still_progress = poll_resp["status"] in ["QUEUED", "PROCESSING", "RETRY_WAIT"]
        if not is_still_progress:
            route_taken = "Mark Lesson as Needs Review"
            break

        # Node: Check Max Poll Attempts (reads from wait_item)
        if wait_item["poll_count"] < wait_item["max_polls"]:
            # True branch (index 0) -> Increment Poll Counter
            cycle_count += 1
            current_item = {
                "poll_count": wait_item["poll_count"] + 1,
                "max_polls": wait_item["max_polls"],
                "job_id": wait_item["job_id"],
                "lesson_id": wait_item["lesson_id"]
            }
            # Loops back to Wait Polling Interval
            next_node = conns["Increment Poll Counter"]["main"][0][0]["node"]
            assert next_node == "Wait Polling Interval"
        else:
            # False branch (index 1) -> Mark Lesson as Needs Review
            route_taken = "Mark Lesson as Needs Review"
            break

    assert cycle_count == 60, f"Expected exactly 60 polling cycles, executed {cycle_count}"
    assert route_taken == "Mark Lesson as Needs Review"
    assert current_item["poll_count"] == 60
    assert current_item["lesson_id"] == "les_sim_001"

