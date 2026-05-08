"""Root conftest — sets env vars BEFORE any CC Telegram module is imported.

The config.py module-level singleton requires TELEGRAM_BOT_TOKEN and
ALLOWED_USERS at import time, so these must be set before pytest
discovers any test that transitively imports cctelegram.
"""

import os
import tempfile

# Force-set (not setdefault) to prevent real env vars from leaking into tests
os.environ["TELEGRAM_BOT_TOKEN"] = "test:0000000000:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
os.environ["ALLOWED_USERS"] = "12345"
os.environ["CC_TELEGRAM_DIR"] = tempfile.mkdtemp(prefix="cc-telegram-test-")
