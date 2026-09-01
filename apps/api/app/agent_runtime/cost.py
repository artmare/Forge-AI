from decimal import Decimal

from app.agent_runtime.contracts import ModelUsage


class CostEstimator:
    """Estimates token cost from configuration; unknown pricing remains unknown."""

    def __init__(self, pricing: dict[str, dict[str, float]]) -> None:
        self.pricing = pricing

    def estimate(self, provider: str, model: str, usage: ModelUsage) -> Decimal | None:
        if provider in {"mock", "deterministic"}:
            return Decimal("0")
        rates = self.pricing.get(f"{provider}:{model}") or self.pricing.get(model)
        if rates is None:
            return None
        million = Decimal("1000000")
        input_rate = Decimal(str(rates.get("input", 0)))
        cached_rate = Decimal(str(rates.get("cached_input", rates.get("input", 0))))
        output_rate = Decimal(str(rates.get("output", 0)))
        uncached = max(usage.input_tokens - usage.cached_input_tokens, 0)
        return (
            Decimal(uncached) * input_rate
            + Decimal(usage.cached_input_tokens) * cached_rate
            + Decimal(usage.output_tokens) * output_rate
        ) / million
