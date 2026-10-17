"""Local-first personal AI assistant."""

from dotenv import load_dotenv

# Load .env before any submodule reads its settings (ASSISTANT_MODEL, LIBRARIAN_MODEL,
# JUDGE_MODEL...) at import time. Variables already set in the environment win.
load_dotenv()
