import streamlit as st

from tools.manifold import (
    get_markets,
    scout_markets,
    to_market,
)

from agent.agent import (
    analyse_market,
    AgentError,
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
    "Autonomous AI research agent for prediction markets"
)


# =========================
# SESSION STATE
# =========================

if "analysis" not in st.session_state:
    st.session_state.analysis = None

if "market" not in st.session_state:
    st.session_state.market = None

if "last_market_id" not in st.session_state:
    st.session_state.last_market_id = None


# =========================
# SIDEBAR
# =========================

st.sidebar.header("Market selection")

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
        "No suitable markets found with these filters."
    )
    st.stop()


# =========================
# SELECT MARKET
# =========================

market_by_id = {
    market.id: market
    for market in markets
}

selected_id = st.selectbox(
    "Choose a Manifold market",
    options=list(market_by_id.keys()),
    format_func=lambda market_id: (
        market_by_id[market_id].question
    ),
)

market = market_by_id[selected_id]

st.session_state.market = market


# Reset previous analysis if the user changes market

if (
    st.session_state.last_market_id
    != market.id
):
    st.session_state.analysis = None
    st.session_state.last_market_id = (
        market.id
    )


# =========================
# MARKET CARD
# =========================

st.subheader(
    market.question
)

col1, col2, col3 = st.columns(3)

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
# ANALYSIS BUTTON
# =========================

if st.button(
    "Analyze with EdgeX",
    type="primary",
):

    with st.spinner(
        "Researcher and Critic are analyzing the market..."
    ):

        try:
            analysis = analyse_market(
                market
            )

            st.session_state.analysis = (
                analysis
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
# ANALYSIS
# =========================

analysis = st.session_state.analysis


if analysis is not None:

    st.divider()

    st.header(
        "EdgeX Analysis"
    )

    # -------------------------
    # MAIN METRICS
    # -------------------------

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


    # -------------------------
    # DECISION
    # -------------------------

    left, right = st.columns(2)

    left.metric(
        "Confidence",
        analysis.confidence.upper(),
    )

    right.metric(
        "Decision",
        analysis.decision,
    )


    if analysis.decision == "BUY_YES":
        st.success(
            "EdgeX recommends BUY YES."
        )

    elif analysis.decision == "BUY_NO":
        st.warning(
            "EdgeX recommends BUY NO."
        )

    else:
        st.info(
            "EdgeX recommends SKIP."
        )


    # =========================
    # EVIDENCE
    # =========================

    st.subheader(
        "Evidence"
    )

    researcher_col, critic_col = (
        st.columns(2)
    )


    # -------------------------
    # RESEARCHER
    # -------------------------

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


    # -------------------------
    # CRITIC
    # -------------------------

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


    # =========================
    # REASONING
    # =========================

    st.subheader(
        "Final reasoning"
    )

    st.write(
        analysis.reasoning
    )


    # =========================
    # DEBUG
    # =========================

    with st.expander(
        "Structured output"
    ):
        st.json(
            analysis.model_dump()
        )
