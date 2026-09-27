"""Cache SQLite avec durée de validité absolue, sans rajeunissement des données.

Les analyses gardent leur analysis_id, reference_at et completed_at d'origine.
Une lecture ne modifie jamais l'expiration. La clé doit inclure modèle/version
et empreinte du contrat ; le prix sera de toute façon relu par B avant la gate.
Ne jamais cacher une autorisation, un solde, ou le résultat supposé d'un envoi.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from tools.tracker import Tracker, encode, finite, fingerprint


@dataclass(frozen=True)
class CacheEntry:
    value: Any
    created_at: float
    expires_at: float


class Cache:
    def __init__(self, tracker: Tracker):
        self.tracker = tracker

    @staticmethod
    def key(*parts: Any) -> str:
        return fingerprint(parts)

    def get(self, namespace: str, key: str) -> CacheEntry | None:
        now = self.tracker.clock()
        with self.tracker.connection() as db:
            row = db.execute("SELECT * FROM d_cache WHERE namespace=? AND key=?", (namespace, key)).fetchone()
        if not row or not row["created_at"] <= now < row["expires_at"]:
            self.tracker.log_event("D", "CACHE_MISS", payload={"namespace": namespace, "key": key,
                "cause": "ABSENT" if not row else "EXPIRED_OR_FUTURE"})
            return None
        try:
            entry = CacheEntry(json.loads(row["payload"]), row["created_at"], row["expires_at"])
            self.tracker.log_event("D", "CACHE_HIT", payload={"namespace": namespace, "key": key,
                "original_created_at": entry.created_at, "original_expires_at": entry.expires_at})
            return entry
        except (ValueError, TypeError):
            # Un cache illisible est un miss, jamais une autorisation implicite.
            return None

    def put(self, namespace: str, key: str, value: Any, *, created_at: float, ttl: float) -> None:
        if namespace not in ("analysis", "research") or not key:
            raise ValueError("Seuls analysis/research sont mis en cache")
        finite(created_at, "created_at", minimum=0)
        finite(ttl, "ttl", minimum=0)
        if created_at > self.tracker.clock() or ttl <= 0:
            raise ValueError("Horodatage futur ou TTL nul")
        with self.tracker.transaction() as db:
            db.execute("""INSERT INTO d_cache VALUES(?,?,?,?,?)
                ON CONFLICT(namespace,key) DO UPDATE SET created_at=excluded.created_at,
                expires_at=excluded.expires_at,payload=excluded.payload""",
                       (namespace, key, created_at, created_at + ttl, encode(value)))
            self.tracker.log_event("D", "CACHE_WRITE", payload={"namespace": namespace, "key": key,
                "original_created_at": created_at, "expires_at": created_at+ttl}, db=db)

    def invalidate(self, namespace: str, key: str) -> None:
        with self.tracker.transaction() as db:
            db.execute("DELETE FROM d_cache WHERE namespace=? AND key=?", (namespace, key))
            self.tracker.log_event("D", "CACHE_INVALIDATED", payload={"namespace": namespace, "key": key}, db=db)

    def purge_expired(self) -> int:
        with self.tracker.transaction() as db:
            cursor = db.execute("DELETE FROM d_cache WHERE expires_at<=?", (self.tracker.clock(),))
            self.tracker.log_event("D", "CACHE_PURGED", payload={"count": cursor.rowcount}, db=db)
            return cursor.rowcount
