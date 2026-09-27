# EdgeX — corrections de présentation et observation courte

Cette mise à jour restaure les explications initiales/finales, les sources Researcher/Critic et la trace des étapes dans la vue portefeuille. `app.py` (V1) reste intact.

## Lancer

Depuis la branche `feature/portfolio-v2-hackathon` :

```powershell
git pull --ff-only origin feature/portfolio-v2-hackathon
uv pip install -r requirements-portfolio.txt --python .\.venv\Scripts\python.exe
.\.venv\Scripts\python.exe -m streamlit run portfolio_app.py
```

Ne changer de branche qu'après avoir sauvegardé les modifications locales. Ne pas effacer de base SQLite.

## Trois durées différentes

- **Clôture au plus tard dans** : filtre strict, dont 15 minutes, basé sur `closeTime`. Tri `close-date`, puis validation des détails. Aucun élargissement silencieux. L'absence de résultat signifie qu'aucun candidat n'a été trouvé dans la collecte bornée, pas une preuve d'absence sur tout Manifold.
- **Durée maximale du cycle** : limite de démarrage des appels ; une part est réservée aux prévisualisations. Un appel en cours reste soumis à son propre timeout.
- **Durée d'observation** : suivi prospectif des prix, pas attente garantie d'un paiement. La clôture n'est pas la résolution.

## Pourquoi il peut n'y avoir aucune position

L'écran explique séparément : échec d'API/modèle, budget épuisé, absence de sources, confiance faible, écart sous le seuil, coût/frais, remplissage partiel, délai/péremption et contraintes de portefeuille. Les contrôles ne sont pas abaissés en secret pour obtenir une belle vidéo.

Le modèle est maintenant sélectionnable dans l'interface. Les modèles doivent être disponibles pour la clé du projet. Les erreurs 401/403/404/429 sont explicites, sans journaliser de secret.

Le temps réservé aux prévisualisations empêche que toutes les analyses consomment la fenêtre entière. Une erreur sur la cotation d'un gros palier ne détruit plus les petits paliers réussis. Le plafond par marché porte sur le coût estimé avec réserve, pas seulement le montant envoyé à l'API.

## Rendu

Premier onglet : comparaison Manifold/Researcher/Critic, critères, explications publiques séparées, sources et synthèse finale. Une explication initiale absente d'une ancienne entrée de cache est signalée, jamais recréée.

Onglet Workflow : étapes terminées/échouées et raisons par réponse et par taille. Une analyse échouée n'est pas affichée comme un edge nul.

Onglet Observation : ouvrir le panier **en papier**, puis observer 15 minutes. Les positions sont persistées et le double clic ne crée pas un second panier. Actualisation manuelle ou toutes les 15 secondes pendant la session Streamlit active. Aucun appel IA pour observer. À la fin de la fenêtre, une dernière observation puis arrêt des lectures automatiques ; une lecture manuelle reste possible plus tard.

S'il n'y a pas de position de stratégie, une **expérience papier de 1 Mana, hors stratégie**, peut être choisie explicitement sur un dossier analysé. Elle peut avoir une espérance négative et ne se présente pas comme un choix rentable de l'optimiseur. Aucun gain ni historique n'est fabriqué.

## Ce que cette version ne fait pas

Pas de mise réelle, pas de vente automatique après 15 minutes, pas de résolution automatique et pas de revenu garanti. L'ajout d'un mode de mise réelle n'est pas publié dans cette correction. La V1 existante n'est pas modifiée.

Les valorisations papier utilisent les prix affichés ; ce ne sont pas des prix de liquidation garantis. Les frais d'entrée sont estimés avec réserve. Les règlements binaires YES/NO confirmés peuvent être évalués pour le papier ; annulations/pondérations non prises en charge restent non mesurées. Les gains Mana et coûts USD restent distincts. Une espérance à résolution n'est pas une prévision du cours à 15 minutes.

La démonstration synthétique est explicitement signalée et ne fabrique pas de mouvements de prix pour simuler une performance.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_portfolio_v2.py tests/test_portfolio_ui.py tests/test_portfolio_showcase.py
```

Tous les tests interdisent le réseau. Les tests Streamlit requièrent les dépendances de `requirements-portfolio.txt`. Le workflow GitHub les exécute sur Python 3.11 et 3.13. Aucun test authentifié/payant n'est lancé par cette correction.
