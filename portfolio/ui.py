"""Optional portfolio UI. The team's existing app.py is left untouched."""
from __future__ import annotations

import json
import os

from dotenv import load_dotenv


def main():
    import pandas as pd
    import streamlit as st
    from .demo import build_demo
    from .engine import run_cycle
    from .manifold import Manifold, TOPICS
    from .models import Policy
    from .research import Researcher, ResearchPolicy, Store

    load_dotenv()
    st.set_page_config(page_title='EdgeX — Portefeuille', page_icon='📊', layout='wide')
    st.title('EdgeX · Portefeuille')
    st.caption('Researcher → Critic → allocation collective sous contraintes. Binaire + multi-réponses.')
    st.info('Cette vue ne passe AUCUN pari réel. Elle construit un portefeuille proposé ; les gains affichés sont des estimations, pas du P&L réalisé.')
    with st.form('portfolio_configuration'):
        mode = st.radio('Données', ['Démonstration hors ligne', 'Manifold + OpenAI'], horizontal=True)
        domains = st.multiselect('Domaines (hors politique)', list(TOPICS), default=list(TOPICS))
        c1, c2, c3 = st.columns(3)
        count = c1.slider('Marchés maximum', 1, 8, 6)
        capital = c2.number_input('Capital de simulation (Mana)', min_value=10.0, max_value=10000.0, value=100.0, step=10.0)
        edge = c3.number_input('Marge minimale (points)', min_value=1.0, max_value=50.0, value=8.0, step=1.0)
        c1, c2, c3 = st.columns(3)
        per_market = c1.number_input('Plafond / marché (Mana)', min_value=1.0, value=20.0, step=1.0)
        per_domain = c2.number_input('Plafond / domaine et famille (Mana)', min_value=1.0, value=30.0, step=1.0)
        research_usd = c3.number_input('Réservation budget IA / cycle (USD)', min_value=.25, max_value=8.0, value=2.0, step=.25)
        approved = st.checkbox('En mode Manifold, je confirme le périmètre non politique et les appels IA payants.')
        submitted = st.form_submit_button('Construire le panier', type='primary')
    st.caption('Capital hypothétique, pas le solde réel du compte. Les réservations USD ne garantissent pas la facture fournisseur. La V1 reste disponible dans app.py.')
    if submitted:
        st.session_state.pop('portfolio_v2_report', None)
        policy = Policy(bankroll=capital, cash_reserve=capital*.4, max_total=capital*.6,
                        max_market=per_market, max_domain=per_domain, max_family=per_domain,
                        min_edge=edge/100, max_markets=count)
        try:
            if not domains:
                st.error('Choisis au moins un domaine.')
                return
            if mode == 'Démonstration hors ligne':
                report = build_demo(policy, domains)
            else:
                if not approved:
                    st.error('Confirme le périmètre et les appels payants avant de continuer.')
                    return
                if not os.getenv('OPENAI_API_KEY') or not os.getenv('MANIFOLD_API_KEY'):
                    st.error('OPENAI_API_KEY et MANIFOLD_API_KEY sont requises dans .env. Aucune clé n’est requise pour la démo.')
                    return
                source = Manifold()
                store = Store()
                researcher = Researcher(store, policy=ResearchPolicy(calls_per_run=count*2,
                                        reserve_per_run_usd=research_usd))
                with st.spinner('Collecte, recherche et prévisualisations…'):
                    values, rejected = source.discover(domains, limit=count)
                    if not values:
                        st.warning('Aucun marché compatible. Aucune analyse IA lancée.')
                        st.json(rejected)
                        return
                    progress = st.empty()
                    report = run_cycle(values, researcher, source, policy=policy, store=store,
                        progress=lambda e: progress.caption(f"{e['stage']} · {e['market_id']}"))
                    report['discovery_errors'] = rejected
                    report['data_origin'] = 'MANIFOLD_AND_OPENAI_PREVIEW'
                    store.save_report(report['run_id'], report)
            st.session_state.portfolio_v2_report = report
        except Exception as exc:
            st.error(f'Cycle arrêté sans mise : {type(exc).__name__}. Vérifie la configuration et les limites ; ne partage pas tes clés.')
            return
    report = st.session_state.get('portfolio_v2_report')
    if not report:
        return
    if report.get('data_origin') == 'SYNTHETIC_DEMO':
        st.warning('DÉMONSTRATION : marchés, prévisions et exécutions entièrement synthétiques. Ce résultat ne mesure aucune performance réelle.')
    plan, usage = report['plan'], report['usage']
    a, b, c, d = st.columns(4)
    a.metric('Engagement estimé', f"{plan['estimated_debit']:.2f} Mana")
    b.metric('Cash simulé restant', f"{plan['cash_remaining']:.2f} Mana")
    c.metric('Profit espéré, non garanti', f"{plan['expected_profit']:+.2f} Mana")
    d.metric('P&L réalisé', 'Non mesuré')
    tab1, tab2, tab3, tab4 = st.tabs(['Portefeuille', 'Analyses', 'Budget et observations', 'Export'])
    with tab1:
        selected = plan['selected']
        if selected:
            rows = [dict(marche=o['question'], reponse=o['answer_label'], cote=o['side'],
                         domaine=o['domain'], engagement=o['estimated_debit'], profit_espere=o['expected_profit'],
                         edge_points=100*(o['agent_probability']-o['market_probability'])) for o in selected]
            frame = pd.DataFrame(rows)
            st.dataframe(frame, use_container_width=True, hide_index=True)
            left, right = st.columns(2)
            left.subheader('Allocation par domaine')
            left.bar_chart(frame.groupby('domaine')['engagement'].sum())
            right.subheader('Écart par position (points)')
            right.bar_chart(frame.set_index('marche')['edge_points'])
        else:
            st.info('Aucune position retenue. Rester en cash est une décision valide.')
        st.caption(f"Recherche : {plan['searched_nodes']} nœuds. Optimum sur les seules options fournies : {plan['optimal_for_supplied_options']}. Au maximum une réponse/un côté par question ; pas de corrélations statistiques inventées.")
        st.subheader('Refus / dossiers non retenus')
        st.dataframe(pd.DataFrame(report['errors']), use_container_width=True)
    with tab2:
        by_id = {m['id']: m for m in report['markets']}
        for analysis in report['analyses']:
            market = by_id[analysis['market_id']]
            with st.expander(market['question']):
                rows = [dict(reponse=a['label'], marche=a['probability'],
                             researcher=analysis['initial'][a['id']], critic=analysis['final'][a['id']])
                        for a in market['answers']]
                st.dataframe(pd.DataFrame(rows), hide_index=True)
                st.bar_chart(pd.DataFrame(rows).set_index('reponse'))
                st.write(analysis['reasoning'])
                st.caption(f"Confiance : {analysis['confidence']} · Critic terminé : {analysis['critic_completed']} · Source : {analysis['source']}")
                st.dataframe(pd.DataFrame(analysis['evidence']), use_container_width=True)
    with tab3:
        a, b, c = st.columns(3)
        a.metric('Appels IA', usage['calls'])
        b.metric('Coût IA estimé documenté', f"${usage['known_cost_usd']:.4f}")
        c.metric('Durée du cycle', f"{report['duration_seconds']:.1f} s")
        if usage['unknown_cost_calls']:
            st.warning(f"{usage['unknown_cost_calls']} appel(s) avec coût inconnu : les réservations sont conservées. Le coût affiché n’est pas le coût total.")
        st.caption('Coût estimé selon tarifs configurés, sans réduction pour crédits offerts ; à rapprocher de la facture. Gains Mana et coûts USD restent des unités séparées.')
        st.dataframe(pd.DataFrame(report['events']), use_container_width=True, hide_index=True)
        st.json(usage)
    with tab4:
        st.download_button('Télécharger le rapport JSON', json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
                           file_name=f"edgex-{report['run_id']}.json", mime='application/json')
        st.json({'mode': report['mode'], 'origine': report.get('data_origin'), 'parametres': report['policy']})


if __name__ == '__main__':
    main()
