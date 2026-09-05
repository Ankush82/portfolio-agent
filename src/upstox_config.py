"""Upstox broker configuration.

This module provides a frozen dataclass for Upstox API credentials and a
factory method to load them from the environment with early validation.
"""

from dataclasses import dataclass
import os


class BrokerConfigError(RuntimeError):
    """Raised when required Upstox broker configuration is missing or empty."""
    pass


@dataclass(frozen=True)
class UpstoxConfig:
    """Immutable configuration for Upstox API access.

    Attributes:
        client_id: Upstox API client ID.
        client_secret: Upstox API client secret.
        redirect_uri: Redirect URI registered with Upstox app.
    """
    client_id: str
    client_secret: str
    redirect_uri: str

    @classmethod
    def from_env(cls) -> 'UpstoxConfig':
        """Create UpstoxConfig from environment variables.

        Reads:
            UPSTOX_CLIENT_ID
            UPSTOX_CLIENT_SECRET
            UPSTOX_REDIRECT_URI

        Raises:
            BrokerConfigError: If any required variable is missing or empty.
        """
        client_id = os.environ.get('UPSTOX_CLIENT_ID')
        client_secret = os.environ.get('UPSTOX_CLIENT_SECRET')
        redirect_uri = os.environ.get('UPSTOX_REDIRECT_URI')

        missing = []
        if not client_id or not client_id.strip():
            missing.append('UPSTOX_CLIENT_ID')
        if not client_secret or not client_secret.strip():
            missing.append('UPSTOX_CLIENT_SECRET')
        if not redirect_uri or not redirect_uri.strip():
            missing.append('UPSTOX_REDIRECT_URI')

        if missing:
            raise BrokerConfigError(
                f"Missing or empty Upstox configuration: {', '.join(missing)}. "
                "register an app at https://upstox.com/developer/api-documentation/ "
                "and set these variables in your .env file."
            )

        return cls(
            client_id=client_id.strip(),
            client_secret=client_secret.strip(),
            redirect_uri=redirect_uri.strip()
        )