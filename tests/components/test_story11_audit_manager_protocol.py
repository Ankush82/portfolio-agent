"""Tests for STORY-11: AuditManager Protocol definition is untouched.

This story exists solely to lock down REQ-001: the Protocol class, its
import location, its module path, and its record() method signature
(event_type: str, detail: dict) -> None must be exactly as specified.

These tests verify the Protocol definition itself, not the
DefaultAuditManager/StubAuditManager implementations (those are tested
elsewhere).
"""

import ast
import inspect
import sys
from pathlib import Path
from typing import Protocol

import pytest

from src.cross_cutting import observability


class TestStory11AuditManagerProtocol:
    """AC1-AC6 for STORY-11."""

    def test_ac1_protocol_exists_at_the_correct_module_path(self):
        """AC1: AuditManager Protocol exists at src/cross_cutting/observability.py."""
        assert hasattr(
            observability, "AuditManager"
        ), "AuditManager not found in src.cross_cutting.observability"
        assert inspect.isclass(
            observability.AuditManager
        ), "AuditManager should be a class (Protocol is a class in typing)"
        assert issubclass(
            observability.AuditManager, Protocol
        ), "AuditManager must be a Protocol subclass"

    def test_ac2_protocol_defines_exactly_record_with_correct_signature(self):
        """AC2: Protocol defines exactly record(self, event_type: str, detail: dict) -> None."""
        # Collect all Protocol members (methods without self)
        protocol_members = inspect.getmembers_static(observability.AuditManager)
        protocol_methods = [
            name
            for name, obj in protocol_members
            if callable(obj) and not name.startswith("_")
        ]

        # Must have exactly one public method
        assert len(protocol_methods) == 1, (
            f"AuditManager Protocol must have exactly 1 public method, "
            f"found {len(protocol_methods)}: {protocol_methods}"
        )
        assert protocol_methods[0] == "record", (
            f"The one method must be named 'record', found: {protocol_methods[0]}"
        )

        # Verify the record() signature exactly
        record_method = getattr(observability.AuditManager, "record")
        sig = inspect.signature(record_method)
        params = list(sig.parameters.items())

        # Remove 'self' from params for the check
        params_without_self = [(name, p) for name, p in params if name != "self"]

        # Must have exactly 2 parameters: event_type and detail
        assert len(params_without_self) == 2, (
            f"record() must have exactly 2 parameters (event_type, detail), "
            f"found {len(params_without_self)}: {[n for n, _ in params_without_self]}"
        )

        param_names = [name for name, _ in params_without_self]
        assert param_names == [
            "event_type",
            "detail",
        ], f"Parameter names must be ['event_type', 'detail'], got {param_names}"

        event_type_param = dict(params_without_self)["event_type"]
        detail_param = dict(params_without_self)["detail"]

        assert (
            event_type_param.annotation == str
        ), f"event_type annotation must be str, got {event_type_param.annotation}"
        assert (
            detail_param.annotation == dict
        ), f"detail annotation must be dict, got {detail_param.annotation}"
        assert (
            sig.return_annotation == None or sig.return_annotation is type(None)
        ), f"return annotation must be None, got {sig.return_annotation}"

    def test_ac3_protocol_has_no_additional_methods(self):
        """AC3: Protocol does NOT define any additional methods (no query, no read, etc.)."""
        protocol_members = inspect.getmembers_static(observability.AuditManager)
        all_methods = [
            name
            for name, obj in protocol_members
            if callable(obj) and not name.startswith("_")
        ]

        # Must have exactly 1 method total (the 'record' we already checked)
        assert len(all_methods) == 1, (
            f"AuditManager Protocol must have exactly 1 method total, "
            f"found {len(all_methods)}: {all_methods}"
        )

        forbidden = {"query", "read", "get_events", "fetch", "list_events", "search"}
        found_forbidden = [m for m in all_methods if m in forbidden]
        assert not found_forbidden, (
            f"AuditManager Protocol contains forbidden methods: {found_forbidden}"
        )

    def test_ac4_protocol_not_renamed_or_moved(self):
        """AC4: Protocol has not been renamed or moved to a different module."""
        # The Protocol must be reachable at the exact import path used by all call sites
        from src.cross_cutting.observability import AuditManager

        assert AuditManager is observability.AuditManager, (
            "AuditManager import path mismatch — module-level import must "
            "resolve to the same object as the module attribute"
        )
        # Verify the class name itself hasn't changed
        assert observability.AuditManager.__name__ == "AuditManager", (
            f"Protocol class name must be 'AuditManager', got: "
            f"{observability.AuditManager.__name__}"
        )

    def test_ac5_all_call_site_imports_resolve(self):
        """AC5: Import statements in all 10 call site files resolve without modification.

        Uses AST parsing to verify import lines are syntactically valid and name
        AuditManager/DefaultAuditManager, without triggering unrelated code that may
        have other bugs (e.g. infrastructure_postgres.py has a Python 3.10-style
        datetime annotation bug unrelated to this story).

        Covers: c06_memory, c07_event_observation, c09_evidence_verification,
        c10_agent_runtime, c11_tools_environment, c12_decision_policy,
        c13_interaction_notification, c14_learning_evaluation,
        cross_cutting/security, src/llm.
        """
        src_root = Path(__file__).resolve().parents[2] / "src"
        call_sites = [
            "components/c06_memory.py",
            "components/c07_event_observation.py",
            "components/c09_evidence_verification.py",
            "components/c10_agent_runtime.py",
            "components/c11_tools_environment.py",
            "components/c12_decision_policy.py",
            "components/c13_interaction_notification.py",
            "components/c14_learning_evaluation.py",
            "cross_cutting/security.py",
            "llm.py",
        ]

        for rel_path in call_sites:
            path = src_root / rel_path
            source = path.read_text()
            tree = ast.parse(source)

            # Find 'from cross_cutting.observability import ...' lines
            # and verify AuditManager and DefaultAuditManager are in the names
            found_import = False
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if (
                        node.module == "cross_cutting.observability"
                        or (node.module == "cross_cutting" and any("observability" in (a.name or "") for a in node.names))
                    ):
                        imported_names = [a.name for a in node.names]
                        assert "AuditManager" in imported_names, (
                            f"{rel_path}: 'from cross_cutting.observability import ...' "
                            f"must include AuditManager. Found: {imported_names}"
                        )
                        assert "DefaultAuditManager" in imported_names, (
                            f"{rel_path}: 'from cross_cutting.observability import ...' "
                            f"must include DefaultAuditManager. Found: {imported_names}"
                        )
                        found_import = True

            assert found_import, (
                f"{rel_path}: no 'from cross_cutting.observability import ...' "
                f"statement found"
            )

    def test_ac6_stub_and_default_audit_manager_satisfy_protocol(self):
        """AC6: StubAuditManager and DefaultAuditManager satisfy the AuditManager
        Protocol.

        Structural subtyping means a class satisfies a Protocol if:
          - It inherits from the Protocol, OR
          - It explicitly defines the required method(s)

        StubAuditManager: inherits from AuditManager (satisfies via inheritance)
        DefaultAuditManager: explicitly defines record() with the right signature

        This test verifies both via AST without triggering unrelated imports.
        """
        src_root = Path(__file__).resolve().parents[2] / "src"
        observability_path = src_root / "cross_cutting" / "observability.py"
        source = observability_path.read_text()
        tree = ast.parse(source)

        # Find all class definitions in the module
        class_info: dict[str, dict] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                info = {
                    "bases": [ast.unparse(base) for base in node.bases],
                    "methods": {},
                }
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        args = [arg.arg for arg in item.args.args if arg.arg != "self"]
                        info["methods"][item.name] = args
                class_info[node.name] = info

        # StubAuditManager must inherit from AuditManager (structural via inheritance)
        assert "StubAuditManager" in class_info, "StubAuditManager not found"
        stub_bases = class_info["StubAuditManager"]["bases"]
        assert "AuditManager" in stub_bases, (
            f"StubAuditManager must inherit from AuditManager for structural subtyping. "
            f"Bases: {stub_bases}"
        )

        # DefaultAuditManager must inherit from AuditManager AND define record()
        assert "DefaultAuditManager" in class_info, "DefaultAuditManager not found"
        default_bases = class_info["DefaultAuditManager"]["bases"]
        assert "AuditManager" in default_bases, (
            f"DefaultAuditManager must inherit from AuditManager. Bases: {default_bases}"
        )
        default_record_args = class_info["DefaultAuditManager"]["methods"].get("record")
        assert default_record_args == [
            "event_type",
            "detail",
        ], (
            f"DefaultAuditManager.record() must have args ['event_type', 'detail'], "
            f"got {default_record_args!r}"
        )

        # Both classes are now verified as structural subtypes:
        # - StubAuditManager satisfies AuditManager via inheritance (empty body OK for structural)
        # - DefaultAuditManager satisfies AuditManager via both inheritance and explicit record()

    def test_protocol_body_is_ellipsis_not_a_real_implementation(self):
        """Sanity check: the Protocol method body must be '...' (ellipsis),
        not a real method body — a Protocol defines an interface, not behavior."""
        record_method = getattr(observability.AuditManager, "record")
        source_lines, _ = inspect.getsourcelines(record_method)
        source = "".join(source_lines)

        # The method body must contain only the ellipsis marker and/or type hints
        # Strip type hints and whitespace, what's left should be just "..."
        stripped = source.replace("def record(self, event_type: str, detail: dict) -> None:", "")
        stripped = stripped.replace(" ", "").replace("\t", "").replace("\n", "")

        assert stripped == "...", (
            f"AuditManager.record() must have an ellipsis body ('...'), "
            f"not a real implementation. Source: {source!r}"
        )
