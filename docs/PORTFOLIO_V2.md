# EdgeX — extension portefeuille du hackathon

## Démarrage (Windows, depuis la racine du dépôt)

La V1 (`app.py`, A/B/D existants) est **inchangée**. La nouvelle vue ne remplace
pas le template du partenaire et ne contient **aucune méthode de mise réelle**.

```powershell
uv pip install -r requirements-portfolio.txt --python .\.venv\Scripts\python.exe
.\.venv\Scripts\python.exe -m portfolio.demo --out portfolio-demo.json
.\.venv\Scripts\python.exe -m streamlit run portfolio_app.py
```

Démarrer avec **Démonstration hors ligne → Construire le panier** : aucun compte,
aucune connexion réseau et aucun crédit nécessaire. Les données de ce mode sont
FABRIQUÉES pour la démo technique, pas des résultats de prévision.

Pour le mode connecté : configurer les clés dans `.env`, puis choisir
**Manifold + OpenAI** et confirmer les appels payants. Les requêtes Manifold
POST sont exclusivement des `dryRun: true`. `ALLOW_LIVE_TRADING` de la V1 n'a
aucun effet sur cette extension. Ne pas exposer la vue connectée publiquement
sans authentification : chaque clic confirmé peut consommer des crédits.

```dotenv
OPENAI_API_KEY=
MANIFOLD_API_KEY=
# Utilise PORTFOLIO_MODEL, puis OPENAI_MODEL, puis gpt-6-luna.
PORTFOLIO_MODEL=gpt-6-luna
# Facultatif : tarifs pour tout autre modèle, en USD / million de tokens.
# PORTFOLIO_INPUT_USD_PER_M=
# PORTFOLIO_OUTPUT_USD_PER_M=
```

Ne jamais committer `.env`. La base de budget/cache/rapports se trouve dans
`portfolio/data/state.sqlite3` (dossier ignoré par Git). Ne pas la supprimer
pour contourner un quota. Tous les processus d'une même installation doivent
partager ce fichier. Plusieurs copies du dépôt ont des budgets distincts.

## Les six améliorations

1. **Binaire conservé + multi-réponses** : `BINARY/cpmm-1` et
   `MULTIPLE_CHOICE/cpmm-multi-1`, avec distinction `shouldAnswersSumToOne`.
   Les réponses natives `prob` et `probability` sont acceptées. IDs exacts,
   aucune normalisation ou troncature silencieuse. V2 limitée à 8 réponses et
   refus des marchés partiellement résolus ; `FREE_RESPONSE`, polls et marchés
   numériques restent hors périmètre.
2. **Collecte multi-domaines** : IA, science, spatial, crypto ; présélection
   round-robin entre domaines ET types, déduplication, détails du contrat,
   timestamps en secondes, token MANA explicite, critères non vides.
   Le filtre politique par mots/tags est heuristique : relire les dossiers.
3. **Panier collectif** : les prévisions sont produites avant l'allocation.
   Une même question ne peut recevoir qu'un côté/une réponse/un palier dans
   ce MVP, pour ne pas additionner des dry-runs incompatibles du même AMM.
4. **Rendement espéré sous contraintes de risque** : recherche de type sac à
   dos multi-choix, dans l'esprit du `allocate_batch` de D. Plafonds totaux,
   par marché, domaine et famille définie par l'opérateur. Cash possible.
   Au maximum deux jambes présélectionnées par question et trois tailles.
   L'optimalité ne vaut que pour les options fournies. Le compteur de nœuds
   borne le calcul et affiche honnêtement si la recherche a été interrompue.
   `Holdings` permet de fournir des expositions existantes pour les tests/
   intégrations ; l'UI utilise un **capital de simulation**, pas le compte réel.
5. **Recherche encadrée** : Researcher sans prix Manifold dans le prompt,
   Critic contradictoire, preuve conservée seulement si son URL apparaît dans
   les traces de recherche/citations. Cela ne prouve pas la vérité de la source.
   Plafonds persistants appels/jour/cycle, outils, tokens de sortie, délai et
   cache 5 minutes. Pas de retries automatiques invisibles. Un Critic échoué
   reste signalé et réduit les tailles candidates. Pas d'entraînement ML/RL.
6. **Rendu portefeuille** : allocation, écarts par position, distributions
   par réponse, motifs de refus, temps, coût IA connu/inconnu et export JSON.
   Le P&L réalisé reste `null / Non mesuré` : un dry-run n'est pas un gain.
   Mana et USD restent séparés, sans taux de change artificiel.

## Réutiliser avec le futur frontend

```python
from portfolio.manifold import Manifold
from portfolio.research import Store, Researcher
from portfolio.engine import run_cycle
from portfolio.models import Policy

source = Manifold()  # seule preview() utilise MANIFOLD_API_KEY
store = Store()
markets, refus = source.discover(["AI", "Space", "Science"], limit=6)
report = run_cycle(markets, Researcher(store), source, store=store,
                   policy=Policy(max_markets=6))
# report est sérialisable JSON. Ne pas transformer ses options en autorisations.
```

`Market.answers` contient une entrée YES pour un binaire (NO = complément),
et toutes les réponses pour un multi. `Analysis.initial/final` sont indexées
par les IDs. Dans une somme-à-un, les probabilités doivent totaliser 1 ; dans
un marché indépendant, plusieurs réponses peuvent être vraies simultanément.
Les questions et pages externes sont données au modèle comme données non fiables,
jamais comme instructions d'exécution ; aucune clé ou fonction de mise ne lui
est exposée. Les grandes descriptions sont refusées plutôt que tronquées.

## Hypothèses / limites à ne pas cacher au jury

- Il s'agit d'un **plan / preview**, pas d'un nouveau broker live certifié.
  L'ancien GateService et les interfaces de A/D restent intacts pour permettre
  une intégration ultérieure contrôlée. Ne pas brancher ce plan directement
  à `place_bet(dry_run=False)`.
- Coût de position = montant simulé + frais rapportés + réserve configurable
  (1 Mana par défaut). Cette estimation prudente peut compter des frais déjà
  inclus ; **elle n'est ni le débit exact ni une borne API garantie**. On ne
  met jamais un drapeau `debit_bound_guaranteed=True` sur cette seule base.
- Les plafonds de domaine/famille contrôlent la concentration, pas des
  corrélations estimées. Pas de CVaR, de vente automatique ou de rééquilibrage.
- Les objectifs de temps arrêtent les nouveaux appels ; un appel réseau en
  cours reste soumis à son timeout. Les recherches communes entre questions
  ne sont pas encore mutualisées ; le cache évite les réanalyses du même
  contrat (hash des critères + version du prompt + modèle).
- Le coût USD affiché est une estimation à partir de `usage`, des tarifs
  configurés et des appels web observés, pas une facture. Tarif initial connu
  pour `gpt-6-luna` : 0,10/0,50 USD par million de tokens entrée/sortie, plus
  0,01 USD par appel web, vérifiés le 27/09/2026. Les tokens d'entrée en cache
  sont comptés au tarif normal (prudent). Autres modèles : coût inconnu sans
  tarifs explicites. Les crédits offerts ne sont pas assimilés à un coût nul.
- Les réservations USD peuvent être dépassées par une facture fournisseur
  inattendue. Seuls les compteurs d'appels/outils/tokens configurés sont des
  plafonds techniques locaux. Utiliser également les limites du fournisseur.
- Sources API vérifiées : https://docs.manifold.markets/api ;
  https://github.com/manifoldmarkets/manifold/blob/main/common/src/answer.ts ;
  https://github.com/manifoldmarkets/manifold/blob/main/backend/api/src/place-bet.ts ;
  https://developers.openai.com/api/docs/models ;
  https://developers.openai.com/api/docs/pricing .

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_portfolio_v2.py tests/test_portfolio_ui.py
.\.venv\Scripts\python.exe -m compileall -q portfolio portfolio_app.py
```

Tests réseau interdit : contrats binaires/multi, données absentes, politique,
IDs, dry-run forcé, quotas persistants, erreurs facturables, cache, trace des
sources, optimisation comparée par force brute, risque commun, expositions,
non-exécution, scénarios de bout en bout et AppTest Streamlit.

Aucune dépendance du `requirements.txt` original n'est changée ; les exigences
minimales V2 sont déclarées séparément. La suite historique n'est pas remplacée.
