import os

from dotenv import load_dotenv
from openai import OpenAI

from agent.schemas import Market, AgentAnalysis, Forecast
from agent.prompts import SYSTEM_PROMPT

load_dotenv()

client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY")
)

def analyse_market(market: Market) -> AgentAnalysis:
    """
    Analyse a market and return the structured analysis of the agent
    """

    #temporaire
    """return AgentAnalysis(
        market_probability=market.probability,
        initial_probability= 0.50,
        final_probability= 0.50,
        confidence= "low",
        evidence=[],
        decision= "SKIP",
        reasoning="Agent not implemented yet"
    )"""

    user_prompt = f"""
Prediction market:

Question:
{market.question}

Description:
{market.description}

Current market probability
{market.probability: .2%}

Independently estimate the propability that this event occurs.

    """
    response = client.responses.parse(
        model ="gpt-5.6",
        input=[
            {
                "role": "system",
                "content": SYSTEM_PROMPT
            },
            {
                "role": "user",
                "content": user_prompt
            }
        ],
        tools=[
            {
                "type": "web_search"
            }
        ],
        text_format = Forecast
    )

    forecast = response.output_parsed

    edge = forecast.probability - market.probability

    if abs(edge)<0.08:
        decision= "SKIP"

    elif edge > 0:
        decision = "BUY_YES"

    else:
        decision = "BUY_NO"

    return AgentAnalysis(
        market_probability= market.probability,
        initial_probability= forecast.probability,
        final_probability= forecast.probability,
        confidence= forecast.confidence, 
        evidence= [], 
        decision= decision,
        reasoning= forecast.reasoning
    )