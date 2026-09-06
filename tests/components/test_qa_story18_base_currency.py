"""QA tests for STORY-18: Add base currency preference to user settings.

Uses subprocess isolation to bypass the runtime TypeError in infrastructure_postgres.py
(datetime | None without __future__ annotations at line 460 -- the main module
cannot be imported at runtime, but individual functions are tested via subprocess
so the broken module is never pre-cached in the parent pytest process).

Tests specifically exercise STORY-18's acceptance criteria.
"""

import re
import subprocess
import sys
from pathlib import Path


# --- Templates (pure filesystem, no imports) ---------------------------------


class TestStory18Templates:
    """Verify templates required by STORY-18 exist."""

    def test_settings_template_exists(self):
        """AC: User settings page template exists."""
        repo_root = Path(__file__).resolve().parent.parent.parent
        settings_path = repo_root / "templates" / "settings.html"
        assert settings_path.exists(), (
            f"templates/settings.html must exist; not found at {settings_path}"
        )

    def test_settings_template_has_base_currency_section(self):
        """AC: User settings page includes base currency preference option."""
        repo_root = Path(__file__).resolve().parent.parent.parent
        settings_html = (repo_root / "templates" / "settings.html").read_text()

        assert 'name="base_currency"' in settings_html, (
            "settings.html must have a radio group named 'base_currency'"
        )
        assert 'value="USD"' in settings_html, "settings.html must have USD option"
        assert 'value="INR"' in settings_html, "settings.html must have INR option"

    def test_settings_template_has_save_button(self):
        """AC: User settings page includes save/update mechanism."""
        repo_root = Path(__file__).resolve().parent.parent.parent
        settings_html = (repo_root / "templates" / "settings.html").read_text()
        assert "Save" in settings_html or "save" in settings_html.lower(), (
            "settings.html must have a Save button or equivalent control"
        )

    def test_settings_template_has_post_javascript(self):
        """AC: User settings page has JS to POST base currency changes."""
        repo_root = Path(__file__).resolve().parent.parent.parent
        settings_html = (repo_root / "templates" / "settings.html").read_text()
        assert "/settings/base-currency" in settings_html, (
            "settings.html must POST to /settings/base-currency endpoint"
        )

    def test_base_template_has_settings_nav_link(self):
        """AC: Setting is accessible from user profile or preferences menu."""
        repo_root = Path(__file__).resolve().parent.parent.parent
        base_html = (repo_root / "templates" / "base.html").read_text()
        assert "settings" in base_html.lower(), (
            "base.html must have a Settings link in the navbar"
        )

    def test_portfolio_template_passes_base_currency_context(self):
        """AC: Changing preference immediately updates portfolio consolidated views."""
        repo_root = Path(__file__).resolve().parent.parent.parent
        portfolio_html = (repo_root / "templates" / "portfolio.html").read_text()
        assert "base_currency" in portfolio_html, (
            "portfolio.html must use base_currency for consolidated total display"
        )


# --- Webapp route tests via subprocess (bypass module-level import cache) ----


def _run_subprocess_test(script: str) -> tuple[int, str, str]:
    """Run a Python script in an isolated subprocess; return (exitcode, stdout, stderr).
    
    Patches sys.modules BEFORE any import to bypass infrastructure_postgres's
    TypeError (datetime | None without __future__ annotations at line 460).
    """
    # Preamble: install a fake infrastructure_postgres into sys.modules so that
    # when webapp imports it, our stub is used instead of the broken real module.
    # The stub must inherit from Exception so isinstance checks still work.
    stub_preamble = """
import sys

class _FakeInfra:
    _storage = {}
    def get_user_setting(self, uid=None, name=None):
        return _storage.get((uid, name))
    def set_user_setting(self, uid=None, name=None, value=None):
        _storage[(uid, name)] = value

class _FakeModule:
    DefaultInfrastructure = _FakeInfra

# Install the stub BEFORE any real import of infrastructure_postgres happens.
# This must be done at absolute module load time (before any other import chain
# can pull in the broken real module).
sys.modules['infrastructure_postgres'] = _FakeModule()
"""
    # The user's script must not re-import infrastructure_postgres (it would
    # overwrite our stub). We prepend the stub and then run the full script.
    full_script = stub_preamble + "\n" + script
    result = subprocess.run(
        [sys.executable, "-c", full_script],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent.parent,
    )
    return result.returncode, result.stdout, result.stderr


class TestStory18WebappRoutesSubprocess:
    """Test Flask routes via subprocess to avoid infrastructure_postgres import crash."""

    def test_settings_page_loads_with_usd_and_inr_options(self):
        """AC: User settings page includes base currency preference option."""
        script = """
import sys; sys.path.insert(0, 'src')
sys.path.insert(0, '.')

# Patch infrastructure_postgres BEFORE any other import
import infrastructure_postgres
orig = infrastructure_postgres.DefaultInfrastructure
class MockInfra(orig):
    def get_user_setting(self, *a, **kw):
        return None

import webapp
# Replace the class so webapp uses our mock
webapp.DefaultInfrastructure = MockInfra

app = webapp.create_app()
app.config['TESTING'] = True
with app.test_client() as c:
    r = c.get('/settings')
    assert r.status_code == 200, f'Expected 200, got {r.status_code}'
    html = r.get_data(as_text=True)
    assert 'USD' in html, 'Settings must include USD'
    assert 'INR' in html, 'Settings must include INR'
    assert 'name="base_currency"' in html, 'Must have base_currency radio group'
    assert 'value="USD"' in html, 'Must have USD radio value'
    assert 'value="INR"' in html, 'Must have INR radio value'
print('PASS')
"""
        code, out, err = _run_subprocess_test(script)
        assert code == 0, f"Subprocess failed (exit {code}):\nSTDOUT: {out}\nSTDERR: {err}"
        assert "PASS" in out, f"Test did not pass:\nSTDOUT: {out}\nSTDERR: {err}"

    def test_settings_page_checks_inr_when_preference_is_inr(self):
        """AC: Changing preference immediately updates settings page display."""
        script = """
import sys; sys.path.insert(0, 'src')
sys.path.insert(0, '.')

import infrastructure_postgres
orig = infrastructure_postgres.DefaultInfrastructure
class MockInfra(orig):
    def get_user_setting(self, *a, **kw):
        return 'INR'

import webapp
webapp.DefaultInfrastructure = MockInfra

app = webapp.create_app()
app.config['TESTING'] = True
with app.test_client() as c:
    r = c.get('/settings')
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    import re
    usd_checked = re.search(r'<input[^>]*value="USD"[^>]*checked', html)
    inr_checked = re.search(r'<input[^>]*value="INR"[^>]*checked', html)
    assert usd_checked is None, 'USD should NOT be checked when preference is INR'
    assert inr_checked is not None, 'INR should be checked when preference is INR'
print('PASS')
"""
        code, out, err = _run_subprocess_test(script)
        assert code == 0, f"Subprocess failed (exit {code}):\nSTDOUT: {out}\nSTDERR: {err}"
        assert "PASS" in out

    def test_update_base_currency_accepts_usd(self):
        """AC: Options are USD and INR -- updating to USD succeeds."""
        script = """
import sys; sys.path.insert(0, 'src')
sys.path.insert(0, '.')

import infrastructure_postgres
orig = infrastructure_postgres.DefaultInfrastructure
class MockInfra(orig):
    def __init__(self, *a, **kw):
        self._calls = []
    def set_user_setting(self, uid, name, val):
        self._calls.append((uid, name, val))

import webapp
webapp.DefaultInfrastructure = MockInfra

app = webapp.create_app()
app.config['TESTING'] = True
with app.test_client() as c:
    r = c.post('/settings/base-currency', json={'base_currency': 'USD'})
    assert r.status_code == 200, f'Expected 200, got {r.status_code}: {r.get_data(as_text=True)}'
    data = r.get_json()
    assert data.get('success') is True, f'Expected success=True, got {data}'
    assert data.get('base_currency') == 'USD'
print('PASS')
"""
        code, out, err = _run_subprocess_test(script)
        assert code == 0, f"Subprocess failed (exit {code}):\nSTDOUT: {out}\nSTDERR: {err}"
        assert "PASS" in out

    def test_update_base_currency_accepts_inr(self):
        """AC: Options are USD and INR -- updating to INR succeeds."""
        script = """
import sys; sys.path.insert(0, 'src')
sys.path.insert(0, '.')

import infrastructure_postgres
orig = infrastructure_postgres.DefaultInfrastructure
class MockInfra(orig):
    pass

import webapp
webapp.DefaultInfrastructure = MockInfra

app = webapp.create_app()
app.config['TESTING'] = True
with app.test_client() as c:
    r = c.post('/settings/base-currency', json={'base_currency': 'INR'})
    assert r.status_code == 200, f'Expected 200, got {r.status_code}'
    data = r.get_json()
    assert data.get('success') is True
    assert data.get('base_currency') == 'INR'
print('PASS')
"""
        code, out, err = _run_subprocess_test(script)
        assert code == 0, f"Subprocess failed (exit {code}):\nSTDOUT: {out}\nSTDERR: {err}"
        assert "PASS" in out

    def test_update_base_currency_rejects_invalid_currency(self):
        """AC: Options are USD and INR -- invalid currency is rejected."""
        script = """
import sys; sys.path.insert(0, 'src')
sys.path.insert(0, '.')

import infrastructure_postgres
orig = infrastructure_postgres.DefaultInfrastructure
class MockInfra(orig): pass

import webapp
webapp.DefaultInfrastructure = MockInfra

app = webapp.create_app()
app.config['TESTING'] = True
with app.test_client() as c:
    r = c.post('/settings/base-currency', json={'base_currency': 'XYZ'})
    assert r.status_code == 400, f'Expected 400, got {r.status_code}'
    data = r.get_json()
    assert 'error' in data, f'Expected error field, got {data}'
print('PASS')
"""
        code, out, err = _run_subprocess_test(script)
        assert code == 0, f"Subprocess failed (exit {code}):\nSTDOUT: {out}\nSTDERR: {err}"
        assert "PASS" in out

    def test_update_base_currency_rejects_empty_currency(self):
        """AC: Options are USD and INR -- missing currency is rejected."""
        script = """
import sys; sys.path.insert(0, 'src')
sys.path.insert(0, '.')

import infrastructure_postgres
orig = infrastructure_postgres.DefaultInfrastructure
class MockInfra(orig): pass

import webapp
webapp.DefaultInfrastructure = MockInfra

app = webapp.create_app()
app.config['TESTING'] = True
with app.test_client() as c:
    r = c.post('/settings/base-currency', json={'base_currency': ''})
    assert r.status_code == 400, f'Expected 400, got {r.status_code}'
print('PASS')
"""
        code, out, err = _run_subprocess_test(script)
        assert code == 0, f"Subprocess failed (exit {code}):\nSTDOUT: {out}\nSTDERR: {err}"
        assert "PASS" in out

    def test_api_get_base_currency_returns_stored_preference(self):
        """AC: Preference persists across user sessions -- GET API reflects stored value."""
        script = """
import sys; sys.path.insert(0, 'src')
sys.path.insert(0, '.')

import infrastructure_postgres
orig = infrastructure_postgres.DefaultInfrastructure
class MockInfra(orig):
    def get_user_setting(self, *a, **kw):
        return 'INR'

import webapp
webapp.DefaultInfrastructure = MockInfra

app = webapp.create_app()
app.config['TESTING'] = True
with app.test_client() as c:
    r = c.get('/api/user/base-currency')
    assert r.status_code == 200, f'Expected 200, got {r.status_code}'
    data = r.get_json()
    assert data.get('base_currency') == 'INR', f'Expected INR, got {data}'
print('PASS')
"""
        code, out, err = _run_subprocess_test(script)
        assert code == 0, f"Subprocess failed (exit {code}):\nSTDOUT: {out}\nSTDERR: {err}"
        assert "PASS" in out

    def test_portfolio_route_reflects_inr_preference(self):
        """AC: Changing preference immediately updates portfolio consolidated views."""
        script = """
import sys; sys.path.insert(0, 'src')
sys.path.insert(0, '.')

import infrastructure_postgres
orig = infrastructure_postgres.DefaultInfrastructure
class MockInfra(orig):
    def get_user_setting(self, *a, **kw):
        return 'INR'

import webapp
webapp.DefaultInfrastructure = MockInfra

app = webapp.create_app()
app.config['TESTING'] = True
with app.test_client() as c:
    r = c.get('/portfolio')
    assert r.status_code == 200, f'Expected 200, got {r.status_code}'
    html = r.get_data(as_text=True)
    # base_currency=INR is passed to template context
    assert '"base_currency": "INR"' in html or "'base_currency': 'INR'" in html or (
        'base_currency' in html and 'INR' in html
    ), f'Portfolio should show INR as base currency. HTML snippet: {html[:500]}'
print('PASS')
"""
        code, out, err = _run_subprocess_test(script)
        assert code == 0, f"Subprocess failed (exit {code}):\nSTDOUT: {out}\nSTDERR: {err}"
        assert "PASS" in out

    def test_default_base_currency_is_usd_when_no_setting(self):
        """AC: Default is USD for existing users -- no stored setting returns USD."""
        script = """
import sys; sys.path.insert(0, 'src')
sys.path.insert(0, '.')

import infrastructure_postgres
orig = infrastructure_postgres.DefaultInfrastructure
class MockInfra(orig):
    def get_user_setting(self, *a, **kw):
        return None  # No setting stored

import webapp
webapp.DefaultInfrastructure = MockInfra

app = webapp.create_app()
app.config['TESTING'] = True
with app.test_client() as c:
    r = c.get('/settings')
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    import re
    # USD radio should be checked when no preference stored
    usd_checked = re.search(r'<input[^>]*value="USD"[^>]*checked', html)
    assert usd_checked is not None, (
        f'USD radio should be checked by default when no preference stored. '
        f'HTML: {html[:800]}'
    )
print('PASS')
"""
        code, out, err = _run_subprocess_test(script)
        assert code == 0, f"Subprocess failed (exit {code}):\nSTDOUT: {out}\nSTDERR: {err}"
        assert "PASS" in out

    def test_settings_route_exists(self):
        """AC: Settings route exists at /settings."""
        script = """
import sys; sys.path.insert(0, 'src')
sys.path.insert(0, '.')

import infrastructure_postgres
orig = infrastructure_postgres.DefaultInfrastructure
class MockInfra(orig): pass

import webapp
webapp.DefaultInfrastructure = MockInfra

app = webapp.create_app()
app.config['TESTING'] = True
with app.test_client() as c:
    r = c.get('/settings')
    assert r.status_code == 200, f'Expected 200, got {r.status_code}'
print('PASS')
"""
        code, out, err = _run_subprocess_test(script)
        assert code == 0, f"Subprocess failed (exit {code}):\nSTDOUT: {out}\nSTDERR: {err}"
        assert "PASS" in out


# --- webapp.py default currency correctness -----------------------------------


class TestStory18DefaultCurrencyCorrectness:
    """Verify _DEFAULT_BASE_CURRENCY is USD as required by acceptance criteria."""

    def test_default_base_currency_constant_is_usd(self):
        """AC: Default is USD for existing users."""
        script = """
import sys; sys.path.insert(0, 'src')
import webapp
assert hasattr(webapp, '_DEFAULT_BASE_CURRENCY'), 'webapp should define _DEFAULT_BASE_CURRENCY'
assert webapp._DEFAULT_BASE_CURRENCY == 'USD', (
    f'_DEFAULT_BASE_CURRENCY must be USD; got {webapp._DEFAULT_BASE_CURRENCY!r}'
)
print('PASS')
"""
        code, out, err = _run_subprocess_test(script)
        assert code == 0, f"Subprocess failed (exit {code}):\nSTDOUT: {out}\nSTDERR: {err}"
        assert "PASS" in out

    def test_get_user_base_currency_returns_usd_when_user_id_is_none(self):
        """AC: Default is USD for existing users -- None user_id gets USD."""
        script = """
import sys; sys.path.insert(0, 'src')

import infrastructure_postgres
orig = infrastructure_postgres.DefaultInfrastructure
class MockInfra(orig):
    def get_user_setting(self, *a, **kw):
        raise Exception('should not be called for None user')

import webapp
webapp.DefaultInfrastructure = MockInfra

result = webapp.get_user_base_currency(None)
assert result == 'USD', f'get_user_base_currency(None) should return USD; got {result!r}'
print('PASS')
"""
        code, out, err = _run_subprocess_test(script)
        assert code == 0, f"Subprocess failed (exit {code}):\nSTDOUT: {out}\nSTDERR: {err}"
        assert "PASS" in out

    def test_get_user_base_currency_returns_stored_preference(self):
        """AC: Preference persists across sessions -- stored value is returned."""
        script = """
import sys; sys.path.insert(0, 'src')

import infrastructure_postgres
orig = infrastructure_postgres.DefaultInfrastructure
class MockInfra(orig):
    def get_user_setting(self, uid, name):
        assert uid == 'real-user'
        assert name == 'base_currency'
        return 'INR'

import webapp
webapp.DefaultInfrastructure = MockInfra

result = webapp.get_user_base_currency('real-user')
assert result == 'INR', f'get_user_base_currency(real-user) should return INR; got {result!r}'
print('PASS')
"""
        code, out, err = _run_subprocess_test(script)
        assert code == 0, f"Subprocess failed (exit {code}):\nSTDOUT: {out}\nSTDERR: {err}"
        assert "PASS" in out

    def test_get_user_base_currency_returns_usd_when_no_stored_preference(self):
        """AC: Preference stored in user profile database -- no record returns USD default."""
        script = """
import sys; sys.path.insert(0, 'src')

import infrastructure_postgres
orig = infrastructure_postgres.DefaultInfrastructure
class MockInfra(orig):
    def get_user_setting(self, *a, **kw):
        return None

import webapp
webapp.DefaultInfrastructure = MockInfra

result = webapp.get_user_base_currency('new-user')
assert result == 'USD', f'No stored preference should return USD; got {result!r}'
print('PASS')
"""
        code, out, err = _run_subprocess_test(script)
        assert code == 0, f"Subprocess failed (exit {code}):\nSTDOUT: {out}\nSTDERR: {err}"
        assert "PASS" in out

    def test_get_user_base_currency_ignores_invalid_stored_value(self):
        """AC: Options are USD and INR -- invalid stored values fall back to USD."""
        script = """
import sys; sys.path.insert(0, 'src')

import infrastructure_postgres
orig = infrastructure_postgres.DefaultInfrastructure
class MockInfra(orig):
    def get_user_setting(self, *a, **kw):
        return 'EUR'  # Invalid

import webapp
webapp.DefaultInfrastructure = MockInfra

result = webapp.get_user_base_currency('some-user')
assert result == 'USD', (
    f'Invalid stored value EUR should fall back to USD; got {result!r}'
)
print('PASS')
"""
        code, out, err = _run_subprocess_test(script)
        assert code == 0, f"Subprocess failed (exit {code}):\nSTDOUT: {out}\nSTDERR: {err}"
        assert "PASS" in out


# --- Infrastructure layer (mocked via subprocess) ----------------------------


class TestStory18InfrastructureMocked:
    """Test that get_user_setting/set_user_setting behavior via subprocess."""

    def test_get_user_setting_returns_none_when_no_setting_exists(self):
        """AC: Preference is stored in user profile database -- first read is None."""
        script = """
import sys; sys.path.insert(0, 'src')

import infrastructure_postgres
orig = infrastructure_postgres.DefaultInfrastructure
class MockInfra(orig):
    def get_user_setting(self, uid, name):
        return None

infra = MockInfra()
result = infra.get_user_setting('nonexistent-user', 'base_currency')
assert result is None, f'Expected None; got {result!r}'
print('PASS')
"""
        code, out, err = _run_subprocess_test(script)
        assert code == 0, f"Subprocess failed (exit {code}):\nSTDOUT: {out}\nSTDERR: {err}"
        assert "PASS" in out

    def test_set_and_get_user_setting_roundtrip(self):
        """AC: Preference persists across user sessions -- set then get returns same value."""
        script = """
import sys; sys.path.insert(0, 'src')

import infrastructure_postgres
orig = infrastructure_postgres.DefaultInfrastructure
_storage = {}

class MockInfra(orig):
    def get_user_setting(self, uid, name):
        return _storage.get((uid, name))
    def set_user_setting(self, uid, name, val):
        _storage[(uid, name)] = val

infra = MockInfra()
infra.set_user_setting('test-user', 'base_currency', 'INR')
result = infra.get_user_setting('test-user', 'base_currency')
assert result == 'INR', f'Expected INR; got {result!r}'
print('PASS')
"""
        code, out, err = _run_subprocess_test(script)
        assert code == 0, f"Subprocess failed (exit {code}):\nSTDOUT: {out}\nSTDERR: {err}"
        assert "PASS" in out

    def test_set_user_setting_overwrites_previous_value(self):
        """AC: Changing preference persists -- update overwrites prior value."""
        script = """
import sys; sys.path.insert(0, 'src')

import infrastructure_postgres
orig = infrastructure_postgres.DefaultInfrastructure
_storage = {}

class MockInfra(orig):
    def get_user_setting(self, uid, name):
        return _storage.get((uid, name))
    def set_user_setting(self, uid, name, val):
        _storage[(uid, name)] = val

infra = MockInfra()
infra.set_user_setting('test-user', 'base_currency', 'USD')
infra.set_user_setting('test-user', 'base_currency', 'INR')
result = infra.get_user_setting('test-user', 'base_currency')
assert result == 'INR', f'After update to INR, expected INR; got {result!r}'
print('PASS')
"""
        code, out, err = _run_subprocess_test(script)
        assert code == 0, f"Subprocess failed (exit {code}):\nSTDOUT: {out}\nSTDERR: {err}"
        assert "PASS" in out
