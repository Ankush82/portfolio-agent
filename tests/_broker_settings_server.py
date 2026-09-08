"""Start the Flask app with StubBrokerConnector registered for check_live_ui testing."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Generate and set the encryption key
from cryptography.fernet import Fernet
os.environ["BROKER_TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

# Ensure Upstox env vars are absent so 503 fires
os.environ.pop("UPSTOX_CLIENT_ID", None)
os.environ.pop("UPSTOX_CLIENT_SECRET", None)
os.environ.pop("UPSTOX_REDIRECT_URI", None)

from webapp import create_app
from components.c01_user_portfolio import register_broker_connector, StubBrokerConnector

app = create_app()
with app.app_context():
    register_broker_connector(StubBrokerConnector())

if __name__ == "__main__":
    # Listen on 127.0.0.1:5123; no reloader so it stays single-process
    app.run(host="127.0.0.1", port=5123, threaded=True, use_reloader=False)
