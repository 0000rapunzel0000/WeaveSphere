"""Settings from the environment. backend/.env (git-ignored) fills in anything not already set."""
import os

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_env = os.path.join(BACKEND, ".env")
if os.path.exists(_env):
    for _line in open(_env):
        _k, _, _v = _line.strip().partition("=")
        if _k and not _k.startswith("#") and _v:
            os.environ.setdefault(_k.strip(), _v.strip())

DB_PATH = os.environ.get("DB_PATH", os.path.join(BACKEND, "mill.db"))
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",")
SESSION_HOURS = float(os.environ.get("SESSION_HOURS", "24"))
SIM_SPEED = float(os.environ.get("SIM_SPEED", "1"))  # machine time per real time; e.g. 60 = an hour per minute for demos
ACCRUE_EVERY = float(os.environ.get("ACCRUE_EVERY", "2"))  # seconds between live-floor updates
JWT_SECRET = os.environ.get("JWT_SECRET", "")
if 0 < len(JWT_SECRET) < 32:
    raise SystemExit("JWT_SECRET must be at least 32 characters (try: python3 -c 'import secrets; print(secrets.token_urlsafe(48))')")
