"""
Research Analyst Agent
Hired by the Investment Manager on price momentum signals.
Returns a structured BUY / HOLD / SELL verdict with confidence score.
"""
import os
from crewai import Agent, LLM
from fund.tools.market import get_price_bars, calculate_indicators


def build_research_analyst() -> Agent:
    llm = LLM(
        model=os.getenv("SPECIALIST_MODEL", "anthropic/claude-haiku-4-5-20251001"),
        api_key=os.getenv("ANTHROPIC_API_KEY", ""),
    )

    return Agent(
        role="Research Analyst",
        goal=(
            "Analyse price data and technical indicators for a specific asset. "
            "Return a clear, structured BUY / HOLD / SELL verdict with a confidence score. "
            "Never guess — only trade what the data shows."
        ),
        backstory=(
            "You are a quantitative Research Analyst at an autonomous investment fund. "
            "Your job is to evaluate a single asset when the Investment Manager suspects a "
            "trading opportunity. You use price bars and technical indicators (RSI, moving "
            "averages, momentum) to form a conviction score. "
            "You are disciplined and concise: you always return your output in exactly the "
            "structured format you are asked for — VERDICT, CONFIDENCE, REASON — because "
            "the Manager parses your output programmatically. "
            "You are hired for one task and dismissed immediately after. "
            "Your analysis covers 30–60 bars of price history."
        ),
        tools=[get_price_bars, calculate_indicators],
        llm=llm,
        verbose=True,
        allow_delegation=False,
        max_iter=4,
    )
