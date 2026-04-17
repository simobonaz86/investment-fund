"""Risk Manager Agent — portfolio exposure and sizing."""
from crewai import Agent, LLM
from fund.config import settings
from fund.tools.broker import get_portfolio_state


def build_risk_manager() -> Agent:
    llm = LLM(model=settings.risk_model, api_key=settings.anthropic_api_key)
    return Agent(
        role="Risk Manager",
        goal=(
            "Assess portfolio exposure and propose a safe trade size. "
            "Fill the RiskReport schema exactly."
        ),
        backstory=(
            "You are the Risk Manager. Given a proposed trade, you check current "
            "portfolio state, concentration, and existing exposure to the asset. "
            "You return one of three assessments: clear (safe to proceed at proposed "
            "size), caution (reduce size), or block (do not trade). You are "
            "conservative — when the fund is small, any single trade over 20% of "
            "portfolio value deserves caution."
        ),
        tools=[get_portfolio_state],
        llm=llm,
        verbose=True,
        allow_delegation=False,
        max_iter=3,
    )
