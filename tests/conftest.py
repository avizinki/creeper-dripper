import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Required runtime knobs in config.py are strict (no hidden defaults).
os.environ.setdefault("DISCOVERY_INTERVAL_SECONDS", "30")
os.environ.setdefault("MAX_ACTIVE_CANDIDATES", "7")
os.environ.setdefault("CANDIDATE_CACHE_TTL_SECONDS", "20")
os.environ.setdefault("ROUTE_CHECK_CACHE_TTL_SECONDS", "15")
os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("LIVE_TRADING_ENABLED", "false")
