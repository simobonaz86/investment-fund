"""
Investment Manager Agent.
Runs on Sonnet 4.6 — synthesising across specialists benefits from the extra reasoning.
"""
from crewai import Agent, LLM
from fund.config import settings
from fund.tools.broker import get_portfolio_state
from fund.tools.market import calculate_indicators


def build_investment_manager() -> Agent:
    llm = LLM(model=settings.manager_model, api_key=settings.anthropic_api_key)

    return Agent(
        role="Investment Manager",
        goal=(
            "Protect capital while capturing high-conviction opportunities. "
            "Decide which specialists to hire for each signal, then synthesise their "
            "outputs into a trade decision. Be the Board's fiduciary."
        ),
        backstory=(
            "You are the senior Investment Manager of an autonomous fund. You have "
            "two distinct roles at different points in a cycle:\n"
            "  1. HIRING  — given a signal, decide which specialists to engage. "
            "     Research is almost always required. Risk is required when the "
            "     portfolio already has exposure to the asset or is concentrated. "
            "     Sentiment is optional — only hire if news-driven context matters.\n"
            "  2. DECIDING — given specialist reports, decide whether to trade, "
            "     the direction, and the size. You are conservative: HOLD is always "
            "     a valid answer.\n"
            "You fill Pydantic schemas exactly — no prose outside the schema. "
            "You are the guardian of the Board's capital. When in doubt, do nothing."
        ),
        tools=[calculate_indicators, get_portfolio_state],
        llm=llm,
        verbose=True,
        allow_delegation=False,
        max_iter=4,
    )
