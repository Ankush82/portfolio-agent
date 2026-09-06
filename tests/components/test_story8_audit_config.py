"""QA tests for STORY-8: Add audit configuration to config.py.

Acceptance criteria:
  AC1: config.py includes all 4 new config values with sensible defaults
  AC2: DefaultAuditReader uses AUDIT_MAX_QUERY_LIMIT and AUDIT_DEFAULT_LIMIT
  AC3: Config follows existing module-level constants with type-annotations pattern
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path


# ---------------------------------------------------------------------------
# AC1: config.py includes all 4 audit config values with sensible defaults
# ---------------------------------------------------------------------------


def test_story8_audit_config_values_exist_and_have_sensible_defaults():
    """AC1: config.py must define all four audit settings with values that
    match the story's specification."""
    from src import config

    assert hasattr(config, "AUDIT_TABLE_NAME"), "AUDIT_TABLE_NAME missing from config"
    assert config.AUDIT_TABLE_NAME == "audit_events", (
        f"AUDIT_TABLE_NAME={config.AUDIT_TABLE_NAME!r}, expected 'audit_events'"
    )

    assert hasattr(config, "AUDIT_MAX_QUERY_LIMIT"), "AUDIT_MAX_QUERY_LIMIT missing from config"
    assert isinstance(config.AUDIT_MAX_QUERY_LIMIT, int), (
        f"AUDIT_MAX_QUERY_LIMIT={config.AUDIT_MAX_QUERY_LIMIT!r} is not an int"
    )
    assert config.AUDIT_MAX_QUERY_LIMIT == 1000, (
        f"AUDIT_MAX_QUERY_LIMIT={config.AUDIT_MAX_QUERY_LIMIT}, expected 1000"
    )

    assert hasattr(config, "AUDIT_DEFAULT_LIMIT"), "AUDIT_DEFAULT_LIMIT missing from config"
    assert isinstance(config.AUDIT_DEFAULT_LIMIT, int), (
        f"AUDIT_DEFAULT_LIMIT={config.AUDIT_DEFAULT_LIMIT!r} is not an int"
    )
    assert config.AUDIT_DEFAULT_LIMIT == 100, (
        f"AUDIT_DEFAULT_LIMIT={config.AUDIT_DEFAULT_LIMIT}, expected 100"
    )

    assert hasattr(config, "AUDIT_RETENTION_DAYS"), "AUDIT_RETENTION_DAYS missing from config"
    assert isinstance(config.AUDIT_RETENTION_DAYS, int), (
        f"AUDIT_RETENTION_DAYS={config.AUDIT_RETENTION_DAYS!r} is not an int"
    )
    assert config.AUDIT_RETENTION_DAYS == 365, (
        f"AUDIT_RETENTION_DAYS={config.AUDIT_RETENTION_DAYS}, expected 365"
    )


# ---------------------------------------------------------------------------
# AC2: DefaultAuditReader uses AUDIT_MAX_QUERY_LIMIT and AUDIT_DEFAULT_LIMIT
# ---------------------------------------------------------------------------


def test_story8_audit_default_limit_is_used_when_no_explicit_limit_is_passed():
    """AC2: DefaultAuditReader.query() must use AUDIT_DEFAULT_LIMIT (from
    config) when no explicit limit kwarg is provided."""
    # Import only the specific module under test — avoids triggering the
    # infrastructure_postgres import chain that has a Python-version issue.
    import src.cross_cutting.observability as obs_module

    DefaultAuditReader = obs_module.DefaultAuditReader
    AUDIT_LOG_PATH = obs_module.AUDIT_LOG_PATH

    # Write more events than the default limit (100) so we can confirm
    # only 100 are returned when no explicit limit is given.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".log", delete=False
    ) as f:
        for i in range(250):
            f.write(json.dumps({"event_type": "test", "detail": {"n": i}}) + "\n")
        tmp_path = Path(f.name)

    try:
        original_path = AUDIT_LOG_PATH
        obs_module.AUDIT_LOG_PATH = tmp_path
        reader = DefaultAuditReader()
        # No `limit` argument — must default to AUDIT_DEFAULT_LIMIT (100).
        events = reader.query()
        obs_module.AUDIT_LOG_PATH = original_path

        assert len(events) == 100, (
            f"query() with no limit returned {len(events)} events, "
            f"expected 100 (= AUDIT_DEFAULT_LIMIT from config)"
        )
    finally:
        tmp_path.unlink(missing_ok=True)


def test_story8_audit_max_query_limit_is_used_to_cap_excess_limit_requests():
    """AC2: DefaultAuditReader.query() must cap the result set at
    AUDIT_MAX_QUERY_LIMIT (1000) even when a caller requests more."""
    import src.cross_cutting.observability as obs_module

    DefaultAuditReader = obs_module.DefaultAuditReader
    AUDIT_LOG_PATH = obs_module.AUDIT_LOG_PATH

    # Write 1500 events (more than the 1000 cap).
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".log", delete=False
    ) as f:
        for i in range(1500):
            f.write(json.dumps({"event_type": "test", "detail": {"n": i}}) + "\n")
        tmp_path = Path(f.name)

    try:
        original_path = AUDIT_LOG_PATH
        obs_module.AUDIT_LOG_PATH = tmp_path
        reader = DefaultAuditReader()
        # Request 2000 — way over the cap — but only 1000 should come back.
        events = reader.query(limit=2000)
        obs_module.AUDIT_LOG_PATH = original_path

        assert len(events) == 1000, (
            f"query(limit=2000) returned {len(events)} events, "
            f"expected 1000 (= AUDIT_MAX_QUERY_LIMIT from config)"
        )
    finally:
        tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# AC3: Config follows module-level constants with type-annotation pattern
# ---------------------------------------------------------------------------


def test_story8_config_follows_module_level_constants_with_type_annotations_pattern():
    """AC3: Config must follow the existing codebase pattern — module-level
    constants with type annotations — matching how other config values in
    src/config.py are declared."""
    import ast

    config_path = Path(__file__).resolve().parents[2] / "src" / "config.py"
    source = config_path.read_text()
    tree = ast.parse(source)

    # All four audit constants must be present as assignment targets.
    audit_names = {
        "AUDIT_TABLE_NAME",
        "AUDIT_MAX_QUERY_LIMIT",
        "AUDIT_DEFAULT_LIMIT",
        "AUDIT_RETENTION_DAYS",
    }
    assigned_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned_names.add(target.id)
        # Python 3.10+ annotated assignments (name: type = value) use AnnAssign.
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                assigned_names.add(node.target.id)

    missing = audit_names - assigned_names
    assert not missing, f"audit config constants not found as top-level assignments: {missing}"

    # Every audit constant must have a type annotation (e.g.
    # AUDIT_MAX_QUERY_LIMIT: int = 1000) — use AnnAssign nodes, not line-scan.
    annotated: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            annotated.add(node.target.id)

    missing_annotation = audit_names - annotated
    assert not missing_annotation, (
        f"audit constants missing type annotations: {missing_annotation}"
    )
