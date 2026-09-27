import os

from dotenv import load_dotenv
from openai import OpenAI

from agent.schemas import(
    Market,
    Forecast,
    CriticResult
)
from agent.prompts import CRITIC_PROMPT

from core.budget import(
    check_daily_budget,
    register_analysis,
    BudgetExceeded
)

load_dotenv()

client = OpenAI(api_key= os.getenv("OPENAI_API_KEY"))

OPENAI_MODEL = os.getenv("OPENAI_MODEL","gpt-5.6-luna")

MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS","600" ))

MAX_WEB_SEARCHES_PER_CALL = int(os.getenv("MAX_WEB_SEARCHES_PER_CALL","1"))




def critique_forecast(
        market: Market,
        forecast: Forecast
) -> CriticResult:

    if not 0 <= market.probability <= 1:
        raise ValueError(
            "Market probability must be between 0 and 1."
        )

    if not 0 <= forecast.probability <= 1:
        raise ValueError(
            "Forecast probability must be between 0 and 1."
        )
    
    
    
    check_daily_budget()


    if forecast.evidence:

        evidence_text = "\n\n".join(
            [
                (
                    f"Title: {e.title}\n"
                    f"Summary: {e.summary}\n"
                    f"Supports YES: {e.supports}\n"
                    f"Source: {e.url}"
                )
                for e in forecast.evidence
            ]
        )

    else:

        evidence_text = (
            "No structured evidence was returned "
            "by the researcher."
        )

    user_prompt = f"""
    Prediction market:

    Question:
    {market.question}

    Description:
    {market.description}

    Current market probability:
    {market.probability:.2%}

    Initial EdgeX forecast:
    {forecast.probability:.2%}

    Initial confidence
    {forecast.confidence}

    Initial reasoning
    {forecast.reasoning}

    Evidence already collected:
    {evidence_text}

    Challenge this forecast

    Search specifically for information that could make the initial estimate wrong

    Then return a revised probability

    """
    try:
        response = client.responses.parse(
            model="gpt-5.6",
            input=[
                {
                    "role":"system",
                    "content": CRITIC_PROMPT
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
            max_tool_calls=MAX_WEB_SEARCHES_PER_CALL,
            max_output_tokens= MAX_OUTPUT_TOKENS,
            text_format= CriticResult
        )

        result = response.output_parsed
    except BudgetExceeded:
        raise
    except Exception as e:

        raise RuntimeError(
            f"Critic OpenAI call failed: {e}"
        ) from e

    if result is None:

        raise RuntimeError(
            "Critic returned no structured result."
        )


    if not (0<= result.revised_probability<= 1):

        raise RuntimeError(
            "Critic returned an invalid probability"
        )

    register_analysis()


    return result

                