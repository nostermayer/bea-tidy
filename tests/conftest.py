import os
import sys
from unittest.mock import patch, MagicMock

# Set env vars before any module imports — organizer.py calls make_client() at module level
os.environ["GEMINI_API_KEY"] = "test-key"
os.environ["TMDB_API_KEY"] = ""
os.environ["BASE_MEDIA_DIR"] = "/tmp/bea-tidy-test"

# Create the test media dir so RotatingFileHandler doesn't fail on import
os.makedirs("/tmp/bea-tidy-test", exist_ok=True)

# Add project root and organizer/ to path so imports resolve
ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "organizer"))
sys.path.insert(0, os.path.join(ROOT, "cleanup"))

# Patch genai.Client before organizer.py is imported (it instantiates at module level)
_client_patcher = patch("google.genai.Client", return_value=MagicMock())
_client_patcher.start()
