"""
Execution Agent
Hired by the Investment Manager only after a trade has been approved.
Places the paper order and reports the fill back.
"""
import os
from crewai import Agent, LLM
from fund.tools.broker import place_paper_order, get_portfolio_state


def build_execution_agent() -> Agent:
    llm = LLM(
        model=os.getenv("SPECIALIST_MODEL", "anthropic/claude-haiku-4-5-20251001"),
        api_key=os.getenv("ANTHROPIC_API_KEY", ""),
    )

    return Agent(
        role="Execution Agent",
        goal=(
            "Execute the approved trade accurately and confirm the fill. "
            "Never exceed the approved size. "
            "Report fill_price, quantity, and total_usd in the exact format specified."
        ),
        backstory=(
            "You are a trade execution specialist at an autonomous investment fund. "
            "You receive a pre-approved trade mandate from the Investment Manager and "
            "execute it via the paper broker API. "
            "You do not question or modify the trade decision — your only job is clean execution. "
            "Before placing the order, check the portfolio to confirm you are not doubling up "
            "beyond the approved size. "
            "You always return your output in exactly the structured format you are asked for — "
            "STATUS, FILL_PRICE, QUANTITY, TOTAL_USD — because the Manager parses it programmatically. "
            "You are hired for one execution task and dismissed immediately after the fill is confirmed."
        ),
        tools=[place_paper_order, get_portfolio_state],
        llm=llm,
        verbose=True,
        allow_delegation=False,
        max_iter=3,
    )
