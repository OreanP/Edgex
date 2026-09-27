"""Journal global EdgeX et historique durable : opérations A/B/C/D, prévisions et ordres.

Toutes les écritures critiques utilisent SQLite sur disque et BEGIN IMMEDIATE.
Budget, cache et intégration partagent cette base ; aucun état de sécurité ne
repose uniquement sur une variable Python. Les montants sont conservés en
millionièmes de Mana, arrondis vers le haut pour les engagements.

Ce module ne contacte ni OpenAI ni Manifold. Les résultats d'exécution et les
paiements doivent être confirmés par B, jamais déduits d'un timeout ou d'une
prévision. Il conserve aussi les prévisions refusées, afin de ne pas évaluer
uniquement les paris sélectionnés. Voir metrics() pour le Brier Score.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
import math
import re
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterator
from uuid import uuid4

SCALE = 1_000_000
UNRESOLVED = ("SENDING", "UNKNOWN")
LIVE = ("RESERVED", "SENDING", "UNKNOWN", "OPEN", "PARTIAL")
TERMINAL = ("FILLED", "REJECTED", "CANCELLED", "EXPIRED")


class StateConflict(RuntimeError):
    """Une écriture contredirait une information déjà enregistrée."""


def finite(value: float, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{name}: nombre attendu")
    number = float(value)
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise ValueError(f"{name}: valeur invalide")
    return number


def units(mana: float) -> int:
    """Conversion conservatrice : ne jamais sous-réserver un débit positif."""
    finite(mana, "mana", minimum=0)
    result = int((Decimal(str(mana)) * SCALE).to_integral_value(rounding=ROUND_CEILING))
    if result >= 2**62:
        raise ValueError("Montant hors limites SQLite")
    return result


def available_units(value: float) -> int:
    """Arrondir une ressource disponible vers le bas (ne jamais la surestimer)."""
    finite(value, "available_mana")
    result = int((Decimal(str(value)) * SCALE).to_integral_value(rounding=ROUND_FLOOR))
    if abs(result) >= 2**62:
        raise ValueError("Montant hors limites SQLite")
    return result


def mana(value: int) -> float:
    return value / SCALE


def utc_day(timestamp: float) -> str:
    return datetime.fromtimestamp(finite(timestamp, "timestamp", minimum=0), timezone.utc).date().isoformat()


def plain(value: Any) -> Any:
    if is_dataclass(value):
        return plain(asdict(value))
    if hasattr(value, "model_dump"):
        return plain(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def encode(value: Any) -> str:
    return json.dumps(plain(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS d_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS d_analyses(
 id TEXT PRIMARY KEY, market_id TEXT NOT NULL, reference_at REAL NOT NULL,
 payload TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS d_decisions(
 id TEXT PRIMARY KEY, analysis_id TEXT NOT NULL REFERENCES d_analyses(id),
 at REAL NOT NULL, approved INTEGER NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS d_orders(
 id TEXT PRIMARY KEY, analysis_id TEXT UNIQUE NOT NULL REFERENCES d_analyses(id),
 market_id TEXT NOT NULL, family_id TEXT NOT NULL, created_at REAL NOT NULL,
 authorization TEXT NOT NULL, decision TEXT NOT NULL, state TEXT NOT NULL,
 spent INTEGER NOT NULL DEFAULT 0, remaining INTEGER NOT NULL,
 external_id TEXT UNIQUE, observed_at REAL NOT NULL DEFAULT 0,
 settled_at REAL, payout INTEGER, settlement_ref TEXT);
CREATE TABLE IF NOT EXISTS d_events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
 kind TEXT NOT NULL, ref TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS d_resolutions(
 market_id TEXT PRIMARY KEY, outcome TEXT NOT NULL, resolved_at REAL NOT NULL,
 evidence_ref TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS d_budget_sessions(
 id TEXT PRIMARY KEY, market_id TEXT NOT NULL, cycle_id TEXT NOT NULL,
 started_at REAL NOT NULL, day TEXT NOT NULL, state TEXT NOT NULL,
 policy_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS d_budget_calls(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES d_budget_sessions(id),
 day TEXT NOT NULL, phase TEXT NOT NULL, started_at REAL NOT NULL,
 web_reserved INTEGER NOT NULL, output_reserved INTEGER NOT NULL,
 state TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS d_cache(
 namespace TEXT NOT NULL, key TEXT NOT NULL, created_at REAL NOT NULL,
 expires_at REAL NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(namespace, key));
CREATE TABLE IF NOT EXISTS d_logbook(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
 recorded_at REAL NOT NULL, occurred_at REAL NOT NULL,
 component TEXT NOT NULL, operation TEXT NOT NULL, status TEXT NOT NULL,
 run_id TEXT NOT NULL, batch_id TEXT, market_id TEXT, analysis_id TEXT,
 authorization_id TEXT, operation_id TEXT, parent_id TEXT, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS d_logbook_batch ON d_logbook(batch_id,seq);
CREATE INDEX IF NOT EXISTS d_logbook_analysis ON d_logbook(analysis_id,seq);
CREATE TABLE IF NOT EXISTS d_batches(
 id TEXT PRIMARY KEY, input_digest TEXT NOT NULL, at REAL NOT NULL,
 payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS d_decisions_analysis ON d_decisions(analysis_id, at);
CREATE INDEX IF NOT EXISTS d_budget_market ON d_budget_sessions(market_id, started_at);
"""


class Tracker:
    """Un registre par compte dédié. Ne pas effacer la base pour redémarrer.

    Les connexions sont courtes et propres à chaque opération : utilisable depuis
    plusieurs threads/processus partageant le même fichier local. Une seule
    instance d'exécution réseau est toutefois recommandée pour le MVP.
    """

    def __init__(self, path: str | Path = "data/edgex_d.sqlite3", *, clock=time.time, run_id: str | None = None):
        if str(path) == ":memory:":
            raise ValueError("D exige une base sur disque (utiliser un dossier temporaire pour les tests)")
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.run_id = run_id or uuid4().hex
        self._log_context = ContextVar(f"edgex_log_{id(self)}", default={})
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)
            db.execute("INSERT OR IGNORE INTO d_meta VALUES('schema_version','1')")
            if self.meta("schema_version", db) != "1":
                raise StateConflict("Version de base non prise en charge")
            db.execute("INSERT OR IGNORE INTO d_meta VALUES('logbook_version','2')")
        self.log_event("SYSTEM", "PROCESS_STARTED", status="SUCCEEDED",
                       payload={"logbook_version": 2})

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Vérification + réservation indivisibles ; aucun réseau ici."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()

    @staticmethod
    def meta(key: str, db: sqlite3.Connection) -> str | None:
        row = db.execute("SELECT value FROM d_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def set_meta(key: str, value: str, db: sqlite3.Connection) -> None:
        db.execute("INSERT INTO d_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def bind(self, key: str, value: str, db: sqlite3.Connection) -> None:
        old = self.meta(key, db)
        if old is not None and old != value:
            raise StateConflict(f"{key} diffère de la configuration enregistrée")
        self.set_meta(key, value, db)

    @staticmethod
    def _redact(value: Any) -> Any:
        """Réduire les fuites usuelles ; ne jamais transmettre de réponse brute.

        Ce filtre est une défense supplémentaire, pas une détection exhaustive
        de secrets. Les appelants ne doivent journaliser que des métadonnées.
        """
        value = plain(value)
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                normalized = key.lower().replace("-", "_")
                secret = any(word in normalized for word in ("api_key", "password", "secret", "access_token", "refresh_token"))
                hidden_text = normalized in ("prompt", "instructions", "reasoning", "chain_of_thought", "input", "messages")
                if secret or hidden_text or (normalized in ("authorization", "cookie") and isinstance(item, str)):
                    result[key] = "[REDACTED]"
                else:
                    result[key] = Tracker._redact(item)
            return result
        if isinstance(value, list):
            return [Tracker._redact(x) for x in value]
        if isinstance(value, str):
            value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[REDACTED]", value)
            value = re.sub(r"(?i)([?&](?:token|key|api_key|secret|signature)=)[^&#\s]*", r"\1[REDACTED]", value)
            return value
        return value

    @contextmanager
    def log_context(self, **links) -> Iterator[None]:
        """Lier les opérations imbriquées sans demander au LLM leurs IDs."""
        allowed = {"batch_id", "market_id", "analysis_id", "authorization_id", "parent_id"}
        if set(links) - allowed:
            raise ValueError("Identifiant de corrélation inconnu")
        token = self._log_context.set({**self._log_context.get(), **links})
        try:
            yield
        finally:
            self._log_context.reset(token)

    def log_event(self, component: str, operation: str, *, status: str = "OBSERVED",
                  payload: Any = None, occurred_at: float | None = None,
                  operation_id: str | None = None, db=None, **links) -> str:
        """Événement append-only, horloges UTC secondes et séquence locale.

        occurred_at est l'instant effectivement connu de l'événement ; par
        défaut c'est l'observation locale. recorded_at est l'écriture locale.
        Ne pas inventer les heures internes des recherches du fournisseur.
        C et les actions hors Pipeline peuvent employer cette même méthode.
        """
        if component not in ("A", "B", "C", "D", "SYSTEM") or not operation:
            raise ValueError("Composant/opération invalide")
        if status not in ("STARTED", "SUCCEEDED", "FAILED", "OBSERVED", "SKIPPED"):
            raise ValueError("Statut de journal invalide")
        allowed = {"batch_id", "market_id", "analysis_id", "authorization_id", "parent_id"}
        if set(links) - allowed:
            raise ValueError("Identifiant de corrélation inconnu")
        ctx = {**self._log_context.get(), **links}
        now = finite(self.clock(), "recorded_at", minimum=0)
        at = now if occurred_at is None else finite(occurred_at, "occurred_at", minimum=0)
        if at > now:
            raise ValueError("Événement situé dans le futur")
        event_id = uuid4().hex
        params = (event_id, now, at, component, operation, status, self.run_id,
                  ctx.get("batch_id"), ctx.get("market_id"), ctx.get("analysis_id"),
                  ctx.get("authorization_id"), operation_id, ctx.get("parent_id"),
                  encode(self._redact({} if payload is None else payload)))
        statement = """INSERT INTO d_logbook(event_id,recorded_at,occurred_at,
          component,operation,status,run_id,batch_id,market_id,analysis_id,
          authorization_id,operation_id,parent_id,payload)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
        if db is not None:
            db.execute(statement, params)
        else:
            with self.transaction() as own:
                own.execute(statement, params)
        return event_id

    @contextmanager
    def operation(self, component: str, name: str, *, payload: dict | None = None,
                  **links) -> Iterator[dict]:
        """Tracer début/fin/erreur et durée monotone, sans conserver le prompt.

        Le dictionnaire rendu peut recevoir une synthèse du résultat. Un
        démarrage sans fin est visible après crash : on ne fabrique pas de fin.
        Ne pas ouvrir cette portée réseau dans une transaction de réservation.
        """
        op_id, start = uuid4().hex, time.perf_counter()
        details = dict(payload or {})
        with self.log_context(**links):
            self.log_event(component, name, status="STARTED", payload=details, operation_id=op_id)
            try:
                with self.log_context(parent_id=op_id):
                    yield details
            except BaseException as exc:
                self.log_event(component, name, status="FAILED", operation_id=op_id,
                               payload={"duration_ms": (time.perf_counter()-start)*1000,
                                        "error_type": type(exc).__name__})
                raise
            else:
                details["duration_ms"] = (time.perf_counter()-start)*1000
                self.log_event(component, name, status="SUCCEEDED", payload=details, operation_id=op_id)

    def logbook(self, *, after_seq: int = 0, limit: int = 1000, **filters) -> list[dict]:
        """Lecture chronologique paginée pour C (seq, pas l'horloge, est l'ordre)."""
        if isinstance(limit, bool) or not 1 <= limit <= 10_000 or after_seq < 0:
            raise ValueError("Pagination invalide")
        allowed = {"batch_id", "market_id", "analysis_id", "authorization_id", "run_id", "component", "operation_id"}
        if set(filters) - allowed:
            raise ValueError("Filtre inconnu")
        where, values = ["seq> ?"], [after_seq]
        for key, value in filters.items():
            where.append(f"{key}=?")
            values.append(value)
        with self.connection() as db:
            rows = db.execute("SELECT * FROM d_logbook WHERE " + " AND ".join(where) +
                              " ORDER BY seq LIMIT ?", [*values, limit]).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"]),
                 "timestamp_utc": datetime.fromtimestamp(r["occurred_at"], timezone.utc).isoformat()}
                for r in rows]

    def export_logbook(self, path: str | Path, **filters) -> int:
        """Exporter les événements présents au début de la lecture, en JSONL."""
        with self.connection() as db:
            upper = db.execute("SELECT coalesce(max(seq),0) FROM d_logbook").fetchone()[0]
        after, count = 0, 0
        with Path(path).open("w", encoding="utf-8") as stream:
            while after < upper:
                rows = self.logbook(after_seq=after, limit=1000, **filters)
                rows = [r for r in rows if r["seq"] <= upper]
                if not rows:
                    break
                for row in rows:
                    stream.write(encode(row) + "\n")
                    count += 1
                after = rows[-1]["seq"]
        return count

    def event(self, kind: str, ref: str, payload: Any, db: sqlite3.Connection) -> None:
        # Conserver l'interface V1 et en publier une vue dans le journal global.
        db.execute("INSERT INTO d_events(at,kind,ref,payload) VALUES(?,?,?,?)",
                   (self.clock(), kind, ref, encode(payload)))
        component = "A" if kind.startswith(("OPENAI_", "WEB_TOOL_")) else "D"
        data = plain(payload)
        links = {k: data[k] for k in ("market_id", "analysis_id", "authorization_id") if isinstance(data, dict) and k in data}
        if kind.startswith(("OPENAI_", "WEB_TOOL_", "ANALYSIS_")):
            links.setdefault("analysis_id", ref)
        if isinstance(data, dict) and data.get("cycle_id"):
            links["batch_id"] = data["cycle_id"]
        authorization = data.get("authorization") if isinstance(data, dict) else None
        if not isinstance(authorization, dict):
            order = db.execute("SELECT authorization FROM d_orders WHERE id=?", (ref,)).fetchone()
            authorization = json.loads(order[0]) if order else None
        if isinstance(authorization, dict):
            for key in ("authorization_id", "analysis_id", "market_id", "batch_id"):
                if authorization.get(key) is not None:
                    links[key] = authorization[key]
        if kind.startswith("ANALYSIS_"):
            session = db.execute("SELECT market_id,cycle_id FROM d_budget_sessions WHERE id=?", (ref,)).fetchone()
            if session:
                links.update(market_id=session["market_id"], batch_id=session["cycle_id"])
        state = "FAILED" if "FAILED" in kind else "OBSERVED"
        self.log_event(component, kind, status=state, payload={"ref": ref, "details": data}, db=db, **links)

    def batch(self, batch_id: str, db=None) -> dict | None:
        if db is None:
            with self.connection() as own:
                return self.batch(batch_id, own)
        row = db.execute("SELECT * FROM d_batches WHERE id=?", (batch_id,)).fetchone()
        return {**dict(row), "payload": json.loads(row["payload"])} if row else None

    def save_batch(self, batch_id: str, input_digest: str, payload: Any, db) -> None:
        """À appeler dans la MÊME transaction que toutes les réservations du lot."""
        db.execute("INSERT INTO d_batches VALUES(?,?,?,?)", (batch_id, input_digest, self.clock(), encode(payload)))
        self.log_event("D", "BATCH_ALLOCATED", status="SUCCEEDED", batch_id=batch_id,
                       payload=payload, db=db)

    def pause(self, reason: str = "MANUAL_STOP") -> None:
        with self.transaction() as db:
            self.set_meta("paused", reason, db)
            self.event("PAUSE", "system", {"reason": reason}, db)

    def resume(self) -> None:
        """Action opérateur, jamais exposée à A ; refuse les envois non réconciliés."""
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM d_orders WHERE state IN ('SENDING','UNKNOWN') LIMIT 1").fetchone():
                raise StateConflict("Réconcilier les ordres inconnus avant reprise")
            self.set_meta("paused", "", db)
            self.event("RESUME", "system", {}, db)

    def record_analysis(self, envelope: Any) -> None:
        data = plain(envelope)
        for name in ("market_probability", "initial_probability", "final_probability"):
            if finite(data[name], name, minimum=0) > 1:
                raise ValueError(f"{name}: probabilité supérieure à 1")
        for name in ("reference_at", "completed_at"):
            finite(data[name], name, minimum=0)
        if data["completed_at"] < data["reference_at"]:
            raise ValueError("Dates de prévision incohérentes")
        # Conserver explicitement l'edge de prévision, même pour SKIP/refus.
        # La gate calculera séparément l'edge au prix actualisé d'exécution.
        data["edge"] = data["final_probability"] - data["market_probability"]
        digest = fingerprint(data)
        with self.transaction() as db:
            row = db.execute("SELECT digest FROM d_analyses WHERE id=?", (data["analysis_id"],)).fetchone()
            if row and row[0] != digest:
                raise StateConflict("ANALYSIS_ID_COLLISION : même id, contenu différent")
            db.execute("INSERT OR IGNORE INTO d_analyses VALUES(?,?,?,?,?)",
                       (data["analysis_id"], data["market_id"], data["reference_at"], encode(data), digest))
            if not row:
                self.log_event("D", "ANALYSIS_RECORDED", status="SUCCEEDED",
                               analysis_id=data["analysis_id"], market_id=data["market_id"],
                               payload={"digest": digest, "reference_at": data["reference_at"],
                                        "completed_at": data["completed_at"], "decision": data["decision"]}, db=db)

    def analysis(self, analysis_id: str) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT payload FROM d_analyses WHERE id=?", (analysis_id,)).fetchone()
        if not row:
            raise KeyError(analysis_id)
        return json.loads(row[0])

    def orders(self, db: sqlite3.Connection | None = None) -> list[dict]:
        if db is None:
            with self.connection() as own:
                return self.orders(own)
        result = []
        for row in db.execute("SELECT * FROM d_orders ORDER BY created_at,id"):
            item = dict(row)
            item["authorization"] = json.loads(item["authorization"])
            item["decision"] = json.loads(item["decision"])
            result.append(item)
        return result

    def order(self, authorization_id: str, db: sqlite3.Connection | None = None) -> dict:
        for item in self.orders(db):
            if item["id"] == authorization_id:
                return item
        raise KeyError(authorization_id)

    def order_for_analysis(self, analysis_id: str, db: sqlite3.Connection) -> dict | None:
        return next((r for r in self.orders(db) if r["analysis_id"] == analysis_id), None)

    def save_decision(self, decision: Any, db: sqlite3.Connection) -> None:
        data = plain(decision)
        db.execute("INSERT INTO d_decisions VALUES(?,?,?,?,?)",
                   (data["decision_id"], data["analysis_id"], data["decided_at"],
                    int(data["status"] == "APPROVE"), encode(data)))
        self.event("GATE_DECISION", data["decision_id"], data, db)

    def reserve(self, decision: Any, db: sqlite3.Connection) -> None:
        data = plain(decision)
        auth = data["authorization"]
        db.execute("""INSERT INTO d_orders
            (id,analysis_id,market_id,family_id,created_at,authorization,decision,state,remaining)
            VALUES(?,?,?,?,?,?,?,'RESERVED',?)""",
                   (auth["authorization_id"], data["analysis_id"], auth["market_id"],
                    auth["family_id"], data["decided_at"], encode(auth), encode(data),
                    units(auth["max_total_debit"])))
        self.save_decision(data, db)

    def expire_unsent(self, now: float, db: sqlite3.Connection) -> None:
        for row in self.orders(db):
            if row["state"] == "RESERVED" and now >= row["authorization"]["valid_until"]:
                db.execute("UPDATE d_orders SET state='EXPIRED',remaining=0 WHERE id=?", (row["id"],))
                self.event("AUTHORIZATION_EXPIRED", row["id"], {}, db)
        # OPEN/PARTIAL/UNKNOWN ne sont jamais libérés sur une simple horloge.

    def unknown(self, authorization_id: str, reason: str) -> None:
        with self.transaction() as db:
            row = self.order(authorization_id, db)
            if row["state"] not in ("SENDING", "UNKNOWN", "OPEN", "PARTIAL"):
                raise StateConflict("Seul un ordre envoyé peut être UNKNOWN")
            db.execute("UPDATE d_orders SET state='UNKNOWN' WHERE id=?", (authorization_id,))
            self.event("EXECUTION_UNKNOWN", authorization_id, {"reason": reason}, db)

    def apply_execution(self, authorization_id: str, report: Any) -> None:
        """Enregistrer une observation cumulative et vérifiée de B, sans retry.

        remaining_debit couvre tous frais restants. Pour annuler/libérer, B doit
        confirmer un état terminal. Un rapport ancien ou incohérent est refusé.
        """
        r = plain(report)
        state = r["state"]
        if state not in ("FILLED", "OPEN", "PARTIAL", "REJECTED", "CANCELLED", "EXPIRED"):
            raise ValueError("État non confirmé")
        spent, remaining = units(r["cumulative_debit"]), units(r["remaining_debit"])
        at = finite(r["observed_at"], "observed_at", minimum=0)
        if at > self.clock() or not r.get("evidence_ref"):
            raise ValueError("Confirmation datée et référence de B obligatoires")
        external = r.get("external_id")
        if (spent or remaining or state in ("FILLED", "OPEN", "PARTIAL")) and not external:
            raise ValueError("Identifiant Manifold manquant")
        if state in TERMINAL and remaining != 0:
            raise ValueError("Un état terminal ne peut garder de reliquat")
        if state == "REJECTED" and spent:
            raise ValueError("Un refus ne peut cacher une exécution")
        if state == "OPEN" and (spent or not remaining):
            raise ValueError("OPEN exige un reliquat sans exécution")
        if state == "PARTIAL" and (not spent or not remaining):
            raise ValueError("PARTIAL exige exécution et reliquat")
        if state == "FILLED" and not spent:
            raise ValueError("FILLED exige un débit positif")
        with self.transaction() as db:
            row = self.order(authorization_id, db)
            if row["state"] == "RESERVED":
                raise StateConflict("Ordre non envoyé")
            if at < row["observed_at"] or at < row["created_at"] or spent < row["spent"]:
                raise StateConflict("Observation ancienne / débit cumulatif décroissant")
            if row["external_id"] and external != row["external_id"]:
                raise StateConflict("Identifiant externe différent")
            if row["state"] in TERMINAL:
                if (state, spent, remaining, external) == (row["state"], row["spent"], row["remaining"], row["external_id"]):
                    return
                raise StateConflict("Un état terminal ne peut être réécrit")
            if spent + remaining > units(row["authorization"]["max_total_debit"]):
                raise StateConflict("Débit hors autorisation ; maintenir UNKNOWN et vérifier le compte")
            db.execute("""UPDATE d_orders SET state=?,spent=?,remaining=?,external_id=?,observed_at=?
                          WHERE id=?""", (state, spent, remaining, external, at, authorization_id))
            self.event("EXECUTION_CONFIRMED", authorization_id, r, db)

    def settle(self, authorization_id: str, *, payout: float, settled_at: float,
               evidence_ref: str) -> None:
        """B confirme le versement réel (zéro si perte). Aucun P&L supposé."""
        finite(payout, "payout", minimum=0)
        paid = available_units(payout)
        finite(settled_at, "settled_at", minimum=0)
        if not evidence_ref or settled_at > self.clock():
            raise ValueError("Confirmation de règlement invalide")
        with self.transaction() as db:
            row = self.order(authorization_id, db)
            if row["state"] not in TERMINAL or row["remaining"] or not row["spent"]:
                raise StateConflict("Ordre non entièrement confirmé")
            if settled_at < row["observed_at"]:
                raise StateConflict("Règlement antérieur à l'exécution")
            if row["settled_at"] is not None:
                if row["payout"] == paid and row["settled_at"] == settled_at:
                    return
                raise StateConflict("Règlement déjà enregistré")
            db.execute("UPDATE d_orders SET settled_at=?,payout=?,settlement_ref=? WHERE id=?",
                       (settled_at, paid, evidence_ref, authorization_id))
            self.event("SETTLEMENT", authorization_id, {"payout": payout, "evidence_ref": evidence_ref}, db)

    def resolve_market(self, market_id: str, outcome: str, *, resolved_at: float,
                       evidence_ref: str) -> None:
        """Résolution pour l'évaluation, distincte du paiement des positions."""
        if outcome not in ("YES", "NO", "CANCEL", "MKT") or not evidence_ref:
            raise ValueError("Résolution / référence invalide")
        finite(resolved_at, "resolved_at", minimum=0)
        if resolved_at > self.clock():
            raise ValueError("Résolution future")
        with self.transaction() as db:
            old = db.execute("SELECT * FROM d_resolutions WHERE market_id=?", (market_id,)).fetchone()
            if old and (old["outcome"] != outcome or old["resolved_at"] != resolved_at):
                raise StateConflict("Résolution différente : correction opérateur nécessaire")
            db.execute("INSERT OR IGNORE INTO d_resolutions VALUES(?,?,?,?)",
                       (market_id, outcome, resolved_at, evidence_ref))
            if not old:
                self.log_event("B", "MARKET_RESOLVED", status="OBSERVED", market_id=market_id,
                               occurred_at=resolved_at, payload={"outcome": outcome, "evidence_ref": evidence_ref}, db=db)

    def dashboard(self, limit: int = 100) -> dict:
        """Données JSON pour C : analyses, décisions et états réellement observés."""
        if not 1 <= limit <= 10_000:
            raise ValueError("limit doit être entre 1 et 10000")
        with self.connection() as db:
            analyses = [json.loads(r[0]) for r in db.execute(
                "SELECT payload FROM d_analyses ORDER BY reference_at DESC LIMIT ?", (limit,))]
            decisions = [json.loads(r[0]) for r in db.execute(
                "SELECT payload FROM d_decisions ORDER BY at DESC LIMIT ?", (limit,))]
            events = [dict(r) for r in db.execute("SELECT * FROM d_events ORDER BY seq DESC LIMIT ?", (limit,))]
            for event in events:
                event["payload"] = json.loads(event["payload"])
            orders = self.orders(db)[-limit:]
            paused = self.meta("paused", db)
            batches = [json.loads(r[0]) for r in db.execute(
                "SELECT payload FROM d_batches ORDER BY at DESC LIMIT ?", (limit,))]
            latest = db.execute("SELECT coalesce(max(seq),0) FROM d_logbook").fetchone()[0]
        return {"analyses": analyses, "decisions": decisions, "orders": orders,
                "events": events, "logbook": self.logbook(after_seq=max(0, latest-limit), limit=limit),
                "batches": batches, "paused": paused, "metrics": self.metrics()}

    def metrics(self) -> dict:
        """Brier comparables : première prévision par marché, avant résolution.

        YES/NO seulement ; CANCEL/MKT sont exclus. Critic et Researcher sont
        comparés sur exactement les mêmes événements. Aucun score sans données.
        Le P&L réalisé utilise uniquement les débits et paiements confirmés.
        """
        with self.connection() as db:
            rows = db.execute("""SELECT a.payload,r.outcome,r.resolved_at
                FROM d_analyses a JOIN d_resolutions r ON a.market_id=r.market_id
                WHERE r.outcome IN ('YES','NO') AND a.reference_at<r.resolved_at
                ORDER BY a.reference_at,a.id""").fetchall()
            orders = self.orders(db)
        selected = {}
        for row in rows:
            a = json.loads(row["payload"])
            if a["completed_at"] >= row["resolved_at"]:
                continue
            selected.setdefault(a["market_id"], (a, int(row["outcome"] == "YES")))
        scores = {}
        for label, field in (("market", "market_probability"), ("researcher", "initial_probability"), ("final", "final_probability")):
            errors = [(a[field] - y) ** 2 for a, y in selected.values()]
            scores[label] = sum(errors) / len(errors) if errors else None
        pairs = [(a, y) for a, y in selected.values() if a["critic_completed"]]
        scores["critic_vs_researcher"] = {
            "n": len(pairs),
            "researcher": sum((a["initial_probability"] - y)**2 for a, y in pairs) / len(pairs) if pairs else None,
            "critic": sum((a["final_probability"] - y)**2 for a, y in pairs) / len(pairs) if pairs else None,
        }
        settled = [r for r in orders if r["settled_at"] is not None]
        return {"n_resolved_markets": len(selected), "brier": scores,
                "n_settled_positions": len(settled),
                "realized_pnl_mana": mana(sum(r["payout"] - r["spent"] for r in settled)),
                "open_committed_mana": mana(sum((r["spent"] if r["settled_at"] is None else 0) + r["remaining"] for r in orders))}
