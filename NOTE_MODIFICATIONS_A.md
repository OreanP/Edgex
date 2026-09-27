# EdgeX — règles de D, contrat avec A et livraison V2

## 0. Portée de cette livraison

La V2 conserve les contrôles de la V1 et ajoute **un journal global** ainsi qu'une
**allocation collective par lots de 10 analyses au maximum**. Seuls les mêmes
fichiers D sont modifiés : `tools/risk.py`, `tools/budget.py`, `tools/tracker.py`,
`tools/cache.py`, `tools/integration.py`, `tests/test_d.py`, et cette note.
Les fichiers de A/B/C ne sont pas modifiés.

Le point de départ est l'archive `Edgex-agent.zip` fournie, puis `EdgeX_D.zip`.
L'archive initiale de A contenait un seul appel `responses.parse`, des probabilités
initiale/finale identiques et `evidence=[]`. Le Critic et l'adaptateur Manifold
annoncés dans le compte rendu n'y figuraient pas. Cela ne décrit pas nécessairement
la branche plus récente de vos camarades.

**Validation : 170 tests hors ligne passent**, dont les 110 tests initiaux et
60 tests ajoutés. Le faux broker a reçu les nouveaux champs de valorisation.
Le solveur est notamment comparé à une énumération indépendante sur 50 instances.
Aucun appel OpenAI/Manifold réel, aucune mise réelle. L'adaptateur de B reste à brancher.

## 1. Organisation et responsabilités

- **A** recherche, critique, estime et propose `BUY_YES`, `BUY_NO` ou `SKIP`.
- **B** fournit des snapshots datés, prévisualise chaque taille, exécute et confirme.
- **D** encadre le budget, contrôle les propositions, alloue les enveloppes, réserve,
  autorise précisément, historise et mesure. Il ne change jamais la probabilité de A.
- **C** affiche séparément prévision, validation, allocation et exécution observée.

D reste du code déterministe. Pas de Monte Carlo, Kelly, régression, calibration
inventée ou LLM supplémentaire. Le `risk.py` pur ne réserve rien : c'est
`GateService` qui produit et persiste les autorisations.

```text
B : snapshot daté
→ D : cache / budget avant recherche
→ A : Researcher puis Critic via BudgetSession
→ D : enregistrer chaque analyse, y compris SKIP et erreurs de parcours
→ collecter jusqu'à 10 dossiers (ou fermer plus tôt)
→ B : marchés et portefeuille actualisés, prévisualisations de chaque palier
→ D : contraintes individuelles puis optimisation collective
→ D : réserver toutes les autorisations du lot dans une seule transaction
→ B via GateService : revérifier et envoyer chaque ordre séparément
→ D/C : confirmations, journal et métriques
```

## 2. Règles de contrôle conservées

| Domaine | Règle et traitement |
|---|---|
| Validité | IDs concordants, données finies, probabilités YES dans [0,1], type MANA/BINARY/cpmm-1, question/description et fermeture connus. Marché fermé ou résolu : aucun ordre. |
| Proposition | `SKIP` ne produit pas de mise. D ne retourne pas automatiquement un ordre YES en NO. |
| Arrêt opérationnel | Pause opérateur, envoi `SENDING`/`UNKNOWN`, ou perte réalisée quotidienne dépassée : blocage. |
| Doublons | Une autorisation maximum par analyse. Une répétition retourne l'état enregistré, sans renouveler une autorisation expirée. |
| Fraîcheur | Vérifier l'âge de l'observation de référence de A, du marché et du portefeuille. Refuser les horodatages futurs. Un cache ne rajeunit jamais ces dates. |
| Contrat | Empreinte de la question, description, échéance, type, devise et mécanisme. Conditions modifiées : nouvelle analyse. La fermeture des échanges n'est pas forcément la date du critère de résolution. |
| Mouvement de marché | Recalculer l'edge sur le prix actuel. Un mouvement excessif dans un sens **ou l'autre** impose une actualisation de l'analyse. |
| Sens | `edge_directionnel = final - marché` pour YES ; `marché - final` pour NO. Il doit être positif et au moins égal à la marge de politique. Jamais `abs(edge)` seul. |
| Preuves | Aucune preuve structurée ou aucune URL correspondant aux traces réelles des outils : refus. Traçabilité n'est ni vérité ni indépendance des sources. |
| Confiance | `low` bloque ; `medium` plafonne à U. `high` n'impose pas une grande mise. |
| Critic | Désactivé volontairement : U au maximum, ou refus si `require_critic=True`. Un appel Critic échoué n'est pas traité comme une désactivation normale. |
| Révision | Changement de côté par rapport au marché de référence, ou amplitude de révision >= marge actuelle : U au maximum. C'est une heuristique de prudence, pas un intervalle de confiance. |
| Edge exceptionnel | U au maximum : l'ampleur du désaccord n'établit pas sa fiabilité. |
| Traçabilité partielle | 2U au maximum lorsque seule une partie des URLs citées est retrouvée dans les outils. Ne pas compter les URLs comme des votes. |
| Tailles | Paliers 0/U/2U/4U. Chaque contrôle impose un plafond ; prendre le minimum, jamais une somme de « bons points ». Unité fixe, sans hausse après gain ou perte. |
| Portefeuille | Respecter simultanément solde disponible après réservations, réserve de trésorerie, plafonds marché/famille/total, dépenses jour/session et arrêt après pertes du jour. |
| Concentration | Familles définies par l'équipe, pas par le LLM. Les marchés non classés partagent `UNKNOWN`. Pas de corrélations estimées ni de compensation fictive de pertes. |
| Engagement | Débits frais compris des positions encore ouvertes + reliquats/réservations. Fusionner par véritable betId pour ne pas compter deux fois. Si rapprochement incertain, rester conservateur. |
| Obstination | Aucun renforcement ou inversion automatique d'un marché déjà détenu. Pause après perte sur le marché ou sa famille. Un nouvel ID n'efface pas les contraintes. Ventes/fermetures hors périmètre de cette gate d'achat. |
| Exécution | Montant exact avant frais **et** borne maximale du débit total garanti par B, prix limite et deux échéances : autorisation locale et ordre distant. Absence de borne de frais : refus. |
| Prix limite | YES : `p_finale - marge`, arrondi au centième vers le bas. NO : `p_finale + marge`, arrondi vers le haut. Toujours exprimé comme une probabilité de YES. |

Les paramètres sont des **choix de démonstration**, pas des résultats empiriques :
la V1 garde par défaut U=5 Mana, marge 0,08, edge exceptionnel 0,30, mouvement
maximal 0,12, âge d'analyse 900 s, snapshots 30 s, autorisation 15 s et ordre 30 s.
Les plafonds de Mana sont visibles dans `RiskPolicy`. Ils restent configurables
**avant** de fixer la politique du compte dans la base.

## 3. Budget et cache

Tous les appels de A — Researcher, Critic, tentatives supplémentaires — passent
par `BudgetSession.parse`. Réserver le budget avant le réseau. Un timeout ne
rembourse pas les compteurs puisque la consommation réelle peut être inconnue.

Limites persistantes : analyses/jour et cycle, appels/jour et analyse, appels
aux outils web, tokens de sortie, longueur d'entrée et délais d'exécution. Les
retries automatiques du SDK sont désactivés. Une même analyse ne lance pas deux
appels en vol simultanément. Un budget dépassé ne déclenche aucun fallback direct.

Le nombre d'appels d'outils ne représente pas le nombre exact de pages lues.
Le budget ne prétend pas être un plafond exact en euros : conserver les usages
fournisseur, modèles autorisés et contrôles du compte fournisseur.

Cache : résultats d'analyse/recherche seulement ; clé comprenant version de
l'agent et empreinte du contrat. Garder l'ID, les dates et l'expiration originaux.
Relire le marché avant allocation. Ne pas mettre en cache une permission de mise,
un solde supposé actuel ou le résultat supposé d'un timeout.

## 4. Nouveau journal global (« log book »)

`Tracker` expose à tout le projet :

```python
tracker.log_event("C", "DASHBOARD_RENDERED", batch_id=batch_id,
                  payload={"displayed_markets": 10})

with tracker.operation("B", "CUSTOM_FETCH", market_id=market_id) as log:
    result = do_fetch()
    log["result_count"] = len(result)

rows = tracker.logbook(batch_id=batch_id, after_seq=0, limit=1000)
tracker.export_logbook("logbook.jsonl", batch_id=batch_id)
```

Chaque événement possède un ID et une séquence d'écriture, un composant A/B/C/D,
une opération, un statut, un `run_id`, et si disponibles `batch_id`, `market_id`,
`analysis_id`, `authorization_id`, `operation_id` et `parent_id`.

Deux heures différentes :
- `occurred_at` : heure de l'événement **connue**, sinon observation locale ;
- `recorded_at` : heure de son inscription en base.

Début/fin/erreur sont tracés ; les durées utilisent une horloge monotone. Les
snapshots gardent `fetched_at`, l'analyse garde `reference_at`/`completed_at`, et
son démarrage est dans la session de budget/le journal. Les appels Researcher et
Critic enregistrent aussi modèle, début/fin, identifiant fournisseur, URLs et usage.

**Ne pas confondre heure de fetch, date de publication d'une source, heure de
référence d'une prévision et heure de retour API.** La façade ne connaît pas
forcément l'heure exacte de chaque recherche interne OpenAI : elle consigne
`WEB_TOOL_OBSERVED` à réception, avec `provider_timestamp_known=False`.

Le chemin fourni instrumente automatiquement les raccordements A/B/D, cache,
prévisualisations, allocations et confirmations. **C et les opérations B/A hors
Pipeline doivent appeler cette API commune** : D ne peut pas observer leur code
interne par magie. Après un crash, un début sans fin reste visible, sans faux succès.

Dates internes : secondes Unix UTC ; affichage UTC ISO 8601 ; jours de compteurs
UTC. B convertit les dates Manifold exprimées en millisecondes. Le journal est
append-only via l'API applicative, pas une preuve cryptographique d'intégrité.
Pas de clés, prompts ou raisonnement interne dans les logs. Un filtre masque des
secrets usuels, sans garantir de reconnaître tous les secrets possibles.

## 5. Nouvelle allocation collective : sac à dos multi-choix

### Collecte et admissibilité

La V2 de `Pipeline.run_cycle` collecte d'abord les analyses, puis appelle une fois
`GateService.review_batch(analyses, batch_id=...)`. A peut continuer à travailler
marché par marché : il n'a pas à produire un seul énorme message LLM de dix marchés.

Lot maximal : 10 marchés distincts, un seul dossier par marché. Le budget, les
erreurs ou une fenêtre de collecte (300 s par défaut) peuvent donner un lot plus
petit. La fenêtre empêche de **commencer** une nouvelle analyse trop tard ; elle
ne coupe pas un appel synchrone en cours, qui garde son propre timeout. Pas de
travail asynchrone ajouté ni d'attente automatique jusqu'à avoir dix dossiers.
Les marchés au-delà des limites sont signalés comme non commencés.

Les dossiers anciens gardent leurs propres dates ; l'heure de clôture du lot ne
les remet pas à neuf. Tous les hard stops et plafonds individuels s'appliquent
**avant** l'optimisation. Les échecs d'un dossier n'obligent pas à parier sur les autres.

### Valeur de chaque palier

B doit prévisualiser **chaque** palier encore admissible, car coûts, frais et
quantités peuvent varier avec la taille. Les champs supplémentaires de `Quote` :

```python
estimated_debit: float | None         # coût total estimé, frais compris
payout_if_correct: float | None       # versement BRUT si le côté acheté gagne
payout_if_incorrect: float | None     # versement BRUT dans l'autre issue binaire
full_fill_estimated: bool             # taille entière estimée exécutable
reference_probability: float | None  # probabilité YES lors de cette estimation
```

Ils s'ajoutent à `amount`, `total_debit_upper_bound`, `quoted_at`, `limit_probability`,
`order_expires_at`, `executable`, `debit_bound_guaranteed` et aux IDs/sens.

Pour chaque option k du marché i :

```text
p_côté = p_finale pour YES ; 1 - p_finale pour NO
G[i,k] = p_côté * payout_if_correct
       + (1 - p_côté) * payout_if_incorrect
       - estimated_debit
```

Le capital rendu est déjà inclus dans le payout brut. Ne pas utiliser
`edge * mise`, ni une valorisation sans frais. Ne pas extrapoler un fill partiel
à une exécution entière. Données de valorisation absentes, non fraîches ou
incohérentes : option exclue ; toutes absentes : refus explicite.

### Choix collectif

Choisir pour chaque marché **0 ou un seul** des paliers admissibles, afin de
maximiser `sum(G[i,k])`, sous toutes les enveloppes partagées de Mana. Le poids du
sac à dos est le **plafond de débit réservé**, pas un coût optimiste.

Le solveur effectue une recherche exacte avec élagage : au plus 4^10 combinaisons
pour les trois paliers + zéro. Pas de solveur externe. À valeur égale : réserver
moins de capital, puis départager par IDs, pas par ordre d'arrivée. Une option de
gain nul/négatif n'est pas sélectionnée. Laisser du cash inutilisé est normal.

Exemple abstrait : capacité 10, option X coûte 10 pour gain espéré 5 ; Y et Z
coûtent chacun 5 pour gain espéré 4. Le lot choisit Y+Z, gain espéré 8, plutôt que
X simplement parce qu'il est arrivé en premier.

Un dossier peut être admissible mais `BATCH_NOT_SELECTED` : ce n'est pas une
nouvelle critique de sa prévision. Le problème n'est plus une décision immédiate
et irréversible à chaque arrivée ; c'est une allocation sur un lot fini connu.

### Limites honnêtes de l'objectif

L'optimalité est **conditionnelle aux probabilités de A et aux options
prévisualisées**, pas une garantie de gain réel. Aucune probabilité de fill n'est
inventée. Le modèle de valeur suppose une exécution complète estimée et une
résolution binaire YES/NO ; il ne valorise pas CANCEL/MKT, réinvestissement ou
durée d'immobilisation. La somme des espérances ne nécessite pas l'indépendance,
mais ne décrit pas les pertes conjointes : les plafonds par famille restent actifs.

Par défaut, les quotes valent 20 s et doivent correspondre au même prix de
référence lors du contrôle final (`max_quote_probability_move=0`). Une quotation
n'est pas un prix garanti. Les durées sont configurables, mais ne doivent pas
servir à rajeunir des observations réellement anciennes.

## 6. Réservation, envoi et incidents

Résoudre le lot et réserver **tous** ses ordres dans une seule transaction SQLite.
Une erreur d'écriture annule toutes ces nouvelles réservations. Le plan de lot,
les options, les valeurs estimées et motifs de non-sélection sont persistés ensemble.
Les engagements d'autres lots/processus sont relus sous verrou. Aucun réseau à
l'intérieur de cette transaction.

Une autorisation nomme le compte, marché, sens, montant exact, plafond total,
prix limite, empreinte du contrat/politique, échéances et lot. B reçoit la référence
enregistrée, pas un dictionnaire de A contenant `approved=True`. A ne fixe aucun
montant et n'a aucun outil direct de mise.

Avant chaque envoi : relire état, portefeuille, statut/conditions du marché et
échéances. Pour un ordre issu d'un lot, refaire aussi une prévisualisation du
**même montant** ; annuler localement avant envoi si la valeur estimée se dégrade,
si la prévisualisation est indisponible ou si les paramètres ne correspondent plus.
Pas de redimensionnement implicite, pas de réallocation cachée aux perdants du lot.

Les réservations sont atomiques **localement** ; les paris ne sont pas une
transaction atomique distante. Chaque envoi peut réussir, être partiel ou échouer.

```text
RESERVED → SENDING → FILLED / OPEN / PARTIAL / REJECTED / UNKNOWN
```

`SENDING` est enregistré avant l'appel réseau. Un timeout ou une réponse invalide
après envoi laisse `UNKNOWN`, sans retry automatique ni libération des réserves.
B recherche une confirmation non ambiguë ; le doute reste un blocage. Un reliquat
reste réservé jusqu'à confirmation de disparition ; une annulation n'efface pas
les fractions exécutées. Au redémarrage, `recover_pending()` réconcilie sans envoyer.

Une autorisation déjà consommée/expirée n'est pas renouvelée depuis le cache.
Même `batch_id` + mêmes entrées = restitution du plan existant. Changer le contenu
avec le même ID provoque une collision. Pour réévaluer un lot refusé, utiliser un
nouvel ID, en conservant les vraies analyses encore fraîches. Aucune de ces règles
ne fait disparaître les contraintes ou compteurs historiques.

Les garanties couvrent le chemin fourni. Ce n'est pas un sandbox contre un
programme Python ayant les clés et les droits d'écriture sur la base. Préférer un
compte dédié et un seul exécuteur ; ventes, prêts, transferts et opérations manuelles
non représentées ne sont pas automatiquement comptabilisés.

## 7. Modifications exactes à réaliser côté A

### Signature et appels

```python
from tools.budget import BudgetSession

def analyse_market(market: Market, *, budget: BudgetSession) -> AgentAnalysis:
    researcher_response = budget.parse(
        client,
        phase="researcher",
        web_calls=2,
        model=MODEL,
        input=research_messages,
        text_format=Forecast,
    )
    researcher = researcher_response.output_parsed
    # Éventuel deuxième appel, même façade, phase="critic".
    ...
```

Chaque appel coûteux et chaque tentative passe par cette façade ; pas de retour à
`client.responses.parse` en cas de refus. Laisser remonter erreur de budget, refus
fournisseur ou réponse incomplète. Le pipeline n'exécute pas la prévision échouée.

### Modèles et résultat

Dans `agent/schemas.py`, étendre **Forecast**, qui ne contenait pas de preuves :

```python
class Forecast(BaseModel):
    probability: float = Field(ge=0, le=1, allow_inf_nan=False)
    confidence: Literal["low", "medium", "high"]
    reasoning: str
    evidence: list[Evidence] = Field(default_factory=list)
```

Conserver `Evidence(title, url, summary, supports)` ; `supports=True` signifie
favorable à YES. Renvoyer les sources réellement utilisées par Researcher/Critic,
pas systématiquement `evidence=[]`. D rapproche les URLs des traces d'outils.

Dans `AgentAnalysis`, conserver exactement : `market_probability`,
`initial_probability`, `final_probability`, `confidence`, `evidence`, `decision`,
`reasoning`. Le champ s'appelle **decision**, pas `proposal`.

Toutes les probabilités sont celles de **YES**, entre 0 et 1. Copier
`market_probability=market.probability` depuis l'entrée, sans laisser le LLM
réécrire le prix de référence. A produit une proposition, jamais un montant.

Researcher et Critic font deux appels distincts lorsque la critique est activée.
`initial_probability` vient du premier ; `final_probability` et la confiance finale
viennent du second. Égalité des deux probabilités ne prouve pas que le Critic a tourné.
La preuve d'exécution vient de la session enregistrée. Pas de Critic fictif après erreur.

Ajouter à `Market` les champs facultatifs que Pipeline fournit déjà :

```python
close_time: float | None = None  # secondes Unix, fermeture des échanges
fetched_at: float | None = None  # référence temporelle du snapshot reçu
```

Ils permettent de donner au modèle le bon contexte temporel, sans lui demander
d'inventer ses propres dates. Les bornes Pydantic sont recommandées aussi sur les
probabilités de Market et AgentAnalysis ; D refait ses validations.

### Métadonnées : responsabilité du programme, pas de la génération

Pipeline crée une `AnalysisEnvelope` autour du résultat : `analysis_id`, `market_id`,
`reference_at`, `completed_at`, `conditions_hash`, `budget_session_id`, URLs tracées
et `critic_completed`. La session/le journal enregistrent début et fin des phases.
A ne fabrique aucun ID, timestamp, label de source vérifiée ou autorisation.
`agent_revision` doit changer si modèle, prompts ou schémas changent.

**A n'a pas à agréger le lot lui-même.** Il garde `analyse_market()` ; Pipeline
assemble ses sorties et gère le journal global. Des opérations A supplémentaires
hors façade doivent employer le journal partagé, sans exposer secrets ni prompts.

## 8. Contrat attendu de B et C

B implémente `Broker` : `fetch_market`, `fetch_portfolio`, `preview_order`,
`place_order`, `lookup_order`. Les snapshots sont datés et par véritable betId.
`cash_balance` est le solde brut confirmé : D soustrait ses réservations.
`debited_today` et `realized_loss_today` portent sur le compte entier, en UTC.

B doit notamment fournir la valorisation par palier de la section 5, indépendamment
de A. Une borne totale de débit ne se déduit pas uniquement d'un coût de dry run.
Pas de `debit_bound_guaranteed=True` ou `full_fill_estimated=True` pour « faire passer »
le contrat lorsqu'il manque l'information. Un HTTP 200 ne vaut pas FILLED.

C lit `tracker.dashboard()` (analyses, décisions, ordres, lots, métriques, derniers
logs) ou `tracker.logbook(...)`. Afficher séparément « admissible », « retenu dans
le lot », « réservé » et « effectivement exécuté », avec les raisons et valeurs.
Les événements UI hors pipeline passent explicitement par `log_event`.

## 9. Intégration et mise à jour d'une installation

Copier les fichiers de l'archive à la racine du dépôt. Les modèles de A restent
ceux du dépôt ; aucun SDK OpenAI n'est nécessaire aux tests simulés. La base
SQLite est créée à l'exécution, pas livrée dans le ZIP.

```python
from tools.tracker import Tracker
from tools.risk import RiskPolicy, BatchPolicy
from tools.budget import BudgetEngine, BudgetPolicy
from tools.cache import Cache
from tools.integration import GateService, Pipeline

tracker = Tracker("data/edgex_d.sqlite3")
gate = GateService(tracker, broker, RiskPolicy(), BatchPolicy(max_analyses=10))

# Configuration explicite pour une NOUVELLE installation. Les plafonds V1
# par défaut (dont 3 analyses/cycle) ne sont pas augmentés silencieusement.
budget = BudgetEngine(tracker, BudgetPolicy(
    analyses_per_cycle=10,
    web_calls_per_day=40,      # 10 analyses * 2 phases * 2 appels web maximum
    allowed_models=(MODEL,),
))
pipeline = Pipeline(gate, budget, Cache(tracker))

gate.recover_pending()
results = pipeline.run_cycle(
    market_ids,
    analyse_market,
    cycle_id="cycle-unique-001",
    agent_revision="model-prompts-schema-v2",
    execute=False,
)
```

`execute=False` fait des fetchs/prévisualisations et peut réserver localement,
mais n'envoie aucune mise. `review()` individuel reste disponible pour tests ou
intégration V1 ; le parcours collectif utilise bien `review_batch()`.

Les tables de log/lot s'ajoutent automatiquement à une base V1 sans effacer les
analyses, ordres ou compteurs. **Les empreintes de politiques existantes restent
verrouillées.** Pour rouvrir cette base, conserver ses anciens paramètres.
L'exemple avec dix analyses augmente explicitement une limite : il ne peut pas
être appliqué silencieusement à une base ayant une autre politique. Aucune migration
à chaud de politique n'est fournie ; faire une migration opérateur revue avant ce
changement, jamais supprimer la base pour contourner les compteurs.

## 10. Évaluation et tests

Toutes les analyses sont enregistrées avant résolution, y compris les refus et
non-sélections du lot. Résolution de marché et paiement sont deux confirmations
séparées de B. Brier : YES/NO définitifs uniquement ; première prévision par marché,
terminée avant résolution ; Researcher/Critic comparés sur le même sous-ensemble
avec Critic réellement exécuté. Sans résultat disponible : `None`, pas zéro.
P&L réalisé : débits et paiements confirmés, jamais variation de solde seule ou
valorisation d'une position ouverte présentée comme gain encaissé.

```bash
python -m unittest discover -s tests -p 'test_d.py' -v
```

Les 170 tests couvrent les contrôles V1, optimisation exacte, lots de dix,
non-sélection, frais/payouts manquants, ordre des arrivées, péremption avant/après
solveur, réservation collective/rollback, concurrence, relecture de lot,
prévisualisation avant envoi, timeout, partial fills, logs et reprise de la base.

## Références techniques consultées

Les règles de trading sont des choix de conception, pas des résultats validés par
ces sources. Les paramètres des fournisseurs peuvent évoluer.

- API Manifold : https://docs.manifold.markets/api
- Manifold, prévisualisation et placement : https://raw.githubusercontent.com/manifoldmarkets/manifold/main/backend/api/src/place-bet.ts
- OpenAI, sources des recherches : https://developers.openai.com/api/docs/guides/tools-web-search
- OpenAI, plafonds Responses : https://developers.openai.com/api/reference/cli/resources/responses/methods/create
