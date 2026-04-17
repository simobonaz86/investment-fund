"""Research Analyst Agent."""
from crewai import Agent, LLM
from fund.config import settings
from fund.tools.market import get_price_bars, calculate_indicators


def build_research_analyst() -> Agent:
    llm = LLM(model=settings.research_model, api_key=settings.anthropic_api_key)
    return Agent(
        role="Research Analyst",
        goal=(
            "Analyse price data and technical indicators for a specific asset, and "
            "return a structured BUY/HOLD/SELL verdict with a confidence score."
        ),
        backstory=(
            "You are a quantitative Research Analyst at an autonomous investment fund. "
            "You evaluate a single asset when the Investment Manager hires you. "
            "You use get_price_bars and calculate_indicators to form a view. "
            "You are disciplined, concise, and evidence-driven. You fill the "
            "ResearchVerdict schema exactly."
        ),
        tools=[get_price_bars, calculate_indicators],
        llm=llm,
        verbose=True,
        allow_delegation=False,
        max_iter=4,
    )
