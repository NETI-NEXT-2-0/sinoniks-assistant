"""Exact tariff arithmetic; missing values never become zero."""

from decimal import Decimal
from typing import Any


def numeric_usage(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return None
    if type(value) in (int, float) and value >= 0:
        return value if Decimal(str(value)).is_finite() else None
    if isinstance(value, dict):
        return {key: cleaned for key, item in list(value.items())[:100]
                if isinstance(key, str) and len(key) <= 80
                and (cleaned := numeric_usage(item, depth + 1)) is not None}
    return None


def token_count(raw: dict[str, Any], *names: str) -> int | None:
    values = [raw[name] for name in names if name in raw]
    if not values or any(type(value) is not int or value < 0 for value in values):
        return None
    return values[0] if len(set(values)) == 1 else None


def usage_record(raw: Any) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    prompt = data.get("prompt_tokens_details", data.get("input_tokens_details", {}))
    output = data.get("completion_tokens_details", data.get("output_tokens_details", {}))
    return {
        "raw": numeric_usage(data),
        "input_tokens": token_count(data, "prompt_tokens", "input_tokens"),
        "output_tokens": token_count(data, "completion_tokens", "output_tokens"),
        "total_tokens": token_count(data, "total_tokens"),
        "cached_tokens": token_count(prompt, "cached_tokens") if isinstance(prompt, dict) else None,
        "reasoning_tokens": (token_count(output, "reasoning_tokens")
                             if isinstance(output, dict) else None),
        "provider_cost_usd": (str(data["cost"]) if type(data.get("cost")) in (int, float)
                              and numeric_usage(data["cost"]) is not None else None),
    }


def priced_usage(
    usage: dict[str, Any], input_price: str | None, output_price: str | None,
) -> dict[str, str | None]:
    """Cache uses base input price; reasoning is already included in output."""
    def component(tokens: int | None, price: str | None) -> Decimal | None:
        return (Decimal(tokens) * Decimal(price) / Decimal(1_000_000)
                if tokens is not None and price is not None else None)

    incoming = component(usage.get("input_tokens"), input_price)
    outgoing = component(usage.get("output_tokens"), output_price)
    total = incoming + outgoing if incoming is not None and outgoing is not None else None
    return {"input_cost_usd": str(incoming) if incoming is not None else None,
            "output_cost_usd": str(outgoing) if outgoing is not None else None,
            "estimated_cost_usd": str(total) if total is not None else None}
