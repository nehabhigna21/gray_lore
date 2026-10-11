
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AUTO_INIT_DB", "1")   # first request on an empty Neon DB loads schema + seed

from app import app as flask_app  # noqa: E402

FUNCTION_PREFIX = "/api/index"


def app(environ, start_response):
   
    path = environ.get("PATH_INFO", "") or "/"
    if path == FUNCTION_PREFIX or path == FUNCTION_PREFIX + ".py":
        path = "/"
    elif path.startswith(FUNCTION_PREFIX + "/"):
        path = path[len(FUNCTION_PREFIX):]
    # Some runtimes put the original URL in a header; prefer it when present.
    original = environ.get("HTTP_X_VERCEL_ORIGINAL_PATH") or environ.get("HTTP_X_ORIGINAL_PATH")
    if original and original.startswith("/") and not original.startswith(FUNCTION_PREFIX):
        path = original.split("?", 1)[0]
    environ["PATH_INFO"] = path
    environ["SCRIPT_NAME"] = ""
    return flask_app(environ, start_response)
