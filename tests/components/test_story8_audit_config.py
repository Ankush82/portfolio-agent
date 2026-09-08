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
    """AC2: DefaultAuditReader.query() defaults to 100 rows when no
    explicit limit kwarg is provided. DefaultAuditReader is now real,
    Postgres-backed, constructor-injected (STORY-6 / #201) -- the old
    file-based implementation this test originally targeted (reading
    AUDIT_LOG_PATH, removed by STORY-10 / #205) is gone. See
    tests/test_audit_manager.py for the fuller real-Postgres query()
    coverage this file doesn't need to duplicate."""
    import inspect

    import src.cross_cutting.observability as obs_module

    default_limit = inspect.signature(obs_module.DefaultAuditReader.query).parameters["limit"].default
    assert default_limit == 100, (
        f"DefaultAuditReader.query()'s real default limit is {default_limit}, expected 100"
    )


def test_story8_audit_max_query_limit_is_used_to_cap_excess_limit_requests():
    """AC2: DefaultAuditReader.query() caps the effective result set at
    1000 rows even when a caller requests more -- verified directly
    against the real, current SQL-building logic (the LIMIT clause it
    actually sends), not by seeding 1500 real rows through Postgres just
    to count them back (tests/test_audit_manager.py's own
    test_query_no_arguments_returns_most_recent_events_capped already
    covers that end-to-end, against a live database)."""
    import src.cross_cutting.observability as obs_module

    class _CapturingCursor:
        def __init__(self) -> None:
            self.sql = None
            self.params = None

        def execute(self, sql, params):
            self.sql = sql
            self.params = params

        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _CapturingConnection:
        def __init__(self) -> None:
            self.cursor_obj = _CapturingCursor()

        def cursor(self):
            return self.cursor_obj

    class _FakeInfrastructure:
        def __init__(self) -> None:
            self.conn = _CapturingConnection()

        def _connection(self):
            return self.conn

    infra = _FakeInfrastructure()
    reader = obs_module.DefaultAuditReader(infra)
    reader.query(limit=2000)  # way over the cap

    # The real LIMIT param actually sent to Postgres must be capped at
    # 1000, regardless of the 2000 the caller asked for.
    assert infra.conn.cursor_obj.params[-2] == 1000


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
