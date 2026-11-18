"""Vercel entry point: serves the FastAPI dashboard (sprinto_web/app.py)."""
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from sprinto_web.app import app  # noqa: E402,F401
except Exception:  # surface the real import error instead of an opaque 500
    _err = traceback.format_exc()
    _listing = sorted(os.listdir(ROOT))

    async def app(scope, receive, send):  # minimal ASGI fallback
        if scope["type"] != "http":
            return
        body = ("App failed to import.\n\n" + _err + "\nBundle root: " + str(ROOT) + "\n" + "\n".join(_listing)).encode()
        await send({"type": "http.response.start", "status": 500, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": body})
