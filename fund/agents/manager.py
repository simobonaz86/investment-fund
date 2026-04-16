"""
Investment Manager Agent
The orchestrator.  Monitors portfolio state, evaluates research verdicts,
and decides whether to hire the Execution Agent.
Never trades without Research backing at or above the confidence threshold.
"""
import os
from crewai import Agent, LLM
from fund.tools.market import calculate_indicators
from fund.tools.broker import get_portfolio_state


def build_investment_manager() -> Agent:
    llm = LLM(
        model=os.getenv("MANAGER_MODEL", "anthropic/claude-haiku-4-5-20251001"),
        api_key=os.getenv("ANTHROPIC_API_KEY", ""),
    )

    confidence_threshold = float(os.getenv("CONFIDENCE_THRESHOLD", "0.70"))
    max_position_usd     = float(os.getenv("MAX_POSITION_USD",      "1000.0"))

    return Agent(
        role="Investment Manager",
        goal=(
            "Protect the fund's capital while capturing high-conviction opportunities. "
            "Only approve trades where the Research Analyst's confidence is at or above "
            f"{confidence_threshold:.0%}. "
            f"Never risk more than ${max_position_usd:.0f} per trade. "
            "Explain every decision with a one-sentence reason."
        ),
        backstory=(
            "You are the Investment Manager of a small autonomous fund. "
            "You are the senior decision-maker: you receive research verdicts, check portfolio "
            "exposure, and decide whether to trade. "
            "Your core rule is non-negotiable: no trade without Research Analyst approval "
            f"at confidence >= {confidence_threshold:.0%}. "
            "A HOLD verdict or confidence below threshold = no trade, period. "
            "You size positions conservatively — never exceeding the per-trade cap. "
            "You always return your decision in exactly the structured format you are asked for — "
            "TRADE, DIRECTION, SIZE_USD, REASON — because the system parses it programmatically. "
            "You are the guardian of the Board's capital. When in doubt, do nothing."
        ),
        tools=[calculate_indicators, get_portfolio_state],
        llm=llm,
        verbose=True,
        allow_delegation=False,   # Manager's decision logic is in task descriptions
        max_iter=4,
    )
