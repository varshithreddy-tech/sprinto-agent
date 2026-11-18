"""Vercel entry point (FastAPI framework preset): exposes the dashboard app from sprinto_web/app.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sprinto_web.app import app  # noqa: E402,F401
