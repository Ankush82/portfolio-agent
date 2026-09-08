"""Demo test: STORY-16 cannot be verified due to a pre-existing NameError.

This test documents the blocking bug — c01_user_portfolio.py line 1697 references
`LifecycleMixin` which is never defined anywhere in the codebase. The module
fails to load, which prevents webapp.py from loading (it imports from
c01_user_portfolio), which prevents the story-16 endpoint from being tested.
"""
import pytest
import sys


def test_module_c01_user_portfolio_loads():
    """c01_user_portfolio.py must load without NameError.

    Currently fails at line 1697: class DefaultUserPortfolio(UserPortfolio, LifecycleMixin):
    LifecycleMixin is never defined or imported anywhere in the codebase.
    This NameError fires at module load time and propagates to webapp.py,
    making every story-16 test impossible to collect.
    """
    # These two imports currently raise NameError because they transitively
    # import c01_user_portfolio which fails at module level.
    try:
        import webapp          # noqa: F401
        from components.c01_user_portfolio import BrokerConfigError  # noqa: F401
        module_loaded = True
    except NameError as exc:
        if "LifecycleMixin" in str(exc):
            pytest.fail(
                f"BLOCKING BUG: c01_user_portfolio.py failed to load: {exc}\n"
                "Fix: change 'class DefaultUserPortfolio(UserPortfolio, LifecycleMixin):'\n"
                "to 'class DefaultUserPortfolio(UserPortfolio):'\n"
                "at line 1697 of src/components/c01_user_portfolio.py"
            )
        raise  # re-raise if it's a different NameError

    assert module_loaded, "Module should have loaded without NameError"


def test_webapp_create_app_returns_functioning_app():
    """create_app() must return a working Flask app.

    BLOCKED by the LifecycleMixin NameError above.
    """
    if not _postgres_available():
        pytest.skip("Postgres not available")

    from webapp import create_app

    app = create_app()
    assert app is not None
    # The story-16 endpoint must be registered
    assert "/api/brokers/upstox/connect" in [rule.rule for rule in app.url_map.iter_rules()]


def _postgres_available():
    try:
        import psycopg  # noqa: F401
        psycopg.connect(
            "postgresql://portfolio_agent:portfolio_agent@localhost:5432/portfolio_agent",
            connect_timeout=2,
        )
        return True
    except Exception:
        return False


def test_upstox_connect_returns_200_for_authenticated_user():
    """AC: Happy path — authenticated POST returns 200 with authorize_url and state.

    BLOCKED by the LifecycleMixin NameError above.
    """
    import oauth_state
    import unittest.mock

    # Configure oauth_state with a test DB
    if not _postgres_available():
        pytest.skip("Postgres not available")

    oauth_state.configure("postgresql://portfolio_agent:portfolio_agent@localhost:5432/portfolio_agent")

    from webapp import create_app
    from components.c01_user_portfolio import (
        DefaultUpstoxBrokerConnector,
        register_broker_connector,
    )

    app = create_app()
    with app.app_context():
        import os
        with unittest.mock.patch.dict(os.environ, {
            "UPSTOX_CLIENT_ID": "test-client-id",
            "UPSTOX_CLIENT_SECRET": "test-client-secret",
            "UPSTOX_REDIRECT_URI": "https://example.com/callback",
        }):
            from upstox_config import UpstoxConfig
            config = UpstoxConfig.from_env()
            connector = DefaultUpstoxBrokerConnector(config=config, http=unittest.mock.Mock())
            register_broker_connector(connector)

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = "story16-verification-user"

    response = client.post("/api/brokers/upstox/connect")
    assert response.status_code == 200
    data = response.get_json()
    assert "authorize_url" in data
    assert "state" in data
    assert "api.upstox.com/v2/login/authorization/dialog" in data["authorize_url"]
    assert f"state={data['state']}" in data["authorize_url"]


def test_upstox_connect_returns_503_when_env_vars_missing():
    """AC: Missing env vars → 503 with error='broker_not_configured'.

    BLOCKED by the LifecycleMixin NameError above.
    """
    import oauth_state
    import unittest.mock

    if not _postgres_available():
        pytest.skip("Postgres not available")

    oauth_state.configure("postgresql://portfolio_agent:portfolio_agent@localhost:5432/portfolio_agent")

    from webapp import create_app
    from upstox_config import BrokerConfigError as UpstoxBrokerConfigError

    app = create_app()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = "story16-verification-user"

    with unittest.mock.patch("webapp.UpstoxConfig.from_env") as mock_from_env:
        mock_from_env.side_effect = UpstoxBrokerConfigError(
            "Missing or empty Upstox configuration: UPSTOX_CLIENT_ID, "
            "UPSTOX_CLIENT_SECRET, UPSTOX_REDIRECT_URI."
        )
        response = client.post("/api/brokers/upstox/connect")

    assert response.status_code == 503
    data = response.get_json()
    assert data["error"] == "broker_not_configured"
    assert "UPSTOX_CLIENT_ID" in data["message"]
    assert "UPSTOX_CLIENT_SECRET" in data["message"]
    assert "UPSTOX_REDIRECT_URI" in data["message"]


def test_upstox_connect_returns_401_unauthenticated():
    """AC: Unauthenticated → 401.

    BLOCKED by the LifecycleMixin NameError above.
    """
    import oauth_state

    if not _postgres_available():
        pytest.skip("Postgres not available")

    oauth_state.configure("postgresql://portfolio_agent:portfolio_agent@localhost:5432/portfolio_agent")

    from webapp import create_app
    app = create_app()
    client = app.test_client()  # no session

    response = client.post("/api/brokers/upstox/connect")
    assert response.status_code == 401
