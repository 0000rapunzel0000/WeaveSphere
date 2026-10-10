"""Everest Textile Mills backend. Run: uvicorn app:app --reload  (API docs at /docs)
Each business area is a router in mill/ (see mill/__init__.py); this file only assembles them."""
import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from mill import (accounts, auth, db, defects, digest, dispatch, inventory, livefloor, masters, messages, orders,
                  production, purchasing, quality, reviews, voice, workforce)
from mill.config import CORS_ORIGINS

db.init()


@asynccontextmanager
async def lifespan(_):
    task = digest.start_scheduler()  # only runs if DIGEST_HOUR is set
    yield
    if task:
        task.cancel()


app = FastAPI(title="Everest Textile Mills", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"])
for area in (auth, masters, orders, inventory, reviews, production, quality, dispatch, workforce, accounts, messages,
             defects, purchasing, digest, voice):
    app.include_router(area.router)
app.include_router(voice.web)


@app.middleware("http")
async def live_floor(request, call_next):
    await run_in_threadpool(livefloor.accrue_now)
    return await call_next(request)


@app.exception_handler(sqlite3.IntegrityError)
def integrity_error(_, e):
    m = str(e)
    if m.startswith("UNIQUE"):
        return JSONResponse({"detail": "Already exists: " + m.split(": ", 1)[-1]}, status_code=409)
    if m.startswith("FOREIGN KEY"):
        return JSONResponse({"detail": "It refers to a record that does not exist."}, status_code=409)
    if m.startswith("NOT NULL"):
        return JSONResponse({"detail": m.split(": ", 1)[-1] + " is required."}, status_code=422)
    return JSONResponse({"detail": "Not allowed: " + m}, status_code=422)  # CHECK or wrong type


@app.exception_handler(RequestValidationError)
def bad_input(_, e):
    """One readable line, so `detail` is always a string."""
    return JSONResponse({"detail": "; ".join(f"{'.'.join(map(str, x['loc'][1:])) or 'body'}: {x['msg']}"
                                             for x in e.errors())}, status_code=422)
