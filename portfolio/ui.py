"""Portfolio UI with the V1's explanations/sources and an observable workflow.
The existing app.py remains unchanged. This extension does not send live bets.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv


def _date(value):
    return datetime.fromtimestamp(value, timezone.utc).strftime('%d/%m %H:%M:%S UTC')


def _sources(st, evidence):
    from .models import canonical_url
    if not evidence:
        st.info('Aucune source traçable pour cette phase.')
    for i, e in enumerate(evidence):
        st.markdown('**' + e.get('title', 'Source') + '**')
        st.write(e.get('summary', ''))
        url = canonical_url(e.get('url', ''))
        if url:
            st.link_button('Consulter la source', url)


def _monitor(st, pd, report, minutes):
    from .manifold import Manifold
    from .observation import Observation
    from .research import Store
    from .showcase import error_info

    st.subheader('Observation prospective — positions papier')
    st.caption('Aucun ordre réel. Parts simulées au départ, puis prix Manifold observés. Le résultat marqué n’est ni un paiement ni un prix de vente garanti.')
    store = Store()
    observer = Observation(store)
    if report['plan']['selected']:
        if st.button('Ouvrir ce panier en papier et observer', key='observe_plan'):
            try:
                st.session_state['observation_id'] = observer.start(report, minutes)
            except Exception as exc:
                st.error(error_info(exc)['reason'])
    elif report.get('data_origin') == 'MANIFOLD_AND_OPENAI_PREVIEW' and report['analyses']:
        st.info('Aucune opportunité retenue par la stratégie. Une expérience papier distincte peut tester la mesure sans prétendre être rentable.')
        with st.expander('Expérience papier de 1 Mana — hors stratégie'):
            by_id = {m['id']:m for m in report['markets']}
            ids = [a['market_id'] for a in report['analyses'] if a['market_id'] in by_id]
            mid = st.selectbox('Marché de mesure', ids, format_func=lambda x:by_id[x]['question'], key='probe_market')
            answers = {a['id']:a['label'] for a in by_id[mid]['answers']}
            aid = st.selectbox('Réponse observée', list(answers), format_func=lambda x:answers[x], key='probe_answer')
            side = st.selectbox('Côté de mesure', ['YES','NO'], key='probe_side')
            agreed = st.checkbox('Je distingue ce test papier d’une position recommandée et d’un pari réel.', key='probe_confirm')
            if st.button('Prévisualiser et ouvrir 1 Mana papier', disabled=not agreed, key='probe_start'):
                try:
                    st.session_state['observation_id'] = observer.start_probe(report, Manifold(), mid, aid, side, minutes, confirmed=agreed)
                except Exception as exc:
                    st.error(error_info(exc)['reason'])
    ident = st.session_state.get('observation_id')
    if not ident:
        return
    auto = st.checkbox('Actualiser les observations toutes les 15 secondes pendant cette session', value=False, key='observe_auto')

    @st.fragment(run_every=15 if auto else None)
    def live_observation():
        try:
            watch = observer.get(ident)
            ended = time.time() >= watch['ends_at']
            need_final = not watch['points'] or watch['points'][-1]['at'] < watch['ends_at']
            manual = st.button('Actualiser les prix / la résolution (sans IA)', key='observe_refresh')
            if manual or (auto and (not ended or need_final)):
                watch = observer.update(ident, Manifold())
            st.write('**Statut : ' + watch['label'] + '**')
            st.write(f"Début : {_date(watch['started_at'])} · Fin d’observation demandée : {_date(watch['ends_at'])}")
            if ended:
                st.info('Fenêtre d’observation terminée. Cela ne clôture pas le marché et ne déclenche aucun paiement.')
            else:
                st.caption(f"Temps restant à observer : {max(0,int(watch['ends_at']-time.time()))} secondes")
            latest = watch['points'][-1]['positions']
            complete = all(p['pnl_marked'] is not None for p in latest)
            a,b,c = st.columns(3)
            a.metric('Positions PAPIER', len(watch['positions']))
            b.metric('Résultat papier marqué', f"{sum(p['pnl_marked'] for p in latest):+.3f} Mana" if complete else 'Donnée indisponible')
            c.metric('Paiement réel', 'Non applicable')
            st.dataframe(pd.DataFrame(latest), hide_index=True, use_container_width=True)
            points = []
            for point in watch['points']:
                values = [p['pnl_marked'] for p in point['positions']]
                points.append(dict(instant=datetime.fromtimestamp(point['at'],timezone.utc),
                    resultat_papier=None if any(v is None for v in values) else sum(values)))
            if len(points)>1:
                st.line_chart(pd.DataFrame(points).set_index('instant'))
            else:
                st.caption('Premier point enregistré. Le graphique apparaîtra après une nouvelle observation ; aucun historique n’est fabriqué.')
            st.caption(f"Profit théorique à résolution initialement estimé : {watch['expected_profit']:+.3f} Mana. Il ne prédit pas le mouvement du prix sur 15 minutes.")
            if watch['origin']=='SYNTHETIC_DEMO':
                st.warning('Données synthétiques : aucun cours ni règlement réel ne sera inventé pendant cette observation.')
        except Exception as exc:
            st.error(error_info(exc)['reason'])
    live_observation()


def main():
    import pandas as pd
    import streamlit as st
    from .demo import markets as demo_markets, DemoResearcher, DemoSource
    from .manifold import TOPICS
    from .models import Policy
    from .research import ResearchPolicy, Store
    from .showcase import HorizonManifold, NarratedResearcher, run_showcase, error_info

    load_dotenv()
    st.set_page_config(page_title='EdgeX — Portefeuille', page_icon='📊', layout='wide')
    st.title('EdgeX · Portefeuille')
    st.caption('Explorer → Researcher → Critic → risque → allocation du panier → observation')
    st.info('Mode prévisualisation / papier. Aucun pari réel n’est envoyé par cette V2. Une clôture dans 15 min ne garantit ni résolution, ni revenu, ni paiement dans 15 min.')
    with st.form('portfolio_configuration'):
        mode = st.radio('Données', ['Démonstration hors ligne','Manifold + OpenAI'], horizontal=True)
        domains = st.multiselect('Domaines (hors politique)', list(TOPICS), default=list(TOPICS))
        c1,c2,c3 = st.columns(3)
        count = c1.slider('Marchés maximum',1,8,3)
        capital = c2.number_input('Capital de simulation (Mana)',min_value=10.0,max_value=10000.0,value=100.0,step=10.0)
        edge = c3.number_input('Marge minimale (points)',min_value=1.0,max_value=50.0,value=8.0,step=1.0)
        c1,c2,c3 = st.columns(3)
        horizon_label = c1.selectbox('Clôture du marché au plus tard dans', ['Sans limite','15 minutes','30 minutes','60 minutes','6 heures','24 heures'], key='close_horizon')
        observation_minutes = c2.number_input('Durée d’observation (minutes)',min_value=1,max_value=1440,value=15)
        cycle_seconds = c3.number_input('Durée maximale du cycle (secondes)',min_value=30,max_value=600,value=180,step=30)
        st.caption('Clôture = fin des mises. Résolution = résultat déclaré. Observation = durée de votre mesure. Ces trois durées sont distinctes.')
        c1,c2,c3 = st.columns(3)
        per_market = c1.number_input('Plafond / marché (Mana)',min_value=1.0,value=20.0,step=1.0)
        per_domain = c2.number_input('Plafond / domaine et famille (Mana)',min_value=1.0,value=30.0,step=1.0)
        min_volume = c3.number_input('Volume 24 h minimum',min_value=0.0,value=0.0,step=10.0)
        c1,c2 = st.columns(2)
        model = c1.text_input('Modèle OpenAI à utiliser',value=os.getenv('PORTFOLIO_MODEL') or os.getenv('OPENAI_MODEL') or 'gpt-6-luna')
        research_usd = c2.number_input('Réservation budget IA / cycle (USD)',min_value=.25,max_value=8.0,value=2.0,step=.25)
        approved = st.checkbox('En mode Manifold, je confirme le périmètre non politique et les appels IA payants.')
        submitted = st.form_submit_button('Construire le panier',type='primary')
    st.caption('Trois marchés par défaut pour garder du temps au Critic et aux prévisualisations. Les seuils sont visibles et ne sont jamais abaissés automatiquement pour forcer une position.')
    if submitted:
        st.session_state.pop('portfolio_v2_report',None)
        st.session_state.pop('observation_id',None)
        tiers = tuple(v for v in (5,10,20) if v < per_market) or (1.0,)
        policy = Policy(bankroll=capital,cash_reserve=capital*.4,max_total=capital*.6,
            max_market=per_market,max_domain=per_domain,max_family=per_domain,
            min_edge=edge/100,max_markets=count,tiers=tiers,max_quote_age=60)
        horizon = {'Sans limite':None,'15 minutes':15,'30 minutes':30,'60 minutes':60,'6 heures':360,'24 heures':1440}[horizon_label]
        try:
            if not domains:
                st.error('Choisis au moins un domaine.')
                return
            if mode=='Manifold + OpenAI' and not approved:
                st.error('Confirme le périmètre et les appels payants avant de continuer.')
                return
            if mode=='Manifold + OpenAI' and (not os.getenv('OPENAI_API_KEY') or not os.getenv('MANIFOLD_API_KEY')):
                st.error('OPENAI_API_KEY et MANIFOLD_API_KEY requises dans .env. Ne les partage pas.')
                return
            with st.status('Workflow EdgeX en cours',expanded=True) as status:
                stage = st.empty()
                timeline = st.empty()
                rows = []
                def progress(e):
                    rows.append(dict(etape=e['stage'],etat=e.get('status',''),marche=e.get('market_id',''),secondes=e.get('elapsed_seconds',0)))
                    stage.write(f"**{e['stage']} · {e.get('status','')}**")
                    timeline.dataframe(pd.DataFrame(rows),hide_index=True,use_container_width=True)
                stage.write('Collecte et validation des critères de résolution…')
                rejected = []
                store = Store()
                if mode=='Démonstration hors ligne':
                    values = [m for m in demo_markets() if m.domain in domains][:count]
                    researcher,source = DemoResearcher(),DemoSource(values)
                    report = run_showcase(values,researcher,source,policy=policy,seconds=cycle_seconds,progress=progress)
                    report['data_origin']='SYNTHETIC_DEMO'
                    report['horizon_cutoff']=None
                else:
                    source = HorizonManifold(horizon_minutes=horizon,max_requests=150)
                    values,rejected = source.discover(domains,limit=count,min_volume=min_volume,deadline_seconds=30)
                    if not values:
                        status.update(label='Aucun marché compatible trouvé — aucun appel IA lancé',state='complete')
                        st.warning('Aucun marché trouvé avec ces critères. Le filtre 15 min peut être vide : ne pas confondre cela avec un échec du modèle.')
                        st.dataframe(pd.DataFrame(rejected),hide_index=True,use_container_width=True)
                        return
                    researcher = NarratedResearcher(store,model=model.strip(),policy=ResearchPolicy(
                        calls_per_run=count*2,reserve_per_run_usd=research_usd,timeout=50,output_tokens=2400))
                    report = run_showcase(values,researcher,source,policy=policy,store=store,seconds=cycle_seconds,progress=progress)
                    report['data_origin']='MANIFOLD_AND_OPENAI_PREVIEW'
                    report['horizon_cutoff']=source.cutoff
                report['discovery_errors']=rejected
                report['observation_minutes']=observation_minutes
                report['horizon_label']=horizon_label
                store.save_report(report['run_id'],report)
                st.session_state.portfolio_v2_report=report
                status.update(label=f"Cycle terminé · {len(report['analyses'])} analyses · {len(report['plan']['selected'])} positions proposées",state='complete',expanded=True)
        except Exception as exc:
            st.error(error_info(exc)['reason'])
            st.json(error_info(exc))
            return
    report=st.session_state.get('portfolio_v2_report')
    if not report:
        return
    if report.get('data_origin')=='SYNTHETIC_DEMO':
        st.warning('DÉMONSTRATION SYNTHÉTIQUE : marchés, prévisions et prévisualisations fictifs. Les échéances ne sont pas celles de vrais marchés. Aucun résultat réel revendiqué.')
    plan,usage=report['plan'],report['usage']
    a,b,c,d=st.columns(4)
    a.metric('Positions proposées',len(plan['selected']))
    b.metric('Engagement estimé',f"{plan['estimated_debit']:.2f} Mana")
    c.metric('Profit espéré à résolution',f"{plan['expected_profit']:+.2f} Mana")
    d.metric('Mises réelles par cette V2',0)
    if not plan['selected']:
        st.warning('Aucune position de stratégie retenue. Les explications et diagnostics ci-dessous distinguent erreur API, budget, manque de sources, seuil, frais et contraintes.')
    tabs=st.tabs(['Analyses et raisonnement','Portefeuille','Workflow et diagnostics','Observation 15 min','Coûts et export'])
    with tabs[0]:
        by_id={m['id']:m for m in report['markets']}
        for index,analysis in enumerate(report['analyses']):
            m=by_id[analysis['market_id']]
            with st.expander(m['question'],expanded=index==0):
                st.caption(f"{m['domain']} · {m['kind']} · clôture {_date(m['close_time'])} · source {analysis['source']}")
                st.write('**Critères de résolution**')
                st.write(m['description'])
                frame=pd.DataFrame([dict(reponse=a['label'],Manifold=a['probability'],Researcher=analysis['initial'][a['id']],
                    Critic=analysis['final'][a['id']],edge_points=100*(analysis['final'][a['id']]-a['probability'])) for a in m['answers']])
                st.dataframe(frame,hide_index=True,use_container_width=True)
                st.bar_chart(frame.set_index('reponse')[['Manifold','Researcher','Critic']])
                st.caption(f"Confiance {analysis['confidence']} · Critic terminé : {analysis['critic_completed']}. Edge en points ; pas un rendement garanti.")
                phases=report.get('phase_reports',{}).get(m['id'],{})
                left,right=st.columns(2)
                for column,phase,title in ((left,'researcher','Researcher — estimation initiale'),(right,'critic','Critic — remise en question')):
                    with column:
                        st.subheader(title)
                        detail=phases.get(phase,{})
                        if detail.get('reasoning'):
                            st.write(detail['reasoning'])
                        elif detail.get('reason'):
                            st.warning(detail['reason'])
                        elif analysis['source']=='SYNTHETIC_DEMO':
                            st.caption('Phase simulée : aucune recherche réelle, données prévues pour le test du workflow.')
                        else:
                            st.caption('Explication de phase non conservée dans cette entrée du cache ; synthèse finale ci-dessous.')
                        _sources(st,[e for e in analysis['evidence'] if e['source_agent']==phase])
                st.subheader('Synthèse finale de l’agent')
                st.write(analysis['reasoning'])
        if not report['analyses']:
            st.error('Aucune analyse terminée. Voir les diagnostics techniques ; ce n’est pas un edge de 0 %.')
    with tabs[1]:
        selected=plan['selected']
        if selected:
            frame=pd.DataFrame([dict(marche=o['question'],reponse=o['answer_label'],cote=o['side'],domaine=o['domain'],
                mise=o['amount'],cout_estime=o['estimated_debit'],profit_espere=o['expected_profit'],
                edge_points=100*(o['agent_probability']-o['market_probability'])) for o in selected])
            st.dataframe(frame,hide_index=True,use_container_width=True)
            left,right=st.columns(2)
            left.subheader('Allocation par domaine')
            left.bar_chart(frame.groupby('domaine')['cout_estime'].sum())
            right.subheader('Edge par position (points)')
            right.bar_chart(frame.set_index('marche')['edge_points'])
        st.metric('Cash simulé restant',f"{plan['cash_remaining']:.2f} Mana")
        st.caption(f"Optimum parmi les options fournies : {plan['optimal_for_supplied_options']} · {plan['searched_nodes']} nœuds. Contraintes de concentration, pas de garantie de gain ni d’optimisation statistique des corrélations.")
        st.dataframe(pd.DataFrame(report['errors']),hide_index=True,use_container_width=True)
    with tabs[2]:
        st.subheader('Trace complète des étapes')
        st.dataframe(pd.DataFrame(report['events']),hide_index=True,use_container_width=True)
        st.subheader('Pourquoi chaque réponse / taille est acceptée ou refusée')
        st.dataframe(pd.DataFrame(report.get('diagnostics',[])),hide_index=True,use_container_width=True)
        st.subheader('Erreurs techniques / budgets / données')
        st.dataframe(pd.DataFrame(report['errors']),hide_index=True,use_container_width=True)
        if report.get('discovery_errors'):
            with st.expander('Marchés écartés à la collecte'):
                st.dataframe(pd.DataFrame(report['discovery_errors']),hide_index=True,use_container_width=True)
        st.caption(f"Temps réservé à la recherche : {report.get('research_seconds_reserved',0):.0f}s · Prévisualisations : {report.get('preview_seconds_reserved',0):.0f}s. Une erreur sur un palier ne supprime plus les plus petits paliers réussis.")
    with tabs[3]:
        _monitor(st,pd,report,report.get('observation_minutes',15))
    with tabs[4]:
        a,b,c=st.columns(3)
        a.metric('Appels IA',usage['calls'])
        b.metric('Coût IA estimé documenté',f"${usage['known_cost_usd']:.4f}")
        c.metric('Durée du cycle',f"{report['duration_seconds']:.1f}s")
        st.write('Modèle réellement demandé :',report['model'])
        if usage['unknown_cost_calls']:
            st.warning(f"{usage['unknown_cost_calls']} appel(s) de coût inconnu : le montant connu n’est pas la facture totale. Réservations conservées.")
        st.caption('Mana et USD restent séparés. Un profit théorique à résolution ne se compare pas directement à une variation de prix sur 15 minutes.')
        st.json(usage)
        st.download_button('Télécharger le rapport JSON',json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),
            file_name=f"edgex-{report['run_id']}.json",mime='application/json')


if __name__=='__main__':
    main()
