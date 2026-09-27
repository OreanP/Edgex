import os

from dotenv import load_dotenv
from openai import OpenAI

from agent.schemas import Market, AgentAnalysis, Forecast
from agent.prompts import SYSTEM_PROMPT
from agent.critic import critique_forecast

from core.budget import(
    check_daily_budget,
    register_analysis,
    BudgetExceeded
)

ENABLE_CRITIC = (os.getenv("ENABLE_CRITIC","true").lower()=="true")

load_dotenv()

client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY")
)

OPENAI_MODEL = os.getenv("OPENAI_MODEL","gpt-5.6-luna")

MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS","600"))

MAX_WEB_SEARCHES_PER_CALL = int(os.getenv("MAX_WEB_SEARCHES_PER_CALL","1"))




class AgentError(Exception):
    """
    Error raised when EdgeX cannot complete
    a market analysis.
    """
    pass

def analyse_market(market: Market) -> AgentAnalysis:
    """
    Analyse a market and return the structured analysis of the agent
    """


    check_daily_budget()


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

    try:

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

    except Exception as e:
        raise AgentError(
            f"Researcher failed {e}"
        )from e

    register_analysis()


    if ENABLE_CRITIC:
        critic = critique_forecast(
            market= market,
            forecast=forecast
        )
    else:
        critic_probability = forecast.probability

    edge = (critic.revised_probability - market.probability)

    if abs(edge)<0.08:
        decision= "SKIP"

    elif edge > 0:
        decision = "BUY_YES"

    else:
        decision = "BUY_NO"

    return AgentAnalysis(
        market_probability= market.probability,
        initial_probability= forecast.probability,
        final_probability= critic.revised_probability,
        edge= edge,
        confidence= critic.confidence, 
        evidence= (forecast.evidence+ critic.counter_evidence),
        decision= decision,
        reasoning= critic.reasoning
    )

class AgentError(Exception):
    pass