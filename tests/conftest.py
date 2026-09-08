"""Set BROKER_TOKEN_ENCRYPTION_KEY before any module that imports broker_token_crypto.

broker_token_crypto validates the key at module load time (module-level
_FERNET = _load_fernet()), so this must run before the first import of any
module in the chain: c01_user_portfolio -> c04_knowledge_entity ->
infrastructure_postgres -> broker_token_crypto.
"""
import os
from cryptography.fernet import Fernet

os.environ["BROKER_TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import psycopg


def _postgres_available() -> bool:
    try:
        psycopg.connect(
            "postgresql://portfolio_agent:portfolio_agent@localhost:5432/portfolio_agent",
            connect_timeout=2,
        )
        return True
    except Exception:
        return False


POSTGRES_SKIP_REASON = "Postgres not available (start docker-compose.yml services)"


# pytest is imported lazily inside the fixture body so that this conftest
# remains importable as a plain module in environments where pytest isn't
# installed (e.g. when a checkpoint validates an `import infrastructure`
# chain by walking the conftest as part of its import resolution, instead
# of running it under the pytest runner).
def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    pass


# Register `postgres_dsn` as a real pytest fixture at pytest configure time,
# but only if pytest is actually available — otherwise leave it absent so
# plain `import` of this file never fails with ModuleNotFoundError.
try:
    import pytest

    @pytest.fixture
    def postgres_dsn() -> str:
        if not _postgres_available():
            pytest.skip(POSTGRES_SKIP_REASON)
        return "postgresql://portfolio_agent:portfolio_agent@localhost:5432/portfolio_agent"
except ModuleNotFoundError:
    # pytest isn't installed in this environment — that's fine; the fixture
    # is only meaningful under the pytest runner.
    pass
