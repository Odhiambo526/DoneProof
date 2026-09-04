from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .domain import CompletionContract, VerificationReceipt


class Store:
    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("DONEPROOF_DB", "./doneproof.db")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def _init(self):
        with self._connect() as con:
            con.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS contracts (
                    id TEXT PRIMARY KEY,
                    task TEXT NOT NULL,
                    body_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    receipt_id TEXT PRIMARY KEY,
                    contract_id TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    body_json TEXT NOT NULL,
                    verified_at TEXT NOT NULL,
                    receipt_hash TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    FOREIGN KEY(contract_id) REFERENCES contracts(id)
                );
                CREATE INDEX IF NOT EXISTS idx_receipts_contract ON receipts(contract_id, verified_at DESC);
                """
            )

    def save_contract(self, contract: CompletionContract):
        body = contract.model_dump_json()
        with self._connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO contracts(id, task, body_json, created_at) VALUES(?,?,?,?)",
                (contract.id, contract.task, body, contract.created_at.isoformat()),
            )

    def save_receipt(self, receipt: VerificationReceipt):
        body = receipt.model_dump_json()
        with self._connect() as con:
            con.execute(
                "INSERT INTO receipts(receipt_id, contract_id, verdict, body_json, verified_at, receipt_hash, signature) VALUES(?,?,?,?,?,?,?)",
                (
                    receipt.receipt_id,
                    receipt.contract_id,
                    receipt.verdict.value,
                    body,
                    receipt.verified_at.isoformat(),
                    receipt.receipt_hash,
                    receipt.signature,
                ),
            )

    def get_contract(self, contract_id: str) -> CompletionContract | None:
        with self._connect() as con:
            row = con.execute("SELECT body_json FROM contracts WHERE id=?", (contract_id,)).fetchone()
        return CompletionContract.model_validate_json(row[0]) if row else None

    def list_receipts(self, limit: int = 50) -> list[VerificationReceipt]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT body_json FROM receipts ORDER BY verified_at DESC LIMIT ?", (max(1, min(limit, 200)),)
            ).fetchall()
        return [VerificationReceipt.model_validate_json(r[0]) for r in rows]
