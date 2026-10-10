"""Only the signed voice-agent endpoints, for exposing to the internet (Retell can't reach localhost).
Run: uvicorn voice_app:app --port 8002   then tunnel port 8002, never the main app."""
from fastapi import FastAPI

from mill import db, voice

db.init()
app = FastAPI(title="Everest voice agent", docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(voice.router)
