"""Model pricing used by the `estimated_cost` evaluator.

Prices come from a config file (see `config/pricing.yaml`), never from this
code: AgentForge does not ship vendor prices, because a hardcoded price
would silently go stale and turn into an invented number. Any cost computed
from this table is an *estimate* -- adapter-reported token counts times
configured per-token rates -- and is always labeled "estimated".
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPrice:
    input_usd_per_million_tokens: float
    output_usd_per_million_tokens: float


class PricingConfigError(ValueError):
    pass


def parse_pricing(raw: object) -> dict[str, ModelPrice]:
    """Parse the `models:` mapping of a pricing config into ModelPrice rows.

    Expected shape (YAML shown):

        models:
          <model name as reported by the adapter>:
            input_usd_per_million_tokens: <number >= 0>
            output_usd_per_million_tokens: <number >= 0>
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise PricingConfigError("pricing config must be a mapping with a 'models' key")
    models = raw.get("models") or {}
    if not isinstance(models, Mapping):
        raise PricingConfigError("'models' must be a mapping of model name -> rates")

    prices: dict[str, ModelPrice] = {}
    for model, rates in models.items():
        if not isinstance(rates, Mapping):
            raise PricingConfigError(f"model '{model}': rates must be a mapping")
        try:
            price = ModelPrice(
                input_usd_per_million_tokens=float(rates["input_usd_per_million_tokens"]),
                output_usd_per_million_tokens=float(rates["output_usd_per_million_tokens"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PricingConfigError(f"model '{model}': invalid or missing rate ({exc})") from exc
        if price.input_usd_per_million_tokens < 0 or price.output_usd_per_million_tokens < 0:
            raise PricingConfigError(f"model '{model}': rates must be >= 0")
        prices[str(model)] = price
    return prices


def cost_usd(price: ModelPrice, input_tokens: int | None, output_tokens: int | None) -> float:
    """input_tokens/1e6 * input_rate + output_tokens/1e6 * output_rate (missing counts as 0). An estimate."""
    return (input_tokens or 0) / 1_000_000 * price.input_usd_per_million_tokens + (
        output_tokens or 0
    ) / 1_000_000 * price.output_usd_per_million_tokens
