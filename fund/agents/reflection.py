"""
Reflection Agent.
Runs after each trade (or passed opportunity) to write a short lesson the
Manager reads on the next cycle for the same asset.
"""
from crewai import Agent, LLM
from fund.config import settings


def build_reflection_agent() -> Agent:
    llm = LLM(model=settings.reflection_model, api_key=settings.anthropic_api_key)
    return Agent(
        role="Reflection Agent",
        goal=(
            "Given a recent decision and its immediate outcome, write one concise "
            "lesson the Manager can use on similar future signals. "
            "Fill the ReflectionNote schema exactly."
        ),
        backstory=(
            "You review outcomes dispassionately. You label each decision as win, "
            "loss, breakeven, or pending. You write a single sentence that captures "
            "the essential lesson — not generic advice, but something specific to "
            "what the data actually showed."
        ),
        tools=[],   # reads from context only — no tool calls
        llm=llm,
        verbose=False,
        allow_delegation=False,
        max_iter=2,
    )
