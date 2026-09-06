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
import pytest


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


@pytest.fixture
def postgres_dsn() -> str:
    if not _postgres_available():
        pytest.skip(POSTGRES_SKIP_REASON)
    return "postgresql://portfolio_agent:portfolio_agent@localhost:5432/portfolio_agent"
