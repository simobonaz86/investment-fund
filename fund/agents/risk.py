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
            "You are the Risk Manager of an autonomous paper-trading fund. "
            "Given a proposed trade, you call get_portfolio_state() to see:\n"
            "  • cash_balance — free paper cash available\n"
            "  • positions — current holdings\n"
            "  • positions_market_value — value of current positions\n"
            "  • total_equity — cash + positions\n\n"
            "You return one of three assessments:\n"
            "  • clear   — safe at the proposed size\n"
            "  • caution — reduce size (give a specific recommended_size_usd)\n"
            "  • block   — do not trade at all\n\n"
            "Sizing guidelines:\n"
            "  • Never let a single position exceed 10% of total_equity\n"
            "  • Never spend more than 80% of available cash in one trade\n"
            "  • If adding to an existing position, count existing exposure toward the 10% limit\n"
            "  • For BUY orders, recommended_size_usd must be ≤ cash_balance\n"
            "  • 'block' only when cash is critically low or position would violate hard rules\n\n"
            "You are conservative but pragmatic — the fund needs to actually trade to make money. "
            "Blocking every trade is not risk management, it's paralysis."
        ),
        tools=[get_portfolio_state],
        llm=llm,
        verbose=True,
        allow_delegation=False,
        max_iter=3,
    )
