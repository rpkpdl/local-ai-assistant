"""Test environment: no real keys, no tracing, no network, a throwaway database.

Set before any assistant module is imported. load_dotenv() never overrides variables that
already exist, so these win over a developer's real .env.
"""

import os
import tempfile

os.environ["ANTHROPIC_API_KEY"] = "test-key"
os.environ["ASSISTANT_MODEL"] = "claude-opus-5-5"
os.environ["BRAVE_API_KEY"] = ""
os.environ["PHOENIX_COLLECTOR_ENDPOINT"] = ""
os.environ["ASSISTANT_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
