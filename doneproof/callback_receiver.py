"""Standalone staging receiver: signature validation and atomic durable inbox."""
import hashlib
import json
import os
import re

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .sdk_callbacks import verify_callback
from .sdk_common import CallbackError, DuplicateEvent


def create_receiver(dsn, secrets):
    if not dsn.startswith(("postgresql://", "postgres://")) or not secrets or any(
        not re.fullmatch(r"[a-z0-9_-]{1,64}", k) or not isinstance(v, str) or len(v) < 32
        for k, v in secrets.items()
    ):
        raise RuntimeError("Receiver requires PostgreSQL and configured signature secrets")
    with psycopg.connect(dsn, connect_timeout=8) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS rc_callback_inbox (
            receiver TEXT NOT NULL, event_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL, expires_at DOUBLE PRECISION NOT NULL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY(receiver,event_id))""")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    def health():
        try:
            with psycopg.connect(dsn, connect_timeout=3) as con:
                con.execute("SELECT 1")
            return {"ready": True, "scope": "callback_inbox"}
        except Exception:
            return JSONResponse({"ready": False}, status_code=503)

    @app.post("/callbacks/{receiver}")
    async def receive(receiver: str, request: Request):
        secret = secrets.get(receiver)
        if not secret:
            return JSONResponse({"accepted": False}, status_code=404)
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 16384:
                return JSONResponse({"accepted": False}, status_code=413)
            raw.extend(chunk)
        def persist():
            with psycopg.connect(dsn, connect_timeout=8) as con:
                con.execute("SET LOCAL statement_timeout='10s'")
                class Inbox:
                    def claim(self, event_id, expires_at):
                        checksum = hashlib.sha256(raw).hexdigest()
                        cursor = con.execute("""INSERT INTO rc_callback_inbox
                            (receiver,event_id,payload_hash,payload_json,expires_at)
                            VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                            (receiver, event_id, checksum, raw.decode(), expires_at))
                        if cursor.rowcount:
                            return True
                        existing = con.execute("""SELECT payload_hash FROM rc_callback_inbox
                            WHERE receiver=%s AND event_id=%s""", (receiver, event_id)).fetchone()
                        if existing[0] != checksum:
                            raise ValueError("Conflicting event")
                        return False
                try:
                    verify_callback(bytes(raw), request.headers, secret=secret, replay_store=Inbox())
                except DuplicateEvent:
                    pass  # Durable prior acceptance; acknowledge an at-least-once delivery.
        try:
            import asyncio
            await asyncio.to_thread(persist)
        except CallbackError as exc:
            status = 503 if exc.code == "receiver_deduplication_unavailable" else 400
            return JSONResponse({"accepted": False}, status_code=status)
        except Exception:
            return JSONResponse({"accepted": False}, status_code=503)
        return {"accepted": True}

    return app


def factory():
    return create_receiver(os.environ["CALLBACK_DATABASE_URL"],
                           json.loads(os.environ["DONEPROOF_CALLBACK_RECEIVER_KEYS_JSON"]))
