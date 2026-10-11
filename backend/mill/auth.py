"""Sign-in: scrypt password hashes, JWTs (HS256) with revocable sign-out, roles, and user accounts."""
import hashlib
import secrets
import time
from functools import cache
from typing import Literal

import jwt
from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel, Field

from mill.config import JWT_SECRET, SESSION_HOURS
from mill.db import connect, get_db, insert, rows, tx

router = APIRouter()


@cache
def jwt_secret():
    """JWT_SECRET if set (needed when several servers share users), else the key generated once into the DB."""
    if JWT_SECRET:
        return JWT_SECRET
    db = connect()
    try:
        return db.execute("SELECT value FROM app_secrets WHERE name = 'jwt'").fetchone()[0]
    finally:
        db.close()


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + "$" + hashlib.scrypt(pw.encode(), salt=salt.encode(), n=2**14, r=8, p=1).hex()


def claims(authorization):
    """The verified claims of a bearer JWT, or None. HS256 only: 'none' and other algorithms are rejected."""
    try:
        return jwt.decode(authorization.removeprefix("Bearer "), jwt_secret(), algorithms=["HS256"],
                          options={"require": ["exp", "sub", "jti"]})
    except jwt.PyJWTError:
        return None


def user(authorization: str = Header(""), db=Depends(get_db)):
    c = claims(authorization)
    # The role is re-read from users, so a changed role or removed user takes effect at once, not at token expiry.
    u = c and db.execute("SELECT id, username, role FROM users WHERE id = ? AND NOT EXISTS "
                         "(SELECT 1 FROM revoked_tokens WHERE jti = ?)", (int(c["sub"]), c["jti"])).fetchone()
    if not u:
        raise HTTPException(401, "login required")
    return dict(u)


def role(*roles):
    """Owner may do anything; everyone else only what is listed."""
    def check(u=Depends(user)):
        if u["role"] not in ("owner", *roles):
            raise HTTPException(403, "not allowed for your role")
        return u
    return Depends(check)


Role = Literal["owner", "admin", "store", "supervisor", "qc"]


EMAIL, PHONE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$", r"^\+?[0-9][0-9 -]{6,18}[0-9]$"


class UserIn(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^\S+$")
    password: str = Field(min_length=8)
    role: Role
    email: str | None = Field(None, max_length=254, pattern=EMAIL)
    phone: str | None = Field(None, max_length=20, pattern=PHONE)


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class Setup(Login):
    email: str = Field(max_length=254, pattern=EMAIL)
    phone: str = Field(max_length=20, pattern=PHONE)


# ponytail: in-memory lockout, per process; move to a table if the API ever runs as several processes.
FAILED: dict[str, list[float]] = {}
LOCK_AFTER, LOCK_SECONDS = 5, 15 * 60


@router.post("/auth/setup")
def setup(body: Setup, db=Depends(get_db)):
    """Creates the first owner. Only works on an empty database."""
    if len(body.password) < 8:
        raise HTTPException(422, "password must be at least 8 characters")
    with tx(db):
        if db.execute("SELECT 1 FROM users").fetchone():
            raise HTTPException(403, "already set up")
        insert(db, "users", {"username": body.username.strip(), "pw_hash": hash_pw(body.password), "role": "owner",
                             "email": body.email, "phone": body.phone})
    return login(body, db)


@router.post("/auth/login")
def login(body: Login, db=Depends(get_db)):
    now, name = time.time(), body.username.strip().lower()
    recent = [t for t in FAILED.get(name, []) if now - t < LOCK_SECONDS]
    if len(recent) >= LOCK_AFTER:
        raise HTTPException(429, f"too many failed sign-ins; try again in {int((LOCK_SECONDS - (now - recent[0])) // 60) + 1} min")
    u = db.execute("SELECT * FROM users WHERE username = ?", (body.username.strip(),)).fetchone()
    # Hash even for unknown users so the response time doesn't reveal which usernames exist.
    stored = u["pw_hash"] if u else hash_pw("x")
    if not secrets.compare_digest(hash_pw(body.password, stored.split("$")[0]), stored) or not u:
        if len(FAILED) > 10_000:  # made-up usernames must not grow this forever
            FAILED.clear()
        FAILED[name] = recent + [now]
        raise HTTPException(401, "wrong username or password")
    FAILED.pop(name, None)
    db.execute("DELETE FROM revoked_tokens WHERE expires_at < ?", (int(time.time()),))
    return {"token": token_for(u), "role": u["role"], "username": u["username"]}


def token_for(u, hours=SESSION_HOURS):
    now = int(time.time())
    return jwt.encode({"sub": str(u["id"]), "username": u["username"], "role": u["role"], "iat": now,
                       "exp": now + int(hours * 3600), "jti": secrets.token_urlsafe(16)}, jwt_secret(), algorithm="HS256")


@router.post("/auth/logout")
def logout(authorization: str = Header(""), db=Depends(get_db)):
    """JWTs can't be recalled, so sign-out lists this one's id until it would have expired anyway."""
    if c := claims(authorization):
        db.execute("INSERT OR IGNORE INTO revoked_tokens VALUES (?, ?)", (c["jti"], c["exp"]))
    return {"ok": True}


@router.get("/me")
def me(u=Depends(user)):
    return u


@router.post("/users")
def create_user(body: UserIn, db=Depends(get_db), u=role("admin")):
    if body.role == "owner" and u["role"] != "owner":
        raise HTTPException(403, "only an owner can create an owner")
    if db.execute("SELECT 1 FROM users WHERE username = ?", (body.username,)).fetchone():
        raise HTTPException(409, "that username is taken")
    uid = insert(db, "users", {"username": body.username, "pw_hash": hash_pw(body.password), "role": body.role,
                               "email": body.email, "phone": body.phone})
    return {"id": uid, "username": body.username, "role": body.role, "email": body.email, "phone": body.phone}


@router.get("/users")
def list_users(db=Depends(get_db), _=role("admin")):
    return rows(db.execute("SELECT id, username, role, email, phone FROM users ORDER BY id"))
