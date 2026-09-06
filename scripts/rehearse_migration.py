"""Upgrade an explicitly selected isolated snapshot; print only aggregate results."""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from doneproof.domain import VerificationReceipt  # noqa: E402
from doneproof.signing import ReceiptSigner  # noqa: E402
from doneproof.store import Store  # noqa: E402

TABLES = ("contracts", "receipts", "contract_baselines", "idempotency", "audit_events", "evidence_events")


def snapshot(con):
    result = {}
    for table in TABLES:  # Fixed identifiers, never user SQL.
        rows = con.execute("SELECT row_to_json(t)::text FROM " + table + " t").fetchall()
        hashes = sorted(hashlib.sha256(row[0].encode()).hexdigest() for row in rows)
        result[table] = {"rows": len(rows), "sha256": hashlib.sha256("".join(hashes).encode()).hexdigest()}
    return result


def rehearse(dsn, expected_host, pinned_key):
    import psycopg
    if not expected_host or urlsplit(dsn).hostname != expected_host or "-pooler." in expected_host:
        raise ValueError("An exact isolated direct database host is required")
    with psycopg.connect(dsn) as con:
        before = snapshot(con)
        versions = [r[0] for r in con.execute("SELECT version FROM schema_migrations ORDER BY version")]
    if versions != [1]:
        raise ValueError("Rehearsal requires a fresh migration-1 snapshot")
    Store(dsn)
    with psycopg.connect(dsn) as con:
        after = snapshot(con)
        versions_after = [r[0] for r in con.execute("SELECT version FROM schema_migrations ORDER BY version")]
        receipts = [VerificationReceipt.model_validate_json(r[0]) for r in con.execute("SELECT body_json FROM receipts")]
        valid = sum(ReceiptSigner.verify_trusted(r, pinned_key) for r in receipts)
    if before != after or versions_after != list(range(1, 8)) or valid != len(receipts):
        raise RuntimeError("Migration preservation gate failed")
    return {"isolated_host": expected_host, "before_versions": versions, "after_versions": versions_after,
            "tables": after, "unchanged": before == after, "receipts_checked": len(receipts),
            "receipts_matching_supplied_pin": valid, "production_modified": False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-host", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if os.getenv("RC_ALLOW_MIGRATION") != "isolated-staging":
        raise SystemExit("Set RC_ALLOW_MIGRATION=isolated-staging after confirming the branch")
    try:
        result = rehearse(os.environ["RC_DATABASE_URL"], args.expected_host, os.environ["RC_PINNED_PUBLIC_KEY"])
    except Exception as exc:
        print(json.dumps({"passed": False, "error_type": type(exc).__name__}))
        return 1
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
