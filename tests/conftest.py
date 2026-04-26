"""Shared test environment — set env BEFORE commodore module is imported."""
import os
import sys
from pathlib import Path

os.environ["BOT_TOKEN"] = "TEST_TOKEN"
os.environ["BOT_USERNAME"] = "commodore_lev_bot"
os.environ["BOT_HQ_GROUP_ID"] = "-1001111111111"
os.environ["SQUID_CAVE_GROUP_ID"] = "-1002222222222"
os.environ["AGENT_CHAT_GROUP_ID"] = "-1003675648747"
os.environ["LEV_DEV_GROUP_ID"] = "-1004444444444"
os.environ["ADMIN_TELEGRAM_IDS"] = "1234982301"
# Per-test isolation for the file-backed scratch dir
os.environ.setdefault("COMMODORE_RESULTS_DIR",
                      str(Path("/tmp") / "commodore-test-results"))

# Make the repo root importable so `from commodore import ...` works.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
