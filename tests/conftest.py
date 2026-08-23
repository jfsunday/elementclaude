from __future__ import annotations

import os
import tempfile

# `app.config` requires these and `app.db` builds its engine at import time, so the
# environment has to be complete *before* anything from `app` is imported.
# Real env vars outrank the repo's .env file in pydantic-settings, so this also
# isolates the test run from a developer's local configuration.
_TMP_DATA_DIR = tempfile.mkdtemp(prefix="elementclaude-tests-")

os.environ.update({
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "MESSAGING_BOT_URL": "http://localhost:9999",
    "MESSAGING_BOT_API_KEY": "mb_test",
    "WEBHOOK_SECRET": "test-secret",
    "INITIAL_ADMIN_USER": "@test:example.org",
    "DATA_DIR": _TMP_DATA_DIR,
})
os.environ.pop("WORKSPACE_ROOT", None)
