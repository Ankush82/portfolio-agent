"""Tests verifying the STORY-2 domain extraction.

STORY-2 extracts the four domain dataclasses (User, Portfolio, Holding,
Transaction) and the four table-name constants (USERS_TABLE,
PORTFOLIOS_TABLE, HOLDINGS_TABLE, TRANSACTIONS_TABLE) from
src/components/c01_user_portfolio.py into a new stdlib-only module
src/domain.py, and turns c01 into a thin re-exporter.

Acceptance criteria that these tests cover:

  - All eight names remain importable from src.components.c01_user_portfolio
    and are the *same objects* as in domain (is-identical, not copies).
  - The four dataclasses' __module__ is 'domain', confirming the class
    objects were moved (not redefined) into src/domain.py.
"""

from __future__ import annotations

import importlib

EIGHT_NAMES = (
    "User",
    "Portfolio",
    "Holding",
    "Transaction",
    "USERS_TABLE",
    "PORTFOLIOS_TABLE",
    "HOLDINGS_TABLE",
    "TRANSACTIONS_TABLE",
)

DATACLASS_NAMES = ("User", "Portfolio", "Holding", "Transaction")


def _load_modules():
    """Import domain and c01 by their real module names.

    domain.py lives at src/domain.py, so when pytest runs with src/ on
    pythonpath (configured via pytest.ini) it is importable as `domain`
    and its __module__ reads 'domain'. c01 lives at
    src/components/c01_user_portfolio.py and is importable as
    `components.c01_user_portfolio`. We use importlib (rather than the
    `from x import y` form) so that getattr() reflects whatever each
    module actually binds at the time of the test.
    """
    domain_module = importlib.import_module("domain")
    c01_module = importlib.import_module("components.c01_user_portfolio")
    return domain_module, c01_module


def test_story2_eight_names_in_c01_are_identical_objects_in_domain():
    """For each of the eight names, getattr(c01, name) is getattr(domain, name).

    The story explicitly forbids defining these names in two places and
    requires the c01 attribute to be the *same object* (is-identical,
    not merely equal). This guards against a future regression where
    someone re-introduces a duplicate definition in c01 that shadows
    the re-export with a copy.
    """
    domain_module, c01_module = _load_modules()

    failures = []
    for name in EIGHT_NAMES:
        c01_obj = getattr(c01_module, name, None)
        domain_obj = getattr(domain_module, name, None)
        if c01_obj is None:
            failures.append(f"{name!r} is not importable from components.c01_user_portfolio")
            continue
        if domain_obj is None:
            failures.append(f"{name!r} is not defined in domain")
            continue
        if c01_obj is not domain_obj:
            failures.append(
                f"{name!r} is defined in both modules but they are not "
                f"the same object: c01={c01_obj!r} domain={domain_obj!r}"
            )
    assert not failures, "; ".join(failures)


def test_story2_dataclasses_module_is_domain():
    """User.__module__ == 'domain' and likewise for Portfolio, Holding, Transaction.

    A dataclass's __module__ is the module where the `class` statement
    was executed (or where dataclass() was called). For c01's attribute
    to be is-identical to domain's and for its __module__ to be
    'domain', the class object must have been defined inside domain.py
    — which is exactly the move this story performs.
    """
    domain_module, _c01_module = _load_modules()

    failures = []
    for name in DATACLASS_NAMES:
        cls = getattr(domain_module, name, None)
        if cls is None:
            failures.append(f"{name!r} is not defined in domain")
            continue
        if getattr(cls, "__module__", None) != "domain":
            failures.append(
                f"{name!r}.__module__ is {getattr(cls, '__module__', None)!r}, "
                f"expected 'domain' (the class was not moved into src/domain.py)"
            )
    assert not failures, "; ".join(failures)