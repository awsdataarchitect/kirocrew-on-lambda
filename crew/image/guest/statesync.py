"""Durable crew state: mirror the crew home (KIROCREW_HOME) to an S3 prefix over the internet.

The VM keeps the home on local disk, so SQLite and file locks behave. The mirror is what survives the VM:
  restore()  at /run:                 S3 -> local, before the gateway starts
  push()     every minute, at /suspend and at /terminate: local -> S3, after the gateway has stopped for the last two

What is mirrored is decided by `included()`. The Strands project venv, caches, locks, sockets and temp files are image
content or per-process state and never leave the VM. SQLite files are copied through SQLite's online backup, never as raw
bytes, because a raw copy of an open database can be torn. A remote key is deleted only when the matching local file is
gone AND restore() completed on this VM, so a failed restore can never wipe the saved state.

Everything that decides what to transfer is a pure function (`included`, `plan_push`, `plan_pull`), so it is tested with
property tests. The S3 calls are thin and are exercised end to end against a real bucket.
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

# Path components (anywhere in the path) that never leave the VM.
_EXCLUDED_DIRS = frozenset({".venv", "__pycache__", "tmp", "cache", ".cache", "node_modules", "pi-gate-venv",
                            ".member-backups"})
# Top-level directories of the crew home that are not state. Secrets (the vault and signing keys are re-created per VM, and
# /run re-injects the NIM key from SSM), runtime files, image-style content KiroCrew rebuilds on first start (the browser
# harness venv is 158 MB, the Strands project and app clones are regenerated; `engines` is the KAS agent-engine bundle KiroCrew
# downloads on first use: 2,733 files and 182 MB measured 2026-11-13, 35 s of every boot's restore), and its own rotating backups.
_EXCLUDED_TOP = frozenset({".vault", "run", "browser-harness", "app-sources", "pi-gate", "backups", "engines"})
# File names and suffixes that are per-process state or secrets.
_EXCLUDED_NAMES = frozenset({"gateway.lock", "gateway.pid", ".DS_Store", ".local_secret", ".env", "token_signing.key",
                             "telemetry_salt", "gateway_id", "gateway.log", "kiro_pids.txt", "kiro_session_pids.txt"})
_EXCLUDED_SUFFIXES = (".lock", ".pid", ".sock", ".tmp", ".swp", "-wal", "-shm", "-journal", ".secret", ".key", ".pem")
# Anything whose file name says it is a credential stays on the VM, whatever the suffix (".app_secret" under apps/*/).
# Matched on the file name only, so a directory named "secrets-handling" in a user's notes is not affected.
_SECRET_NAME_RE = re.compile(r"(secret|token|credential|passwd|password|private[_-]?key|api[_-]?key)", re.IGNORECASE)
_SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
_KEY_RE = re.compile(r"^[A-Za-z0-9._\-/]+$")
MAX_OBJECT_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class Entry:
    size: int
    mtime: int  # whole seconds since the epoch


def included(rel: str) -> bool:
    """Whether a path relative to the crew home is mirrored."""
    if not rel or rel.startswith("/") or "\\" in rel or not _KEY_RE.fullmatch(rel):
        return False
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return False
    if any(p in _EXCLUDED_DIRS for p in parts[:-1]):
        return False
    if parts[0] in _EXCLUDED_TOP:  # only the first component: workspace/run/x is the user's, run/x is runtime
        return False
    name = parts[-1]
    if name in _EXCLUDED_NAMES or name.endswith(_EXCLUDED_SUFFIXES) or _SECRET_NAME_RE.search(name):
        return False
    return True


def plan_push(local: dict[str, Entry], remote: dict[str, Entry], *, may_delete: bool) -> tuple[list[str], list[str]]:
    """(keys to upload, keys to delete). Upload when absent remotely, or the size differs, or local is newer.

    Delete a remote key when it is no longer mirrored (the rules changed, e.g. a secret was found in the bucket) or its local
    file is gone. Both only when restore() completed on this VM.
    """
    upload = sorted(
        rel for rel, entry in local.items()
        if included(rel) and (rel not in remote or remote[rel].size != entry.size or entry.mtime > remote[rel].mtime)
    )
    delete = sorted(rel for rel in remote if not included(rel) or rel not in local) if may_delete else []
    return upload, delete


def plan_pull(local: dict[str, Entry], remote: dict[str, Entry]) -> list[str]:
    """Keys to download: everything remote that is absent locally or differs. Local files are never deleted."""
    return sorted(
        rel for rel, entry in remote.items()
        if included(rel) and (rel not in local or local[rel].size != entry.size or entry.mtime > local[rel].mtime)
    )


def scan_local(root: str) -> dict[str, Entry]:
    found: dict[str, Entry] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        at_top = os.path.abspath(dirpath) == os.path.abspath(root)
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS and not (at_top and d in _EXCLUDED_TOP)]
        for name in filenames:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            if not included(rel) or os.path.islink(path):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue  # removed while scanning
            if st.st_size <= MAX_OBJECT_BYTES:
                found[rel] = Entry(st.st_size, int(st.st_mtime))
    return found


class StateSync:
    def __init__(self, s3: object, bucket: str, prefix: str, root: str, *, workers: int = 8) -> None:
        if not prefix.endswith("/") or not _KEY_RE.fullmatch(prefix):
            raise ValueError("prefix must be a plain key ending in /")
        self.s3, self.bucket, self.prefix, self.root, self.workers = s3, bucket, prefix, root, workers
        self.restored = False
        self.last: dict[str, object] = {}

    def _remote(self) -> dict[str, Entry]:
        found: dict[str, Entry] = {}
        pages = self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=self.prefix)  # type: ignore[attr-defined]
        for page in pages:
            for obj in page.get("Contents", []):
                rel = obj["Key"][len(self.prefix):]
                # The object's own mtime metadata is the source file's mtime; fall back to LastModified.
                found[rel] = Entry(obj["Size"], int(obj["LastModified"].timestamp()))
        return found

    def _mtimes(self, keys: list[str]) -> dict[str, int]:
        """Source mtime recorded in object metadata (LastModified is upload time, which would always look newer)."""
        def head(rel: str) -> tuple[str, int]:
            meta = self.s3.head_object(Bucket=self.bucket, Key=self.prefix + rel)["Metadata"]  # type: ignore[attr-defined]
            return rel, int(meta.get("mtime", "0"))
        with ThreadPoolExecutor(self.workers) as pool:
            return dict(pool.map(head, keys))

    def _remote_exact(self) -> dict[str, Entry]:
        remote = self._remote()
        mt = self._mtimes(sorted(remote))
        return {rel: Entry(e.size, mt.get(rel, 0)) for rel, e in remote.items()}

    def restore(self) -> dict[str, object]:
        """S3 -> local. Idempotent; a failure leaves restored False so push() will not delete anything."""
        started = time.monotonic()
        os.makedirs(self.root, exist_ok=True)
        remote = self._remote_exact()
        todo = plan_pull(scan_local(self.root), remote)

        def get(rel: str) -> None:
            dest = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), suffix=".tmp")
            os.close(fd)
            try:
                self.s3.download_file(self.bucket, self.prefix + rel, tmp)  # type: ignore[attr-defined]
                os.utime(tmp, (remote[rel].mtime, remote[rel].mtime))
                os.replace(tmp, dest)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

        with ThreadPoolExecutor(self.workers) as pool:
            list(pool.map(get, todo))  # list() re-raises the first failure
        self.restored = True
        self.last = {"op": "restore", "files": len(todo), "remote": len(remote), "seconds": round(time.monotonic() - started, 1)}
        return self.last

    def _snapshot_db(self, path: str) -> str:
        """A consistent copy of a live SQLite database via the online backup API; the caller deletes it."""
        fd, tmp = tempfile.mkstemp(suffix=".sqlite-copy")
        os.close(fd)
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        return tmp

    def push(self) -> dict[str, object]:
        """local -> S3 for what changed. Deletes remote keys only if this VM completed restore()."""
        started = time.monotonic()
        local = scan_local(self.root)
        upload, delete = plan_push(local, self._remote_exact(), may_delete=self.restored)

        def put(rel: str) -> bool:
            path = os.path.join(self.root, rel)
            source, cleanup = path, None
            try:
                if rel.endswith(_SQLITE_SUFFIXES):
                    try:
                        source = cleanup = self._snapshot_db(path)
                    except sqlite3.DatabaseError:
                        source, cleanup = path, None  # not actually a database: copy the bytes
                self.s3.upload_file(  # type: ignore[attr-defined]
                    source, self.bucket, self.prefix + rel,
                    ExtraArgs={"Metadata": {"mtime": str(local[rel].mtime)}, "ServerSideEncryption": "AES256"},
                )
                return True
            except FileNotFoundError:
                return False  # the file was removed after the scan; it is not state any more
            except sqlite3.OperationalError:
                if not os.path.exists(path):
                    return False
                raise
            finally:
                if cleanup and os.path.exists(cleanup):
                    os.unlink(cleanup)

        with ThreadPoolExecutor(self.workers) as pool:
            sent = sum(pool.map(put, upload))
        vanished = len(upload) - sent
        for i in range(0, len(delete), 1000):
            batch = delete[i:i + 1000]
            self.s3.delete_objects(  # type: ignore[attr-defined]
                Bucket=self.bucket, Delete={"Objects": [{"Key": self.prefix + rel} for rel in batch], "Quiet": True})
        self.last = {"op": "push", "uploaded": sent, "vanished": vanished, "deleted": len(delete), "local": len(local),
                     "seconds": round(time.monotonic() - started, 1)}
        return self.last
