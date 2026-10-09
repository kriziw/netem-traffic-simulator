from __future__ import annotations

import hmac
from functools import wraps

from flask import current_app, redirect, request, session, url_for

from .config import ensure_api_key, ensure_admin_password


def bearer_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        supplied = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        expected = ensure_api_key(current_app.config["TRAFFICGEN_SETTINGS"])
        if not supplied or not hmac.compare_digest(supplied.encode(), expected.encode()):
            return {"error": "Unauthorized"}, 401
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("trafficgen_admin") is True:
            return view(*args, **kwargs)
        return redirect(url_for("login", next=request.path))

    return wrapped


def admin_password_valid(candidate: str) -> bool:
    expected = ensure_admin_password(current_app.config["TRAFFICGEN_SETTINGS"])
    return bool(candidate) and hmac.compare_digest(candidate.encode(), expected.encode())
