import streamlit as st

from tools.manifold import (
    get_markets,
    scout_markets,
    to_market
)

from agent.agent import (
    analyse_market,
    AgentError
)


# =========================
# PAGE CONFIG
# =========================

st.set_page_config(
    page_title="EdgeX",
    page_icon="X",
    layout="wide"
)

st.title("EdgeX")

st.caption(
    "Autonomous AI agent for prediction markets"
)


# =========================
# SESSION STATE
# =========================

if "analysis" not in st.session_state:
    st.session_state.analysis = None

if "selected_market" not in st.session_state:
    st.session_state.selected_market = None

if "last_market_id" not in st.session_state:
    st.session_state.last_market_id = None


# =========================
# SIDEBAR
# =========================

st.sidebar.header(
    "Market selection"
)

topic = st.sidebar.text_input(
    "Topic",
    value="ai"
)

limit = st.sidebar.slider(
    "Markets to fetch",
    min_value=5,
    max_value=50,
    value=20,
    step=5
)

min_volume = st.sidebar.number_input(
    "Minimum 24h volume",
    min_value=0,
    value=50,
    step=10
)


# =========================
# FETCH MANIFOLD MARKETS
# =========================

try:

    raw_markets = get_markets(
        limit=limit,
        topic=topic
    )

    candidates = scout_markets(
        raw_markets,
        min_volume_24h=min_volume
    )

    markets = [
        to_market(m)
        for m in candidates
    ]

except Exception as e:

    st.error(
        f"Unable to load Manifold markets: {e}"
    )

    st.stop()


if not markets:

    st.warning(
        "No suitable Manifold markets found "
        "with the current filters."
    )

    st.stop()


# =========================
# MARKET SELECTION
# =========================

market_by_id = {
    market.id: market
    for market in markets
}

market_labels = {
    market.id: market.question
    for market in markets
}

selected_market_id = st.selectbox(
    "Choose a Manifold market",
    options=list(market_by_id.keys()),
    format_func=lambda market_id: market_labels[market_id]
)

market = market_by_id[
    selected_market_id
]


# Reset analysis when changing market

if (
    st.session_state.last_market_id
    != market.id
):

    st.session_state.analysis = None

    st.session_state.last_market_id = (
        market.id
    )


st.session_state.selected_market = market


# =========================
# MARKET INFO
# =========================

st.subheader(
    market.question
)

col1, col2, col3 = st.columns(3)

col1.metric(
    "Manifold probability",
    f"{market.probability:.1%}"
)

col2.metric(
    "24h volume",
    (
        f"{market.volume:.0f} Mana"
        if market.volume is not None
        else "N/A"
    )
)

col3.metric(
    "Token",
    market.token
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
        "Open market on Manifold",
        market.url
    )


# =========================
# ANALYZE BUTTON
# =========================

if st.button(
    "Analyze with EdgeX",
    type="primary"
):

    with st.spinner(
        "EdgeX is researching and challenging its forecast..."
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
# ANALYSIS RESULTS
# =========================

analysis = st.session_state.analysis


if analysis is not None:

    st.divider()

    st.header(
        "EdgeX Analysis"
    )


    # =====================
    # METRICS
    # =====================

    c1, c2, c3, c4 = (
        st.columns(4)
    )

    c1.metric(
        "Market",
        f"{analysis.market_probability:.1%}"
    )

    c2.metric(
        "Initial forecast",
        f"{analysis.initial_probability:.1%}"
    )

    c3.metric(
        "After Critic",
        f"{analysis.final_probability:.1%}",
        delta=(
            f"{analysis.final_probability - analysis.initial_probability:+.1%}"
        )
    )

    c4.metric(
        "Edge",
        f"{analysis.edge:+.1%}"
    )


    c1, c2 = st.columns(2)

    c1.metric(
        "Confidence",
        analysis.confidence.upper()
    )

    c2.metric(
        "Decision",
        analysis.decision
    )


    # =====================
    # DECISION SUMMARY
    # =====================

    if analysis.decision == "BUY_YES":

        st.success(
            "EdgeX decision: BUY YES"
        )

    elif analysis.decision == "BUY_NO":

        st.warning(
            "EdgeX decision: BUY NO"
        )

    else:

        st.info(
            "EdgeX decision: SKIP"
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
            for evidence in analysis.evidence
            if evidence.source_agent
            == "researcher"
        ]

        if not researcher_evidence:

            st.info(
                "No researcher evidence."
            )

        for evidence in researcher_evidence:

            prefix = (
                "FOR"
                if evidence.supports
                else "AGAINST"
            )

            st.markdown(
                f"**{prefix} — "
                f"{evidence.title}**"
            )

            st.write(
                evidence.summary
            )

            if evidence.url:

                st.link_button(
                    f"Source: {evidence.title}",
                    evidence.url,
                    key=(
                        f"researcher_"
                        f"{market.id}_"
                        f"{hash(evidence.url)}"
                    )
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
            for evidence in analysis.evidence
            if evidence.source_agent
            == "critic"
        ]

        if not critic_evidence:

            st.info(
                "No critic evidence."
            )

        for evidence in critic_evidence:

            prefix = (
                "FOR"
                if evidence.supports
                else "AGAINST"
            )

            st.markdown(
                f"**{prefix} — "
                f"{evidence.title}**"
            )

            st.write(
                evidence.summary
            )

            if evidence.url:

                st.link_button(
                    f"Source: {evidence.title}",
                    evidence.url,
                    key=(
                        f"critic_"
                        f"{market.id}_"
                        f"{hash(evidence.url)}"
                    )
                )

            st.divider()


    # =====================
    # FINAL REASONING
    # =====================

    st.subheader(
        "Final reasoning"
    )

    st.write(
        analysis.reasoning
    )


    # =====================
    # RAW DEBUG
    # =====================

    with st.expander(
        "Debug / structured output"
    ):

        st.json(
            analysis.model_dump()
        )
