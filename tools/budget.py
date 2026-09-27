"""Budget OpenAI persistant, imposé AVANT chaque analyse et chaque appel.

A doit utiliser BudgetSession.parse au lieu de client.responses.parse. Cette
façade désactive les retries du SDK, borne max_output_tokens/max_tool_calls et
n'expose que web_search. Les plafonds réservés sont consommés même si l'appel
échoue : une erreur réseau ne prouve pas l'absence de facturation.

Le budget porte sur analyses, appels, appels web intégrés et plafonds de tokens
de sortie. Ce n'est PAS un plafond monétaire exact : les tarifs, tokens d'entrée
et contexte de recherche ne sont pas assimilés à zéro. Utiliser également les
contrôles du projet fournisseur. Journées UTC ; aucune remise à zéro au reboot.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Any
from uuid import uuid4

from tools.risk import normalize_url
from tools.tracker import Tracker, finite, fingerprint, plain, encode, utc_day


class BudgetExceeded(RuntimeError):
    """L'appel n'a pas été lancé ; code exploitable par le dashboard."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class BudgetPolicy:
    analyses_per_day: int = 10
    analyses_per_cycle: int = 3
    calls_per_day: int = 40
    calls_per_analysis: int = 4
    web_calls_per_day: int = 30
    web_calls_per_analysis: int = 4
    output_tokens_per_call: int = 4096
    output_tokens_per_day: int = 100_000
    max_input_characters: int = 40_000
    market_cooldown: float = 600
    max_analysis_seconds: float = 1800
    request_timeout: float = 60
    allowed_models: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for key in ("analyses_per_day", "analyses_per_cycle", "calls_per_day", "calls_per_analysis",
                    "web_calls_per_day", "web_calls_per_analysis", "output_tokens_per_call",
                    "output_tokens_per_day", "max_input_characters"):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{key}: entier positif attendu")
        if self.output_tokens_per_call < 16:
            raise ValueError("output_tokens_per_call doit être au moins 16")
        for key in ("market_cooldown", "max_analysis_seconds", "request_timeout"):
            finite(getattr(self, key), key, minimum=0)
        if self.max_analysis_seconds <= 0 or self.request_timeout <= 0:
            raise ValueError("Les délais d'appel et d'analyse doivent être positifs")


class BudgetEngine:
    """Partage le Tracker de l'application ; aucune base parallèle à synchroniser."""

    def __init__(self, tracker: Tracker, policy: BudgetPolicy = BudgetPolicy()):
        self.tracker, self.policy = tracker, policy

    def begin(self, *, analysis_id: str, market_id: str, cycle_id: str) -> "BudgetSession":
        for value in (analysis_id, market_id, cycle_id):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Identifiants d'analyse, marché et cycle obligatoires")
        now, p = self.tracker.clock(), self.policy
        day = utc_day(now)
        with self.tracker.transaction() as db:
            self.tracker.bind("budget_policy_hash", fingerprint(p), db)
            if self.tracker.meta("paused", db):
                raise BudgetExceeded("SYSTEM_PAUSED")
            if db.execute("SELECT 1 FROM d_budget_sessions WHERE id=?", (analysis_id,)).fetchone():
                raise BudgetExceeded("ANALYSIS_ALREADY_STARTED")
            daily = db.execute("SELECT count(*) FROM d_budget_sessions WHERE day=?", (day,)).fetchone()[0]
            cycle = db.execute("SELECT count(*) FROM d_budget_sessions WHERE cycle_id=?", (cycle_id,)).fetchone()[0]
            if daily >= p.analyses_per_day:
                raise BudgetExceeded("DAILY_ANALYSES")
            if cycle >= p.analyses_per_cycle:
                raise BudgetExceeded("CYCLE_ANALYSES")
            last = db.execute("SELECT * FROM d_budget_sessions WHERE market_id=? ORDER BY started_at DESC LIMIT 1", (market_id,)).fetchone()
            if last:
                if now < last["started_at"]:
                    raise BudgetExceeded("CLOCK_ROLLBACK")
                if last["state"] == "RUNNING" and now - last["started_at"] <= p.max_analysis_seconds:
                    raise BudgetExceeded("MARKET_ANALYSIS_RUNNING")
                if now - last["started_at"] < p.market_cooldown:
                    raise BudgetExceeded("MARKET_COOLDOWN")
            db.execute("INSERT INTO d_budget_sessions VALUES(?,?,?,?,?,'RUNNING',?)",
                       (analysis_id, market_id, cycle_id, now, day, fingerprint(p)))
            self.tracker.event("ANALYSIS_BUDGET_RESERVED", analysis_id, {"market_id": market_id, "cycle_id": cycle_id}, db)
        return BudgetSession(self, analysis_id)

    def finish(self, analysis_id: str, *, success: bool) -> None:
        with self.tracker.transaction() as db:
            row = db.execute("SELECT state FROM d_budget_sessions WHERE id=?", (analysis_id,)).fetchone()
            if not row:
                raise KeyError(analysis_id)
            if row["state"] != "RUNNING":
                return
            incomplete = db.execute("SELECT 1 FROM d_budget_calls WHERE session_id=? AND state!='DONE' LIMIT 1", (analysis_id,)).fetchone()
            state = "DONE" if success and not incomplete else "FAILED"
            db.execute("UPDATE d_budget_sessions SET state=? WHERE id=?", (state, analysis_id))
            self.tracker.event("ANALYSIS_FINISHED", analysis_id, {"state": state}, db)

    def trace(self, analysis_id: str) -> dict:
        with self.tracker.connection() as db:
            session = db.execute("SELECT * FROM d_budget_sessions WHERE id=?", (analysis_id,)).fetchone()
            rows = db.execute("SELECT * FROM d_budget_calls WHERE session_id=? ORDER BY started_at,id", (analysis_id,)).fetchall()
        if not session:
            raise KeyError(analysis_id)
        successful = [r for r in rows if r["state"] == "DONE"]
        urls = {u for r in successful for u in json.loads(r["metadata"]).get("urls", [])}
        return {"session_id": analysis_id, "market_id": session["market_id"],
                "state": session["state"], "started_at": session["started_at"],
                "urls": tuple(sorted(urls)),
                "critic_completed": any(r["phase"] == "critic" for r in successful),
                "researcher_completed": any(r["phase"] == "researcher" for r in successful),
                "calls": len(rows)}

    def usage(self, day: str | None = None) -> dict:
        day = day or utc_day(self.tracker.clock())
        with self.tracker.connection() as db:
            a = db.execute("SELECT count(*) FROM d_budget_sessions WHERE day=?", (day,)).fetchone()[0]
            row = db.execute("SELECT count(*),coalesce(sum(web_reserved),0),coalesce(sum(output_reserved),0) FROM d_budget_calls WHERE day=?", (day,)).fetchone()
        return {"day_utc": day, "analyses": a, "calls": row[0], "web_calls_reserved": row[1], "output_tokens_reserved": row[2]}


class BudgetSession:
    """Capacité limitée à une analyse, passée à A comme argument nommé budget."""

    def __init__(self, engine: BudgetEngine, analysis_id: str):
        self.engine, self.analysis_id = engine, analysis_id

    def __enter__(self) -> "BudgetSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.engine.finish(self.analysis_id, success=exc_type is None)
        return False

    def parse(self, client: Any, *, phase: str, web_calls: int = 1, **kwargs) -> Any:
        """client OpenAI injecté ; retourne la même réponse que responses.parse.

        Exemple : budget.parse(client, phase='researcher', web_calls=2,
          model=MODEL, input=messages, text_format=Forecast).
        A doit vérifier output_parsed, gérer ses erreurs, puis renvoyer
        AgentAnalysis. Toute phase Critic fait un deuxième appel par cette voie.
        """
        p, tracker = self.engine.policy, self.engine.tracker
        if phase not in ("researcher", "critic"):
            raise ValueError("Phase attendue : researcher ou critic")
        if isinstance(web_calls, bool) or not isinstance(web_calls, int) or web_calls < 0:
            raise ValueError("web_calls doit être un entier positif ou nul")
        allowed = {"model", "input", "instructions", "text_format", "temperature", "reasoning", "tools", "max_output_tokens"}
        if set(kwargs) - allowed:
            raise ValueError(f"Paramètres non autorisés : {sorted(set(kwargs)-allowed)}")
        if not isinstance(kwargs.get("model"), str) or not kwargs["model"]:
            raise ValueError("model obligatoire")
        if p.allowed_models and kwargs["model"] not in p.allowed_models:
            raise BudgetExceeded("MODEL_NOT_ALLOWED")
        requested = kwargs.pop("max_output_tokens", p.output_tokens_per_call)
        if isinstance(requested, bool) or not isinstance(requested, int) or requested < 16:
            raise ValueError("max_output_tokens doit être un entier >= 16")
        output_cap = min(requested, p.output_tokens_per_call)
        tools = kwargs.pop("tools", [{"type": "web_search"}] if web_calls else [])
        if not isinstance(tools, list) or any(not isinstance(t, dict) or t.get("type") != "web_search" for t in tools):
            raise ValueError("Seul web_search est exposé à A par cette façade")
        if (not web_calls and tools) or (web_calls and len(tools) != 1):
            raise ValueError("Budget web et outil web incohérents")
        schema = kwargs.get("text_format")
        schema_text = schema.model_json_schema() if hasattr(schema, "model_json_schema") else {}
        payload_size = len(encode({"input": kwargs.get("input"), "instructions": kwargs.get("instructions"), "schema": schema_text}))
        if payload_size > p.max_input_characters:
            raise BudgetExceeded("INPUT_TOO_LARGE")
        if not hasattr(client, "with_options"):
            raise TypeError("Client doit exposer with_options(max_retries=0, timeout=...)")

        now, call_id = tracker.clock(), uuid4().hex
        day = utc_day(now)
        with tracker.transaction() as db:
            session = db.execute("SELECT * FROM d_budget_sessions WHERE id=?", (self.analysis_id,)).fetchone()
            if not session or session["state"] != "RUNNING":
                raise BudgetExceeded("SESSION_NOT_RUNNING")
            if tracker.meta("paused", db):
                raise BudgetExceeded("SYSTEM_PAUSED")
            if session["policy_hash"] != fingerprint(p):
                raise BudgetExceeded("POLICY_CHANGED")
            if not 0 <= now - session["started_at"] <= p.max_analysis_seconds:
                raise BudgetExceeded("SESSION_EXPIRED")
            if db.execute("SELECT 1 FROM d_budget_calls WHERE session_id=? AND state='SENDING'", (self.analysis_id,)).fetchone():
                raise BudgetExceeded("CALL_ALREADY_IN_FLIGHT")
            per = db.execute("SELECT count(*),coalesce(sum(web_reserved),0) FROM d_budget_calls WHERE session_id=?", (self.analysis_id,)).fetchone()
            total = db.execute("SELECT count(*),coalesce(sum(web_reserved),0),coalesce(sum(output_reserved),0) FROM d_budget_calls WHERE day=?", (day,)).fetchone()
            checks = ((per[0] + 1 <= p.calls_per_analysis, "ANALYSIS_CALLS"),
                      (per[1] + web_calls <= p.web_calls_per_analysis, "ANALYSIS_WEB_CALLS"),
                      (total[0] + 1 <= p.calls_per_day, "DAILY_CALLS"),
                      (total[1] + web_calls <= p.web_calls_per_day, "DAILY_WEB_CALLS"),
                      (total[2] + output_cap <= p.output_tokens_per_day, "DAILY_OUTPUT_TOKENS"))
            for ok, code in checks:
                if not ok:
                    raise BudgetExceeded(code)
            db.execute("INSERT INTO d_budget_calls VALUES(?,?,?,?,?,?,?,'SENDING','{}')",
                       (call_id, self.analysis_id, day, phase, now, web_calls, output_cap))
            tracker.log_event("A", "OPENAI_CALL", status="STARTED", operation_id=call_id,
                analysis_id=self.analysis_id, market_id=session["market_id"], batch_id=session["cycle_id"],
                payload={"phase": phase, "model": kwargs["model"], "web_reserved": web_calls,
                         "output_reserved": output_cap}, db=db)

        kwargs.update(max_output_tokens=output_cap, tools=tools)
        if web_calls:
            kwargs.update(max_tool_calls=web_calls, include=["web_search_call.action.sources"])
        monotonic_start = time.perf_counter()
        try:
            # Pas de retries invisibles : chaque nouvelle tentative doit réserver.
            response = client.with_options(max_retries=0, timeout=p.request_timeout).responses.parse(**kwargs)
            data = plain(response)
            if not isinstance(data, dict):
                raise TypeError("Réponse OpenAI non sérialisable")
            if data.get("status") != "completed" or getattr(response, "output_parsed", None) is None:
                raise RuntimeError("Réponse incomplète, refusée ou non parsée")
            searches = [item for item in data.get("output", []) if item.get("type") == "web_search_call"]
            if len(searches) > web_calls:
                tracker.pause("WEB_TOOL_LIMIT_VIOLATION")
                raise RuntimeError("Limite web non respectée par le fournisseur")
            urls = set()
            for item in searches:
                if item.get("status") != "completed":
                    continue
                action = item.get("action") or {}
                for source in action.get("sources", []):
                    url = normalize_url(source.get("url", ""))
                    if url:
                        urls.add(url)
                # Un open_page réussi est également une trace d'outil, pas un label LLM.
                if action.get("type") == "open_page":
                    url = normalize_url(action.get("url", ""))
                    if url:
                        urls.add(url)
            metadata = {"response_id": data.get("id"), "urls": sorted(urls),
                        "web_calls_observed": len(searches), "usage": data.get("usage"),
                        "model": kwargs["model"], "started_at": now,
                        "completed_at": tracker.clock(),
                        "duration_ms": (time.perf_counter()-monotonic_start)*1000}
            with tracker.transaction() as db:
                db.execute("UPDATE d_budget_calls SET state='DONE',metadata=? WHERE id=?", (encode(metadata), call_id))
                tracker.event("OPENAI_CALL_COMPLETED", self.analysis_id, {"call_id": call_id, "phase": phase, **metadata}, db)
                tracker.log_event("A", "OPENAI_CALL", status="SUCCEEDED", operation_id=call_id,
                    analysis_id=self.analysis_id, market_id=session["market_id"], batch_id=session["cycle_id"],
                    payload={"phase": phase, **metadata}, db=db)
                for index, item in enumerate(searches):
                    # Le SDK livre ces traces avec la réponse. On ne connaît pas
                    # l'heure exacte de chaque recherche côté fournisseur.
                    action = item.get("action") or {}
                    tracker.log_event("A", "WEB_TOOL_OBSERVED", analysis_id=self.analysis_id,
                        market_id=session["market_id"], batch_id=session["cycle_id"], parent_id=call_id,
                        payload={"phase": phase, "tool_id": item.get("id"), "response_index": index,
                                 "tool_status": item.get("status"), "action": action,
                                 "provider_timestamp_known": False}, db=db)
            return response
        except Exception as exc:
            with tracker.transaction() as db:
                # Les réservations ne sont pas remboursées : le coût est inconnu.
                db.execute("UPDATE d_budget_calls SET state='FAILED',metadata=? WHERE id=?",
                           (encode({"error_type": type(exc).__name__}), call_id))
                tracker.event("OPENAI_CALL_FAILED", self.analysis_id, {"call_id": call_id, "error_type": type(exc).__name__}, db)
                tracker.log_event("A", "OPENAI_CALL", status="FAILED", operation_id=call_id,
                    analysis_id=self.analysis_id, market_id=session["market_id"], batch_id=session["cycle_id"],
                    payload={"phase": phase, "error_type": type(exc).__name__,
                             "duration_ms": (time.perf_counter()-monotonic_start)*1000}, db=db)
            raise
