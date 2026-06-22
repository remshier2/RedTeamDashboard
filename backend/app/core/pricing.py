"""Token pricing for the Costs tab roll-up.

Maps an LLM model name to an approximate USD rate per 1,000,000 tokens, split
into ``input`` (prompt) and ``output`` (completion). The orchestrator agents
record ``model_provider`` / ``model_name`` / ``tokens_in`` / ``tokens_out`` on
every ``AgentExecution`` but never a dollar figure, so cost is derived here at
roll-up time. Computing on read (rather than persisting ``cost_usd``) means a
rate change is picked up immediately with no backfill.

These are **editable defaults** — verify against your provider's current
published pricing and adjust ``_RATE_TABLE``. Matching is case-insensitive
substring on the model name, most-specific pattern first, so
``claude-3-5-haiku-...`` matches the Haiku rate before the generic ``claude``.
A model with no matching pattern is treated as *unpriced*: its tokens still
count, but it contributes $0 and the roll-up surfaces it so the UI can flag it.
Local providers (Ollama and similar) are $0.
"""
from __future__ import annotations

from decimal import Decimal

# (substring pattern, input_per_1M, output_per_1M) in USD. Ordered most-specific
# first; the first substring match on the lowercased model name wins.
_RATE_TABLE: list[tuple[str, str, str]] = [
    # Anthropic Claude
    ("claude-3-5-haiku", "0.80", "4"),
    ("claude-3-haiku", "0.25", "1.25"),
    ("claude-haiku", "0.80", "4"),
    ("claude-3-opus", "15", "75"),
    ("claude-opus", "15", "75"),
    ("claude-3-5-sonnet", "3", "15"),
    ("claude-3-7-sonnet", "3", "15"),
    ("claude-sonnet", "3", "15"),
    # OpenAI
    ("gpt-4o-mini", "0.15", "0.60"),
    ("gpt-4o", "2.50", "10"),
    ("gpt-4-turbo", "10", "30"),
    ("gpt-4", "30", "60"),
    ("gpt-3.5", "0.50", "1.50"),
    ("o1-mini", "1.10", "4.40"),
    ("o1", "15", "60"),
    # local / self-hosted — free
    ("ollama", "0", "0"),
    ("llama", "0", "0"),
    ("mistral", "0", "0"),
    ("qwen", "0", "0"),
]

_PER_TOKEN = Decimal(1_000_000)

# Providers whose models always run locally → no spend, regardless of model name.
_FREE_PROVIDERS = {"ollama"}


def rate_for(
    model_name: str | None, provider: str | None = None
) -> tuple[Decimal, Decimal] | None:
    """Return ``(input_per_1M, output_per_1M)`` USD rates, or ``None`` if the
    model is unpriced. A free/local provider returns ``(0, 0)``."""
    if provider and provider.lower() in _FREE_PROVIDERS:
        return Decimal(0), Decimal(0)
    if not model_name:
        return None
    needle = model_name.lower()
    for pattern, rate_in, rate_out in _RATE_TABLE:
        if pattern in needle:
            return Decimal(rate_in), Decimal(rate_out)
    return None


def cost_usd(
    model_name: str | None,
    tokens_in: int | None,
    tokens_out: int | None,
    provider: str | None = None,
) -> Decimal | None:
    """USD cost of one call, or ``None`` if the model is unpriced (caller should
    treat unpriced as $0 in totals but flag the model)."""
    rates = rate_for(model_name, provider)
    if rates is None:
        return None
    rate_in, rate_out = rates
    used_in = Decimal(tokens_in or 0)
    used_out = Decimal(tokens_out or 0)
    return (used_in * rate_in + used_out * rate_out) / _PER_TOKEN
