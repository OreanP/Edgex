# EdgeX

**Autonomous AI research agent for prediction markets**

EdgeX is an autonomous research and forecasting agent built for the **X-IA Rise of Agents X Hackathon 2026**.

It analyzes live prediction markets from **Manifold Markets**, performs web research, challenges its own initial forecast through a dedicated critic step, measures its edge against the market, applies deterministic risk controls, and can simulate or execute trades using **Mana**, Manifold's play-money currency.

---

## Overview

Prediction markets aggregate collective beliefs into probabilities, but identifying mispriced markets requires continuously:

- discovering relevant markets,
- gathering recent information,
- comparing contradictory evidence,
- estimating an independent probability,
- deciding whether the difference with the market is meaningful,
- managing risk before acting.

EdgeX automates this workflow.

The core loop is:

```text
Manifold Market
      ↓
Market Scout
      ↓
Researcher Agent
      ↓
Web Search
      ↓
Initial Forecast
      ↓
Critic Agent
      ↓
Contradictory Research
      ↓
Revised Forecast
      ↓
Edge Computation
      ↓
Risk Engine
      ↓
SKIP / BUY YES / BUY NO
      ↓
Manifold dry-run or Mana trade
```

---

# Why EdgeX is agentic

EdgeX is not just a prediction model or a scripted automation.

The LLM:

1. receives a market question;
2. determines what information is relevant;
3. uses web search as an external tool;
4. gathers evidence for and against the event;
5. produces an initial probabilistic forecast;
6. passes the forecast to a second critic stage;
7. actively searches for information that could invalidate the initial conclusion;
8. revises its estimate;
9. returns a structured decision.

The final trade decision is then constrained by deterministic Python safety rules.

This gives EdgeX a closed decision loop:

```text
Observe
   ↓
Research
   ↓
Reason
   ↓
Critique
   ↓
Decide
   ↓
Act
```

---

# Main features

## Live Manifold market discovery

EdgeX queries live prediction markets using the Manifold API.

Markets are filtered before any LLM call in order to reduce cost and avoid irrelevant analysis.

The scout currently prioritizes:

- binary markets;
- unresolved markets;
- active markets;
- markets with sufficient recent volume;
- Mana-denominated markets only.

---

## Researcher Agent

The Researcher independently estimates the probability of a market outcome.

It uses recent web information and produces structured evidence:

```text
title
source URL
summary
supports YES / AGAINST YES
```

Example:

```text
Market probability: 39%

Researcher forecast: 52%

Evidence FOR
- Recent technical progress
- New product demonstrations

Evidence AGAINST
- Remaining engineering limitations
- Regulatory uncertainty
```

---

## Critic Agent

The Critic is designed to reduce confirmation bias.

Instead of defending the Researcher's forecast, it receives the initial analysis and is explicitly instructed to search for reasons why that forecast might be wrong.

Example:

```text
Initial forecast:
62%

Critic finds stronger counter-evidence

Revised forecast:
54%
```

The revised forecast becomes EdgeX's final probability.

---

## Edge calculation

EdgeX compares its final forecast with the current market probability.

For a YES position:

```text
edge = EdgeX probability - market probability
```

Example:

```text
Manifold:
41%

EdgeX:
57%

Edge:
+16 percentage points
```

A positive edge suggests a possible YES opportunity.

A negative edge may suggest a NO opportunity.

Small edges are ignored.

---

## Deterministic Risk Engine

The LLM does not directly control position size.

A deterministic Python risk layer can reject or limit trades based on rules such as:

- minimum edge;
- minimum confidence;
- maximum Mana per position;
- Mana-only markets;
- maximum exposure;
- invalid or ambiguous decisions.

Example:

```text
Agent:
BUY YES

Requested opportunity:
+15% edge

Risk Engine:
APPROVED

Maximum position:
10 Mana
```

or:

```text
Risk Engine:
REJECTED

Reason:
EDGE_TOO_SMALL
```

---

# Manifold execution

EdgeX supports two execution modes.

## Dry-run mode

Default mode:

```env
ALLOW_LIVE_TRADING=false
```

No Mana is spent.

The order is sent to Manifold using its dry-run mode so that the full execution path can be tested safely.

---

## Live Mana mode

Optional:

```env
ALLOW_LIVE_TRADING=true
```

EdgeX may place a real Manifold bet using Mana.

Mana is Manifold's play-money currency and is not real-money trading.

The frontend also requires explicit confirmation before live execution.

---

# Safety mechanisms

EdgeX includes several safeguards.

## Mana-only restriction

Markets not using Mana are rejected.

## Position cap

Example configuration:

```env
MAX_BET_MANA=10
```

The Risk Engine prevents larger positions.

## Minimum edge

Example:

```env
MIN_EDGE=0.08
```

An 8 percentage-point edge is required before considering a trade.

## Confidence filtering

Low-confidence forecasts are skipped.

## OpenAI usage budget

EdgeX maintains a local usage budget to prevent excessive API consumption.

Example:

```env
MAX_ANALYSES_PER_DAY=20
MAX_WEB_SEARCHES_PER_CALL=1
MAX_OUTPUT_TOKENS=600
```

## Critic toggle

The Critic can be disabled during development to reduce API usage:

```env
ENABLE_CRITIC=false
```

and enabled for full analysis:

```env
ENABLE_CRITIC=true
```

---

# Architecture

```text
                     ┌────────────────────┐
                     │      Manifold      │
                     │ prediction markets │
                     └─────────┬──────────┘
                               │
                               ▼
                     ┌────────────────────┐
                     │    Market Scout    │
                     │ deterministic      │
                     │ filtering          │
                     └─────────┬──────────┘
                               │
                               ▼
                     ┌────────────────────┐
                     │     Researcher     │
                     │       LLM          │
                     └─────────┬──────────┘
                               │
                         Web Search
                               │
                               ▼
                     Initial Forecast
                               │
                               ▼
                     ┌────────────────────┐
                     │       Critic       │
                     │       LLM          │
                     └─────────┬──────────┘
                               │
                  Contradictory Web Search
                               │
                               ▼
                     Revised Forecast
                               │
                               ▼
                     ┌────────────────────┐
                     │    Risk Engine     │
                     │ deterministic      │
                     └─────────┬──────────┘
                               │
                         APPROVE / REJECT
                               │
                               ▼
                     ┌────────────────────┐
                     │ Manifold execution │
                     │ dry-run / Mana     │
                     └────────────────────┘
```

---

# Project structure

```text
Edgex/
│
├── app.py
│
├── agent/
│   ├── __init__.py
│   ├── agent.py
│   ├── critic.py
│   ├── prompts.py
│   └── schemas.py
│
├── tools/
│   ├── __init__.py
│   └── manifold.py
│
├── core/
│   ├── __init__.py
│   ├── budget.py
│   ├── risk_adapter.py
│   └── tracker.py
│
├── data/
│   └── usage.json
│
├── demo/
│
├── tests/
│
├── test_agent.py
├── test_integration.py
│
├── requirements.txt
├── env.example
├── README.md
└── .gitignore
```

---

# Installation

## 1. Clone the repository

```bash
git clone https://github.com/OreanP/Edgex.git
cd Edgex
```

---

## 2. Create a virtual environment

Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Linux / macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

---

## 3. Install dependencies

```bash
pip install -r requirements.txt
```

---

# Environment variables

Copy:

```text
env.example
```

to:

```text
.env
```

and fill in your keys.

Example:

```env
OPENAI_API_KEY=

MANIFOLD_API_KEY=

OPENAI_MODEL=gpt-5.6-luna

ENABLE_CRITIC=true

MAX_OUTPUT_TOKENS=600

MAX_WEB_SEARCHES_PER_CALL=1

MAX_ANALYSES_PER_DAY=20

ALLOW_LIVE_TRADING=false

MAX_BET_MANA=10

MIN_EDGE=0.08
```

Never commit `.env`.

---

# Running the integration test

Run:

```bash
python test_integration.py
```

Expected flow:

```text
Fetching Manifold markets...

Selected market:
...

Manifold probability:
...

Running EdgeX...

EDGEX RESULT

Market:
...

Initial:
...

Final:
...

Edge:
...

Confidence:
...

Decision:
...
```

The output should also display evidence collected by the Researcher and Critic.

---

# Running the interface

Launch the Streamlit application:

```bash
streamlit run app.py
```

The interface allows you to:

- select a Manifold topic;
- fetch live markets;
- choose a market;
- inspect its current probability;
- run EdgeX;
- compare the Researcher and Critic forecasts;
- inspect supporting and contradicting evidence;
- inspect the final edge;
- inspect the Risk Engine decision;
- test an order using Manifold dry-run;
- optionally execute a Mana trade if live mode is enabled.

---

# Recommended demo configuration

For safe demonstrations:

```env
ALLOW_LIVE_TRADING=false
ENABLE_CRITIC=true
MAX_BET_MANA=10
MAX_WEB_SEARCHES_PER_CALL=1
MAX_OUTPUT_TOKENS=600
```

This allows the full workflow to be demonstrated without spending Mana.

---

# Example workflow

A market is currently priced at:

```text
YES = 42%
```

The Researcher searches recent information and returns:

```text
Initial forecast = 63%
Confidence = medium
```

The Critic finds important contradictory evidence:

```text
Revised forecast = 56%
```

EdgeX calculates:

```text
Edge = 56% - 42%
     = +14 percentage points
```

The agent proposes:

```text
BUY YES
```

The Risk Engine verifies the opportunity and may approve:

```text
APPROVED
Maximum amount: 10 Mana
```

The trade can then be sent to Manifold in dry-run mode.

---

# Evaluation

EdgeX can be evaluated in two different ways.

## Forecast quality

For resolved markets, predictions can be evaluated using the Brier score:

```text
Brier = (forecast probability - outcome)^2
```

This allows comparison between:

```text
EdgeX forecast
vs
Manifold market probability
```

## Trading performance

The Manifold account can also be evaluated using:

- Mana balance;
- P&L;
- number of trades;
- position history;
- resolved bets.

These metrics are useful for long-term experimentation but are not required to demonstrate the hackathon MVP.

---

# Limitations

EdgeX is an experimental hackathon project.

Current limitations include:

- LLM forecasts can be wrong;
- web sources may be incomplete or misleading;
- Manifold resolution criteria may be ambiguous;
- prediction-market prices are not perfect probabilities;
- EdgeX has not been validated as a profitable trading system;
- short hackathon testing does not demonstrate long-term forecasting superiority;
- some components remain intentionally simple in order to prioritize robustness and interpretability.

EdgeX should therefore be viewed as an experimental autonomous forecasting system rather than a guaranteed trading strategy.

---

# Built during the hackathon

EdgeX was developed during the **X-IA Rise of Agents X Hackathon 2026**.

The project focuses on:

- autonomous LLM research;
- tool use;
- probabilistic reasoning;
- adversarial self-critique;
- deterministic risk management;
- prediction markets;
- autonomous action in a play-money environment.

---

# Team

## A — Oréan ONGUENE

Responsibilities:

- OpenAI integration;
- Researcher;
- web search;
- structured forecasting;
- Critic;
- evidence generation;
- probability revision.

## B — Arthur EDZOA

Responsibilities:

- Manifold API;
- market discovery;
- market filtering;
- market parsing;
- account access;
- order execution;
- dry-run support.

## C — (Shared Work)

Responsibilities:

- Streamlit application;
- market selection;
- forecast visualization;
- evidence display;
- Risk Engine visualization;
- execution controls.

## D — Yohann BIKELE

Responsibilities:

- risk rules;
- position sizing;
- API budget safeguards;
- pipeline integration;
- tracking and testing.

Replace this section with the names of the four team members before submission.

---

# Hackathon submission

The submission includes:

- this GitHub repository;
- a short project description;
- the names of the team members;
- a demo video of no more than 2 minutes.

---

# License

Hackathon prototype.

All third-party services and APIs remain subject to their respective terms of service.
