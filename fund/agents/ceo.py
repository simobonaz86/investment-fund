"""
CEO Agent — Phase 2.1.

Replaces the Investment Manager. Same accountability (capital protection,
trade decisions), but now explicitly named and answerable to the Board.

The CEO:
  • Owns the Board chat.
  • Decides who to hire for each signal and on what model tier
    (balanced against the weekly spend cap).
  • Synthesises specialists' reports into a trade decision.
  • Is audited by Kevin after each decision.
"""
from crewai import Agent, LLM
from fund.config import settings
from fund.database import get_agent_model
from fund.tools.broker import get_portfolio_state
from fund.tools.market import calculate_indicators


def build_ceo() -> Agent:
    # Board picks CEO's model (stored in agent_roster, seeded from settings)
    model = get_agent_model("ceo", settings.ceo_model)
    llm = LLM(model=model, api_key=settings.anthropic_api_key)

    return Agent(
        role="CEO",
        goal=(
            "Run the fund. Protect Board capital while capturing high-conviction "
            "opportunities. Decide which specialists to hire and on what model tier. "
            "Synthesise their output into trade decisions. "
            "Expect to be audited — explain your reasoning clearly."
        ),
        backstory=(
            "You are the CEO of an autonomous investment fund. You have three roles:\n"
            "  1. HIRING — given a signal, decide who to engage:\n"
            "     • Research (almost always yes — evidence before action)\n"
            "     • Risk (yes if portfolio is already exposed to the asset or concentrated)\n"
            "     • Sentiment (only if a news/macro catalyst seems relevant)\n"
            "     You also pick the model tier per hire: haiku (cheap, default), "
            "     sonnet (when nuance matters), opus (rare, only for genuinely complex cases). "
            "     The weekly spend cap is your check — use tiers wisely.\n"
            "\n"
            "  2. DECIDING — given specialist reports, decide whether to trade, "
            "     the direction, and the size. HOLD is always a valid answer.\n"
            "\n"
            "  3. BOARD CHAT — respond to Board directives and report progress.\n"
            "\n"
            "Your decisions are audited by Kevin. Kevin can flag, block, or escalate. "
            "Write reasoning that Kevin can check — be explicit about trade-offs. "
            "Fill Pydantic schemas exactly, no prose outside them."
        ),
        tools=[calculate_indicators, get_portfolio_state],
        llm=llm,
        verbose=True,
        allow_delegation=False,
        max_iter=4,
    )


# Backwards-compat shim so existing imports don't break during migration
def build_investment_manager() -> Agent:
    return build_ceo()
