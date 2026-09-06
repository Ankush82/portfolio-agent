#!/usr/bin/env python3
"""Enforce that c01_user_portfolio.py's public API never silently changes.

This script is the automated guard for STORY-16. It introspects every
public callable defined *directly in* c01_user_portfolio.py (not
re-exported from domain.py, infrastructure_postgres.py, etc.) and
compares its inspect.signature against a committed JSON snapshot.

Only names whose __module__ is "components.c01_user_portfolio" are
included — everything imported for backward compatibility (Holding,
Portfolio, Transaction, User, validate_stock_symbol, etc.) is excluded,
because those belong to other components and have their own snapshot
guards.

Rules:
  - Only names defined in c01_user_portfolio.py are snapshotted.
  - A purely additive defaulted keyword-only parameter is the only
    acceptable signature change (c01 refactor story's optional
    repository-injection allowance).
  - If such a parameter was added, the snapshot must be updated and
    the PR description must name it explicitly.

Usage:
    python scripts/check_public_api.py [--generate]
        --generate  overwrite the snapshot with the current signatures
                    and exit 0.  Use ONLY after intentionally accepting
                    a purely additive defaulted keyword-only parameter,
                    and name it in the PR description.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path
from typing import get_type_hints

# Resolve paths relative to the repo root (parent of scripts/).
_REPO_ROOT = Path(__file__).parent.parent.resolve()
_SCRIPT_DIR = Path(__file__).parent.resolve()
_SRC_DIR = _REPO_ROOT / "src"

sys.path.insert(0, str(_SRC_DIR))
_MODULE_NAME = "components.c01_user_portfolio"

# The target module must be importable from src/.
_snapshot_path = _REPO_ROOT / "tests" / "snapshots" / "c01_public_api.json"

# --------------------------------------------------------------------
# Introspection helpers
# --------------------------------------------------------------------

def _is_local(obj: object) -> bool:
    """Return True when obj is defined directly in c01_user_portfolio.py,
    as opposed to being imported / re-exported from another module.

    We check __module__ (which is set correctly for classes and
    functions defined in the file) and fall back to getfile() for
    module-level values (constants, etc.).
    """
    obj_module = getattr(obj, "__module__", None)
    if obj_module == _MODULE_NAME:
        return True
    # For module-level constants / dataclasses decorators that don't
    # carry __module__, check which file they come from.
    try:
        file = inspect.getfile(obj)
    except (TypeError, OSError):
        return False
    return "c01_user_portfolio" in file


def _is_protocol_class(cls: type) -> bool:
    """True when cls is a typing.Protocol base (not one of its concrete
    implementers)."""
    try:
        from typing import Protocol, get_origin
        if cls is Protocol:
            return True
        origin = get_origin(cls)
        return origin is not None and origin.__name__ == "Protocol"
    except Exception:
        return False


def _is_protocol_instance(cls: type) -> bool:
    """True when cls was decorated with @runtime_checkable and is a
    Protocol subclass (i.e. BrokerConnector itself, not StubBrokerConnector)."""
    return (
        hasattr(cls, "_is_protocol")
        and cls._is_protocol  # type: ignore[attr-defined]
        and not isinstance(cls, type(type))
    )


def _is_instantiable_class(obj: object) -> bool:
    """Return True for classes that are concrete types (not Protocols,
    not abstract bases, not Python built-ins re-exported through the
    module).  We want to enumerate public methods on:
      - Concrete implementations (DefaultUserPortfolio, StubUserPortfolio,
        StubBrokerConnector, PlaceholderBrokerConnector, BrokerHolding, …)
      - Protocol definitions (BrokerConnector, UserPortfolio) — callers
        bind to the Protocol and call through it.

    We exclude: Python stdlib types that happen to be imported here
    (date, datetime, Decimal, Protocol, Literal, etc.).
    """
    if not isinstance(obj, type):
        return False
    if obj.__module__ == "builtins":
        return False
    # typing constructs
    if _is_protocol_class(obj):
        return True
    # Concrete classes defined in this module
    if obj.__module__ == _MODULE_NAME:
        return True
    return False


def _get_public_callables(cls: type) -> list[tuple[str, object]]:
    """All public (non-underscore-prefixed) callable members of cls,
    including inherited ones.  Dunders are excluded."""
    results: list[tuple[str, object]] = []
    seen: set[str] = set()
    for name in dir(cls):
        if name.startswith("_"):
            continue
        attr = getattr(cls, name, None)
        if callable(attr):
            if name not in seen:
                seen.add(name)
                results.append((name, attr))
    return sorted(results)


def _signature_str(obj: object) -> str:
    """Return str(inspect.signature) for a callable or class.__init__."""
    # For classes, inspect the __init__ so we capture the constructor
    # signature callers actually use (not the Protocol's __call__).
    target = obj
    if isinstance(obj, type):
        target = obj.__init__

    try:
        sig = inspect.signature(target, follow_wrapped=False)
        return str(sig)
    except (ValueError, TypeError):
        pass

    # Fallback: try to build a signature from type annotations alone.
    try:
        hints = get_type_hints(target)
        params = ", ".join(f"{k}: ..." for k in hints)
        ret = hints.get("return", "")
        return f"({params}) -> {ret}"
    except Exception:
        return "(signature unavailable)"


# --------------------------------------------------------------------
# Snapshot capture
# --------------------------------------------------------------------

def _capture_public_api(added_kwonly_params: list[str] | None = None) -> dict:
    """Capture the full public API surface defined in c01_user_portfolio.py.

    added_kwonly_params is an optional list of "Class.method" strings naming
    any purely additive defaulted keyword-only parameters intentionally
    introduced by the refactor.  It is stored in the snapshot and must be
    updated (and named in the PR description) whenever --generate is used
    to accept such a change.
    """
    import importlib
    mod = importlib.import_module(_MODULE_NAME)

    snapshot: dict = {
        "metadata": {
            "module": _MODULE_NAME,
            "description": (
                "Public-API snapshot for c01_user_portfolio.py. "
                "Contains ONLY names defined directly in this file "
                "(verified via __module__ / inspect.getfile). "
                "Items imported for backward compatibility "
                "(Holding, Portfolio, Transaction, User, validate_stock_symbol, "
                "DefaultInfrastructure, DefaultKnowledgeEntity, etc.) "
                "are excluded because they belong to other components. "
                "Regenerate with --generate ONLY when intentionally "
                "accepting a purely additive defaulted keyword-only "
                "parameter, and name that parameter explicitly in the PR description."
            ),
            "added_kwonly_params": added_kwonly_params or [],
        },
        "module_level_functions": {},
        "classes": {},
    }

    # --- Module-level public names defined locally ---
    for name in dir(mod):
        if not name.isidentifier() or name.startswith("_"):
            continue
        obj = getattr(mod, name)
        if not _is_local(obj):
            continue
        # Functions (not classes)
        if callable(obj) and not isinstance(obj, type):
            snapshot["module_level_functions"][name] = _signature_str(obj)

    # --- Public classes defined locally, and their public callables ---
    for name in dir(mod):
        if not name.isidentifier() or name.startswith("_"):
            continue
        obj = getattr(mod, name)
        if not _is_instantiable_class(obj):
            continue

        class_entry: dict = {
            "kind": (
                "Protocol"
                if _is_protocol_instance(obj)
                else (
                    "Protocol" if _is_protocol_class(obj)
                    else obj.__class__.__name__
                )
            ),
            "public_callables": {
                meth_name: _signature_str(meth)
                for meth_name, meth in _get_public_callables(obj)
            },
        }
        snapshot["classes"][name] = class_entry

    return snapshot


# --------------------------------------------------------------------
# Diff engine
# --------------------------------------------------------------------

def _deep_compare(current: dict, baseline: dict, path: str = "") -> list[str]:
    """Recursively compare two snapshot dicts.  Returns a list of
    human-readable diff lines (empty == identical)."""
    diffs: list[str] = []
    all_keys = set(current) | set(baseline)

    for key in sorted(all_keys):
        if key == "metadata":
            continue
        if key not in current:
            diffs.append(f"  REMOVED {path}{key}")
        elif key not in baseline:
            diffs.append(f"  ADDED   {path}{key}")
        else:
            cv = current[key]
            bv = baseline[key]
            if isinstance(cv, dict) and isinstance(bv, dict):
                diffs.extend(_deep_compare(cv, bv, path=f"{path}{key}."))
            elif cv != bv:
                diffs.append(f"  CHANGED {path}{key}:")
                diffs.append(f"    baseline: {bv}")
                diffs.append(f"    current:  {cv}")

    return diffs


def _parse_signature(sig_str: str) -> dict[str, inspect.Parameter]:
    """Parse a str signature back into a dict of name -> Parameter."""
    import ast, inspect as _inspect

    src = f"def _f{sig_str}: pass"
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}

    params: list[_inspect.Parameter] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            args = node.args
            # positionalonly, positional_or_keyword
            for arg in args.args:
                params.append(_inspect.Parameter(arg.arg, _inspect.Parameter.POSITIONAL_OR_KEYWORD))
            # keyword-only
            for arg in args.kwonlyargs:
                default = _inspect.Parameter.empty
                if arg.default and isinstance(arg.default, ast.Constant):
                    try:
                        default = ast.literal_eval(arg.default.value)
                    except Exception:
                        default = _inspect.Parameter.empty
                params.append(_inspect.Parameter(arg.arg, _inspect.Parameter.KEYWORD_ONLY, default=default))
            # varargs
            if args.vararg:
                params.append(_inspect.Parameter(args.vararg.arg, _inspect.Parameter.VAR_POSITIONAL))
            # kwarg
            if args.kwarg:
                params.append(_inspect.Parameter(args.kwarg.arg, _inspect.Parameter.VAR_KEYWORD))
            break

    return {p.name: p for p in params}


def _is_acceptable_signature_change(cur: str, bas: str) -> bool:
    """Return True when cur differs from bas ONLY by adding one or more
    keyword-only parameters, each with a non-None default value.

    This is the sole permitted signature change (c01 refactor story's
    optional repository-injection allowance).
    """
    cur_p = _parse_signature(cur)
    bas_p = _parse_signature(bas)

    if not bas_p:
        return False

    # Every baseline parameter must be present in current with unchanged kind/default
    for name, param in bas_p.items():
        if name not in cur_p:
            return False
        cur_param = cur_p[name]
        if cur_param.kind != param.kind:
            return False
        if cur_param.default != param.default:
            return False

    # Current may have additional parameters, but they must be
    # keyword-only with a non-empty default.
    for name, cur_param in cur_p.items():
        if name in bas_p:
            continue
        # Acceptable: additional kw-only with a non-None default.
        if cur_param.kind not in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            return False
        if cur_param.kind == inspect.Parameter.KEYWORD_ONLY:
            if cur_param.default is inspect.Parameter.empty:
                return False

    return True


# --------------------------------------------------------------------
# Main check logic
# --------------------------------------------------------------------

def _check(generate: bool = False, added_kwonly_params: list[str] | None = None) -> int:
    current = _capture_public_api(added_kwonly_params=added_kwonly_params)

    if generate:
        _snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        _snapshot_path.write_text(json.dumps(current, indent=2) + "\n")
        if added_kwonly_params:
            print(f"✓ Snapshot written to {_snapshot_path}")
            print("  added_kwonly_params: " + ", ".join(added_kwonly_params))
        else:
            print(f"✓ Snapshot written to {_snapshot_path}")
        return 0

    if not _snapshot_path.exists():
        print(f"ERROR: Snapshot not found at {_snapshot_path}")
        print("  Run with --generate to create it (only after intentionally accepting a change).")
        return 1

    baseline = json.loads(_snapshot_path.read_text())
    raw_diffs = _deep_compare(current, baseline)

    if not raw_diffs:
        print("✓ Public API surface matches snapshot.")
        return 0

    # Categorise each diff: acceptable (additive kw-only) vs real failure
    failures: list[str] = []
    acceptable: list[str] = []

    for diff in raw_diffs:
        if not diff.startswith("  CHANGED classes."):
            failures.append(diff)
            continue

        # e.g. "  CHANGED classes.BrokerConnector.public_callables.fetch_holdings:"
        # or "  CHANGED classes.DefaultUserPortfolio.public_callables.__init__:"
        rest = diff.split("classes.", 1)[1].rstrip(":")
        parts = rest.split(".", 2)
        if len(parts) < 3:
            failures.append(diff)
            continue

        class_name = parts[0]
        # parts[1] is "public_callables"
        if len(parts) < 3:
            failures.append(diff)
            continue
        member_name = parts[2]

        cur_sig = (
            current.get("classes", {})
            .get(class_name, {})
            .get("public_callables", {})
            .get(member_name, "")
        )
        bas_sig = (
            baseline.get("classes", {})
            .get(class_name, {})
            .get("public_callables", {})
            .get(member_name, "")
        )

        if _is_acceptable_signature_change(cur_sig, bas_sig):
            acceptable.append(f"  {class_name}.{member_name}: acceptable additive kw-only param(s)")
        else:
            failures.append(diff)

    if acceptable:
        print("Acceptable additive kw-only parameter(s) detected:")
        for a in acceptable:
            print(a)
        print()

    if failures:
        print("ERROR: Public API surface has changed:")
        for f in failures:
            print(f)
        print()
        print(
            "If this is a purely additive defaulted keyword-only parameter "
            "(the only permitted change), re-generate the snapshot with:"
        )
        print("  python scripts/check_public_api.py --generate")
        print("and name the parameter explicitly in the PR description.")
        return 1

    print("✓ All changes are acceptable additive keyword-only parameters.")
    return 0


# --------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Check c01_user_portfolio.py public API against committed snapshot.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--generate",
        action="store_true",
        help="Overwrite the snapshot with the current signatures and exit 0.",
    )
    parser.add_argument(
        "--accept-additive-kwonly",
        metavar="CLASS.METHOD",
        action="append",
        default=None,
        dest="added_kwonly_params",
        help=(
            "Name a Class.method whose current signature is permitted to have "
            "an additional defaulted keyword-only parameter (the only allowed "
            "signature change).  May be given multiple times.  "
            "The names are stored in the snapshot and must match the PR description."
        ),
    )
    args = parser.parse_args()
    sys.exit(_check(
        generate=args.generate,
        added_kwonly_params=args.added_kwonly_params,
    ))
