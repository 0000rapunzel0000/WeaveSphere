"""Everest Textile Mills backend, one module per business area. app.py assembles their routers.

Shared: config (settings), db (connections and row helpers), auth (sign-in and roles), ai (OpenRouter calls).
Areas: masters, orders, inventory, reviews, livefloor + production, quality, dispatch, workforce, accounts,
messages, and the automation the system raises by itself: defects, purchasing, digest."""
from mill import config  # noqa: F401  loads backend/.env before any module reads a setting
