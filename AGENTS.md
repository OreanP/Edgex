# EdgeX — règles pour Codex (à lire avant toute tâche)

EdgeX : agent de recherche probabiliste avec **paper trading uniquement**
(aucune exécution réelle, aucun wallet, aucune clé de trading).

## Commandes
- Installer : `python -m venv .venv`, activer l'environnement, puis `pip install -r requirements.txt`
- Tests (sans réseau, sans crédits) : `python -m pytest`
- Interface : `streamlit run app.py`

## Qui possède quoi
| Partie | Fichiers |
|---|---|
| A — Agent / OpenAI | `edgex/agent.py`, `edgex/prompts/` |
| B — Marchés + broker | `edgex/polymarket.py`, `edgex/broker.py` |
| C — Interface | `app.py`, `ui/` |
| D — Intégration | `edgex/schemas.py`, `edgex/interfaces.py`, `edgex/risk.py`, `edgex/cache.py`, `edgex/replay.py`, `edgex/fakes.py`, `tests/`, `fixtures/`, `README.md`, `requirements.txt`, `AGENTS.md` |

Chaque prompt commence par la partie concernée (ex. « Partie B : … »).
Ne modifie QUE les fichiers de cette partie.

## Règles
1. `edgex/schemas.py` et `edgex/interfaces.py` sont le contrat de l'équipe : ne les modifie jamais.
   Si un changement semble nécessaire, arrête-toi et décris précisément le changement proposé.
2. Toute fonction publique reçoit et renvoie des objets de `schemas.py`, avec exactement les signatures de `interfaces.py`.
3. Le broker appelle `risk.check()` avant chaque ordre : aucun trade sans `RiskDecision` approuvée.
4. L'agent appelle `on_step(AgentStep(...))` à chaque action : c'est la trace affichée par l'interface.
5. Les calculs (totaux, edge, montants) sont faits en Python, jamais par le LLM.
6. `python -m pytest` doit passer sans réseau : utilise `fixtures/` et `edgex/fakes.py`, aucun appel API dans les tests.
7. N'ajoute aucune dépendance sans le signaler : D met à jour `requirements.txt`.
8. Aucune clé API dans le code : tout passe par `.env` (voir `.env.example`).
9. Si la consigne est ambiguë, pose la question au lieu de supposer.
