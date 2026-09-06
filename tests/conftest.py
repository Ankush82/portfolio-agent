"""Set BROKER_TOKEN_ENCRYPTION_KEY before any module that imports broker_token_crypto.

broker_token_crypto validates the key at module load time (module-level
_FERNET = _load_fernet()), so this must run before the first import of any
module in the chain: c01_user_portfolio -> c04_knowledge_entity ->
infrastructure_postgres -> broker_token_crypto.
"""
import os
from cryptography.fernet import Fernet

os.environ["BROKER_TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
