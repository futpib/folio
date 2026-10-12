from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import Connection
from .models import Snapshot


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise ValueError("state directories must belong to this user and not be symlinks")
    path.chmod(0o700)


@contextmanager
def exclusive(path: Path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


class Store:
    def __init__(self, directory: Path):
        private_directory(directory)
        self.directory = directory
        self.path = directory / "folio.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS connections (
                    id TEXT PRIMARY KEY, provider TEXT NOT NULL,
                    next_sync REAL NOT NULL DEFAULT 0, error TEXT, error_code TEXT
                );
                CREATE TABLE IF NOT EXISTS connection_revisions (
                    id TEXT PRIMARY KEY, revision TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS connection_accounts (
                    id TEXT PRIMARY KEY, account_identity TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS connection_credentials (
                    id TEXT PRIMARY KEY, credential_identity TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, connection TEXT NOT NULL, state TEXT NOT NULL,
                    created REAL NOT NULL, started REAL, finished REAL, since TEXT,
                    error TEXT, error_code TEXT, records INTEGER
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_sync_per_connection
                    ON jobs(connection) WHERE state IN ('queued', 'running');
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, connection TEXT NOT NULL,
                    provider TEXT NOT NULL, job TEXT, created REAL NOT NULL,
                    event TEXT NOT NULL, fields TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS logs_connection ON logs(connection, id);
                CREATE TABLE IF NOT EXISTS snapshots (
                    connection TEXT NOT NULL, resource TEXT NOT NULL, synced REAL NOT NULL,
                    complete INTEGER NOT NULL, coverage TEXT NOT NULL,
                    PRIMARY KEY (connection, resource)
                );
                CREATE TABLE IF NOT EXISTS snapshot_scopes (
                    connection TEXT NOT NULL, resource TEXT NOT NULL, market TEXT NOT NULL,
                    synced REAL, status TEXT NOT NULL,
                    PRIMARY KEY (connection, resource, market)
                );
                CREATE TABLE IF NOT EXISTS records (
                    connection TEXT NOT NULL, resource TEXT NOT NULL, id TEXT NOT NULL,
                    occurred_at TEXT, data TEXT NOT NULL, observed REAL NOT NULL,
                    PRIMARY KEY (connection, resource, id)
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            if "complete" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN complete INTEGER")
            if "warnings" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN warnings TEXT NOT NULL DEFAULT '[]'")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def register(self, connection: Connection) -> None:
        revision = (
            hashlib.sha256(connection.ibkr_config.read_bytes()).hexdigest()
            if connection.ibkr_config is not None
            else None
        )
        with self.db() as db:
            existing = db.execute(
                "SELECT provider FROM connections WHERE id=?", (connection.id,)
            ).fetchone()
            if existing and existing[0] != connection.provider:
                raise ValueError("changing a connection's provider requires a new connection name")
            db.execute(
                "INSERT OR IGNORE INTO connections(id,provider) VALUES (?,?)",
                (connection.id, connection.provider),
            )
            if revision is not None:
                previous = db.execute(
                    "SELECT revision FROM connection_revisions WHERE id=?", (connection.id,)
                ).fetchone()
                if previous is None or previous[0] != revision:
                    # Cached IBKR data must never outlive a narrowed exposure policy.
                    db.execute("DELETE FROM records WHERE connection=?", (connection.id,))
                    db.execute("DELETE FROM snapshots WHERE connection=?", (connection.id,))
                    db.execute(
                        "INSERT OR REPLACE INTO connection_revisions VALUES (?,?)",
                        (connection.id, revision),
                    )

    def status(self, connection: str) -> dict[str, Any]:
        with self.db() as db:
            row = dict(db.execute("SELECT * FROM connections WHERE id=?", (connection,)).fetchone())
            row["snapshots"] = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM snapshots WHERE connection=? ORDER BY resource", (connection,)
                )
            ]
            row["scopes"] = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM snapshot_scopes WHERE connection=? ORDER BY resource,market",
                    (connection,),
                )
            ]
            return row

    def append_log(self, connection, job, event, fields):
        created = time.time()
        with self.db() as db:
            cursor = db.execute(
                "INSERT INTO logs(connection,provider,job,created,event,fields) "
                "VALUES(?,?,?,?,?,?)",
                (connection.id, connection.provider, job, created, event, json.dumps(fields)),
            )
            ident = cursor.lastrowid
            # Bound retention without deleting jobs, snapshots or account records.
            db.execute(
                "DELETE FROM logs WHERE connection=? AND id < (SELECT id FROM logs "
                "WHERE connection=? ORDER BY id DESC LIMIT 1 OFFSET 9999)",
                (connection.id, connection.id),
            )
        return {
            "id": ident,
            "connection": connection.id,
            "provider": connection.provider,
            "job": job,
            "created": created,
            "event": event,
            "fields": fields,
        }

    def logs(self, connection, *, after=0, limit=1000, job=None):
        with self.db() as db:
            result = db.execute(
                "SELECT * FROM logs WHERE connection=? AND id>? AND (? IS NULL OR job=?) "
                "ORDER BY id LIMIT ?",
                (connection, after, job, job, limit),
            ).fetchall()
        items = [{**dict(row), "fields": json.loads(row["fields"])} for row in result]
        return {"items": items, "next_after": items[-1]["id"] if items else after}

    def queue(self, connection: str, since: str | None) -> dict[str, Any]:
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM jobs WHERE connection=? AND state IN ('queued','running')",
                (connection,),
            ).fetchone()
            if row:
                return self.job_value(row)
            ident = str(uuid.uuid4())
            db.execute(
                "INSERT INTO jobs(id,connection,state,created,since) VALUES (?,?,'queued',?,?)",
                (ident, connection, time.time(), since),
            )
            return self.job_value(db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone())

    @staticmethod
    def job_value(row):
        return {
            **dict(row),
            "complete": bool(row["complete"]) if row["complete"] is not None else None,
            "warnings": json.loads(row["warnings"]),
        }

    def job(self, ident: str) -> dict[str, Any] | None:
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone()
            return self.job_value(row) if row else None

    def retry(self, connection: str) -> None:
        with self.db() as db:
            db.execute("UPDATE connections SET next_sync=0 WHERE id=?", (connection,))
        self.queue(connection, None)

    def claim(self, connection: str) -> dict[str, Any] | None:
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT jobs.* FROM jobs JOIN connections
                ON connections.id=jobs.connection WHERE connection=? AND state='queued'
                AND next_sync <= ?""",
                (connection, time.time()),
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE jobs SET state='running',started=? WHERE id=?", (time.time(), row["id"])
            )
            return dict(row)

    def recover(self, connection: str) -> None:
        with self.db() as db:
            db.execute(
                """UPDATE jobs SET state='failed',finished=?,error_code='interrupted',
                error='Worker stopped during sync; stored data is unchanged.'
                WHERE connection=? AND state='running'""",
                (time.time(), connection),
            )

    def finish(self, job: dict, snapshot: Snapshot, cooldown: int) -> None:
        now = time.time()
        with self.db() as db:
            if snapshot.account_identity is not None or snapshot.credential_identity is not None:
                account = db.execute(
                    "SELECT account_identity FROM connection_accounts WHERE id=?",
                    (job["connection"],),
                ).fetchone()
                credential = db.execute(
                    "SELECT credential_identity FROM connection_credentials WHERE id=?",
                    (job["connection"],),
                ).fetchone()
                same_key = (
                    snapshot.credential_identity is not None
                    and credential is not None
                    and credential[0] == snapshot.credential_identity
                )
                same_account = (
                    snapshot.account_identity is not None
                    and account is not None
                    and account[0] == snapshot.account_identity
                )
                account_changed = (
                    snapshot.account_identity is not None
                    and account is not None
                    and not same_account
                )
                if account_changed or (not same_account and not same_key):
                    # A replacement API key may belong to a different account.
                    db.execute("DELETE FROM records WHERE connection=?", (job["connection"],))
                    db.execute("DELETE FROM snapshots WHERE connection=?", (job["connection"],))
                    db.execute(
                        "DELETE FROM snapshot_scopes WHERE connection=?", (job["connection"],)
                    )
                    db.execute("DELETE FROM connection_accounts WHERE id=?", (job["connection"],))
                if snapshot.account_identity is not None:
                    db.execute(
                        "INSERT OR REPLACE INTO connection_accounts VALUES (?,?)",
                        (job["connection"], snapshot.account_identity),
                    )
                if snapshot.credential_identity is not None:
                    db.execute(
                        "INSERT OR REPLACE INTO connection_credentials VALUES (?,?)",
                        (job["connection"], snapshot.credential_identity),
                    )
            if snapshot.scopes is not None:
                markets = sorted({scope.market for scope in snapshot.scopes})
                placeholders = ",".join("?" for _ in markets)
                # Removed markets cease to be exposed; denied configured markets stay cached.
                db.execute(
                    f"DELETE FROM records WHERE connection=? "
                    f"AND json_extract(data,'$.market') NOT IN ({placeholders})",
                    (job["connection"], *markets),
                )
                db.execute(
                    f"DELETE FROM snapshot_scopes WHERE connection=? "
                    f"AND market NOT IN ({placeholders})",
                    (job["connection"], *markets),
                )
                for scope in snapshot.scopes:
                    if scope.refreshed and scope.resource in {"accounts", "balances", "positions"}:
                        db.execute(
                            "DELETE FROM records WHERE connection=? AND resource=? "
                            "AND json_extract(data,'$.market')=?",
                            (job["connection"], scope.resource, scope.market),
                        )
                    db.execute(
                        "INSERT INTO snapshot_scopes VALUES (?,?,?,COALESCE(?,"
                        "(SELECT MAX(observed) FROM records WHERE connection=? AND resource=? "
                        "AND json_extract(data,'$.market')=?)),?) "
                        "ON CONFLICT(connection,resource,market) DO UPDATE SET "
                        "synced=COALESCE(?,snapshot_scopes.synced),status=excluded.status",
                        (
                            job["connection"],
                            scope.resource,
                            scope.market,
                            now if scope.refreshed else None,
                            job["connection"],
                            scope.resource,
                            scope.market,
                            scope.status,
                            now if scope.refreshed else None,
                        ),
                    )
            for resource in snapshot.resources:
                # Current holdings and balances are snapshots; histories accumulate by source ID.
                if snapshot.scopes is None and resource in {"accounts", "balances", "positions"}:
                    db.execute(
                        "DELETE FROM records WHERE connection=? AND resource=?",
                        (job["connection"], resource),
                    )
                synced = now
                sections = [s for s in snapshot.scopes or () if s.resource == resource]
                if sections and not any(section.refreshed for section in sections):
                    previous = db.execute(
                        "SELECT synced FROM snapshots WHERE connection=? AND resource=?",
                        (job["connection"], resource),
                    ).fetchone()
                    synced = previous[0] if previous else None
                if synced is not None:
                    db.execute(
                        "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?)",
                        (job["connection"], resource, synced, snapshot.complete, snapshot.coverage),
                    )
            for record in snapshot.records:
                db.execute(
                    "INSERT OR REPLACE INTO records VALUES (?,?,?,?,?,?)",
                    (
                        job["connection"],
                        record.resource,
                        record.id,
                        record.occurred_at,
                        json.dumps(record.model_dump(mode="json")["data"], allow_nan=False),
                        now,
                    ),
                )
            db.execute(
                "UPDATE connections SET next_sync=?,error=NULL,error_code=NULL WHERE id=?",
                (now + cooldown, job["connection"]),
            )
            db.execute(
                "UPDATE jobs SET state='completed',finished=?,records=?,complete=?,warnings=? "
                "WHERE id=?",
                (
                    now,
                    len(snapshot.records),
                    snapshot.complete,
                    json.dumps(snapshot.warnings),
                    job["id"],
                ),
            )

    def fail(self, job: dict, code: str, message: str, cooldown: int) -> None:
        with self.db() as db:
            db.execute(
                "UPDATE jobs SET state='failed',finished=?,error=?,error_code=? WHERE id=?",
                (time.time(), message, code, job["id"]),
            )
            db.execute(
                "UPDATE connections SET next_sync=?,error=?,error_code=? WHERE id=?",
                (time.time() + cooldown, message, code, job["connection"]),
            )

    def read(self, connection: str, resource: str, since: str | None, limit: int, offset: int):
        with self.db() as db:
            meta = db.execute(
                "SELECT * FROM snapshots WHERE connection=? AND resource=?", (connection, resource)
            ).fetchone()
            rows = db.execute(
                """SELECT * FROM records WHERE connection=? AND resource=?
                AND (? IS NULL OR occurred_at IS NULL OR occurred_at >= ?)
                ORDER BY COALESCE(occurred_at,''), id
                LIMIT ? OFFSET ?""",
                (connection, resource, since, since, limit + 1, offset),
            ).fetchall()
            return {
                "connection": connection,
                "resource": resource,
                "snapshot": dict(meta) if meta else None,
                "scopes": [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM snapshot_scopes WHERE connection=? AND resource=? "
                        "ORDER BY market",
                        (connection, resource),
                    )
                ],
                "items": [
                    {
                        "id": r["id"],
                        "occurred_at": r["occurred_at"],
                        "observed_at": r["observed"],
                        **json.loads(r["data"]),
                    }
                    for r in rows[:limit]
                ],
                "next_offset": offset + limit if len(rows) > limit else None,
                "undated_included": since is not None,
            }
