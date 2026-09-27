import os

import streamlit as st
from dotenv import load_dotenv

from tools.manifold import (
    get_markets,
    scout_markets,
    to_market,
    place_bet,
    get_me,
)

from agent.agent import (
    analyse_market,
    AgentError,
)

from core.risk_adapter import (
    evaluate_analysis,
)


# =========================
# ENVIRONMENT
# =========================

load_dotenv()


ALLOW_LIVE_TRADING = (
    os.getenv(
        "ALLOW_LIVE_TRADING",
        "false"
    ).lower()
    == "true"
)


MAX_BET_MANA = float(
    os.getenv(
        "MAX_BET_MANA",
        "10"
    )
)


MIN_EDGE = float(
    os.getenv(
        "MIN_EDGE",
        "0.08"
    )
)


# =========================
# PAGE CONFIG
# =========================

st.set_page_config(
    page_title="EdgeX",
    page_icon="X",
    layout="wide",
)


st.title("EdgeX")

st.caption(
    "Autonomous AI research agent "
    "for prediction markets"
)


# =========================
# SESSION STATE
# =========================

if "analysis" not in st.session_state:
    st.session_state.analysis = None

if "risk" not in st.session_state:
    st.session_state.risk = None

if "market" not in st.session_state:
    st.session_state.market = None

if "last_market_id" not in st.session_state:
    st.session_state.last_market_id = None

if "bet_result" not in st.session_state:
    st.session_state.bet_result = None


# =========================
# SIDEBAR
# =========================

st.sidebar.header(
    "Market selection"
)


topic = st.sidebar.text_input(
    "Topic",
    value="ai",
)


limit = st.sidebar.slider(
    "Markets to fetch",
    min_value=5,
    max_value=50,
    value=20,
    step=5,
)


min_volume = st.sidebar.number_input(
    "Minimum 24h volume",
    min_value=0,
    value=50,
    step=10,
)


st.sidebar.divider()


st.sidebar.subheader(
    "Safety"
)


st.sidebar.write(
    f"Maximum bet: "
    f"{MAX_BET_MANA:.0f} Mana"
)


st.sidebar.write(
    f"Minimum edge: "
    f"{MIN_EDGE:.0%}"
)


if ALLOW_LIVE_TRADING:

    st.sidebar.warning(
        "Live Mana trading enabled"
    )

else:

    st.sidebar.success(
        "Dry-run mode"
    )


# =========================
# ACCOUNT
# =========================

try:

    account = get_me()

    username = (
        account.get("username")
        or account.get("name")
        or "EdgeX"
    )

    balance = account.get(
        "balance"
    )

    st.sidebar.divider()

    st.sidebar.subheader(
        "Manifold account"
    )

    st.sidebar.write(
        username
    )

    if balance is not None:

        st.sidebar.metric(
            "Balance",
            f"{balance:.0f} Mana"
        )

except Exception:

    # Account info is useful,
    # but should never prevent the app
    # from loading.

    pass


# =========================
# FETCH MARKETS
# =========================

try:

    raw_markets = get_markets(
        limit=limit,
        topic=topic,
    )

    candidates = scout_markets(
        raw_markets,
        min_volume_24h=min_volume,
    )

    markets = [
        to_market(raw_market)
        for raw_market in candidates
    ]

except Exception as e:

    st.error(
        f"Unable to load Manifold markets: {e}"
    )

    st.stop()


if not markets:

    st.warning(
        "No suitable markets found "
        "with these filters."
    )

    st.stop()


# =========================
# MARKET SELECTOR
# =========================

market_by_id = {
    market.id: market
    for market in markets
}


selected_id = st.selectbox(
    "Choose a Manifold market",
    options=list(
        market_by_id.keys()
    ),
    format_func=lambda market_id: (
        market_by_id[
            market_id
        ].question
    ),
)


market = market_by_id[
    selected_id
]


st.session_state.market = market


# Reset when changing market

if (
    st.session_state.last_market_id
    != market.id
):

    st.session_state.analysis = None
    st.session_state.risk = None
    st.session_state.bet_result = None

    st.session_state.last_market_id = (
        market.id
    )


# =========================
# MARKET INFO
# =========================

st.subheader(
    market.question
)


col1, col2, col3 = (
    st.columns(3)
)


col1.metric(
    "Manifold probability",
    f"{market.probability:.1%}",
)


col2.metric(
    "24h volume",
    (
        f"{market.volume:.0f} Mana"
        if market.volume is not None
        else "N/A"
    ),
)


col3.metric(
    "Token",
    market.token,
)


if market.description:

    with st.expander(
        "Market description"
    ):

        st.write(
            market.description
        )


if market.url:

    st.link_button(
        "Open on Manifold",
        market.url,
    )


# =========================
# ANALYZE
# =========================

if st.button(
    "Analyze with EdgeX",
    type="primary",
):

    # Clear previous result

    st.session_state.analysis = None
    st.session_state.risk = None
    st.session_state.bet_result = None

    with st.spinner(
        "Researcher and Critic "
        "are analyzing the market..."
    ):

        try:

            analysis = analyse_market(
                market
            )

            risk = evaluate_analysis(
                market=market,
                analysis=analysis,
                max_bet_mana=MAX_BET_MANA,
                min_edge=MIN_EDGE,
            )

            st.session_state.analysis = (
                analysis
            )

            st.session_state.risk = (
                risk
            )

        except AgentError as e:

            st.error(
                f"EdgeX analysis failed: {e}"
            )

        except Exception as e:

            st.error(
                f"Unexpected error: {e}"
            )


# =========================
# ANALYSIS RESULTS
# =========================

analysis = st.session_state.analysis
risk = st.session_state.risk


if analysis is not None:

    st.divider()

    st.header(
        "EdgeX Analysis"
    )


    # =====================
    # METRICS
    # =====================

    col1, col2, col3, col4 = (
        st.columns(4)
    )


    col1.metric(
        "Market",
        f"{analysis.market_probability:.1%}",
    )


    col2.metric(
        "Researcher",
        f"{analysis.initial_probability:.1%}",
    )


    revision = (
        analysis.final_probability
        - analysis.initial_probability
    )


    col3.metric(
        "After Critic",
        f"{analysis.final_probability:.1%}",
        delta=f"{revision:+.1%}",
    )


    col4.metric(
        "Edge",
        f"{analysis.edge:+.1%}",
    )


    # =====================
    # AGENT DECISION
    # =====================

    col1, col2 = st.columns(2)


    col1.metric(
        "Confidence",
        analysis.confidence.upper(),
    )


    col2.metric(
        "Agent decision",
        analysis.decision,
    )


    if analysis.decision == "BUY_YES":

        st.success(
            "Agent decision: BUY YES"
        )

    elif analysis.decision == "BUY_NO":

        st.warning(
            "Agent decision: BUY NO"
        )

    else:

        st.info(
            "Agent decision: SKIP"
        )


    # =====================
    # RISK ENGINE
    # =====================

    st.subheader(
        "Risk Engine"
    )


    if risk is not None:

        if risk.approved:

            st.success(
                "Trade approved"
            )

            col1, col2 = (
                st.columns(2)
            )

            col1.metric(
                "Outcome",
                risk.outcome,
            )

            col2.metric(
                "Authorized amount",
                f"{risk.amount:.2f} Mana",
            )

        else:

            st.error(
                "Trade rejected"
            )

            st.write(
                f"Reason: {risk.reason}"
            )


    # =====================
    # EVIDENCE
    # =====================

    st.subheader(
        "Evidence"
    )


    researcher_col, critic_col = (
        st.columns(2)
    )


    # ---------------------
    # RESEARCHER
    # ---------------------

    with researcher_col:

        st.markdown(
            "### Researcher"
        )


        researcher_evidence = [
            evidence
            for evidence
            in analysis.evidence
            if evidence.source_agent
            == "researcher"
        ]


        if not researcher_evidence:

            st.info(
                "No researcher evidence."
            )


        for index, evidence in enumerate(
            researcher_evidence
        ):

            label = (
                "FOR"
                if evidence.supports
                else "AGAINST"
            )


            st.markdown(
                f"**{label} — "
                f"{evidence.title}**"
            )


            st.write(
                evidence.summary
            )


            if evidence.url:

                st.link_button(
                    "Source",
                    evidence.url,
                    key=(
                        f"researcher_"
                        f"{market.id}_"
                        f"{index}"
                    ),
                )


            st.divider()


    # ---------------------
    # CRITIC
    # ---------------------

    with critic_col:

        st.markdown(
            "### Critic"
        )


        critic_evidence = [
            evidence
            for evidence
            in analysis.evidence
            if evidence.source_agent
            == "critic"
        ]


        if not critic_evidence:

            st.info(
                "No critic evidence."
            )


        for index, evidence in enumerate(
            critic_evidence
        ):

            label = (
                "FOR"
                if evidence.supports
                else "AGAINST"
            )


            st.markdown(
                f"**{label} — "
                f"{evidence.title}**"
            )


            st.write(
                evidence.summary
            )


            if evidence.url:

                st.link_button(
                    "Source",
                    evidence.url,
                    key=(
                        f"critic_"
                        f"{market.id}_"
                        f"{index}"
                    ),
                )


            st.divider()


    # =====================
    # REASONING
    # =====================

    st.subheader(
        "Final reasoning"
    )


    st.write(
        analysis.reasoning
    )


    # =====================
    # BET EXECUTION
    # =====================

    if (
        risk is not None
        and risk.approved
    ):

        st.divider()

        st.subheader(
            "Manifold Execution"
        )


        st.write(
            f"EdgeX wants to buy "
            f"**{risk.outcome}** "
            f"for **{risk.amount:.2f} Mana**."
        )


        # -----------------
        # DRY RUN
        # -----------------

        if st.button(
            "Test trade (dry-run)",
            key="dry_run_button"
        ):

            try:

                result = place_bet(
                    market_id=market.id,
                    outcome=risk.outcome,
                    amount=risk.amount,
                    dry_run=True,
                )

                st.session_state.bet_result = (
                    result
                )

                st.success(
                    "Manifold dry-run successful."
                )

            except Exception as e:

                st.error(
                    f"Dry-run failed: {e}"
                )


        # -----------------
        # LIVE MANA
        # -----------------

        if ALLOW_LIVE_TRADING:

            st.warning(
                "Live Mana trading is enabled."
            )


            confirm = st.checkbox(
                "I confirm that EdgeX may "
                "spend play-money Mana."
            )


            if st.button(
                "Place Mana bet",
                disabled=not confirm,
                type="primary",
                key="live_bet_button"
            ):

                try:

                    result = place_bet(
                        market_id=market.id,
                        outcome=risk.outcome,
                        amount=risk.amount,
                        dry_run=False,
                    )

                    st.session_state.bet_result = (
                        result
                    )

                    st.success(
                        "Mana bet placed successfully."
                    )

                except Exception as e:

                    st.error(
                        f"Bet failed: {e}"
                    )


        else:

            st.info(
                "Live trading disabled. "
                "Set ALLOW_LIVE_TRADING=true "
                "to enable Mana bets."
            )


    # =====================
    # BET RESULT
    # =====================

    if (
        st.session_state.bet_result
        is not None
    ):

        st.subheader(
            "Execution result"
        )

        result = (
            st.session_state.bet_result
        )


        if result.get(
            "betId"
        ) == "dry-run":

            st.info(
                "Dry-run only — "
                "no Mana was spent."
            )

        else:

            st.success(
                "Transaction recorded by Manifold."
            )


        col1, col2, col3 = (
            st.columns(3)
        )


        col1.metric(
            "Amount",
            f"{result.get('amount', 0):.2f} Mana",
        )


        col2.metric(
            "Shares",
            f"{result.get('shares', 0):.2f}",
        )


        probability_after = (
            result.get(
                "probAfter"
            )
        )


        if probability_after is not None:

            col3.metric(
                "Probability after",
                f"{probability_after:.1%}",
            )


        with st.expander(
            "Raw Manifold response"
        ):

            st.json(
                result
            )


    # =====================
    # DEBUG
    # =====================

    with st.expander(
        "Structured EdgeX output"
    ):

        st.json(
            analysis.model_dump()
        )