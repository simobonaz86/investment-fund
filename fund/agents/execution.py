"""Execution Agent."""
from crewai import Agent, LLM
from fund.config import settings
from fund.tools.broker import place_paper_order, get_portfolio_state


def build_execution_agent() -> Agent:
    llm = LLM(model=settings.execution_model, api_key=settings.anthropic_api_key)
    return Agent(
        role="Execution Agent",
        goal=(
            "Execute the approved trade cleanly and report the fill in the "
            "ExecutionResult schema. Never exceed the approved size."
        ),
        backstory=(
            "You are a trade execution specialist. You receive a pre-approved trade "
            "from the Investment Manager and execute it via the paper broker. "
            "You never question the decision — your only job is clean execution. "
            "Before placing the order you verify portfolio state. You return the "
            "fill in the ExecutionResult schema exactly."
        ),
        tools=[place_paper_order, get_portfolio_state],
        llm=llm,
        verbose=True,
        allow_delegation=False,
        max_iter=3,
    )
