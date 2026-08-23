import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("HARNESS_CACHE_DIR", "/tmp/harness-cache")
from app import app  # noqa: E402
application = app
