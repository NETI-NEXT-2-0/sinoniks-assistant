"""Read-only catalog evidence. Never selects a runtime route or sends model requests."""

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

CATALOG_URL = "https://openrouter.ai/api/v1/models?output_modalities=all"
MAX_BYTES = 8_000_000
INPUT_CAP = Decimal("0.10")
MILLION = Decimal(1_000_000)


class Route(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    route_id: str = Field(min_length=1, max_length=100)
    catalog_id: str = Field(min_length=1, max_length=200)
    served_model: str = Field(min_length=1, max_length=200)
    provider_variant: str = Field(min_length=1, max_length=200)
    purposes: list[Literal["chat", "extraction", "indexing", "embedding", "auxiliary"]] = Field(
        min_length=1, max_length=5
    )
    required_context: int = Field(ge=1, le=10_000_000)
    require_structured_outputs: bool = False


class Inventory(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1]
    all_routes_declared: bool
    routes: list[Route] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_routes(self) -> "Inventory":
        if len({route.route_id for route in self.routes}) != len(self.routes):
            raise ValueError("Duplicate route_id")
        return self


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def read_json(raw: bytes) -> Any:
    if len(raw) > MAX_BYTES:
        raise ValueError("JSON exceeds size limit")

    def reject_constant(value: str) -> None:
        raise ValueError("Non-finite JSON number")

    return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=reject_constant)


def read_file(path: Path) -> bytes:
    with path.open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("File exceeds size limit")
    return raw


def fetch_catalog() -> bytes:
    # No key/environment/configuration is read. Reject redirects to other endpoints.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: Any, **kwargs: Any) -> None:
            return None

    opener = urllib.request.build_opener(NoRedirect())
    request = urllib.request.Request(CATALOG_URL, headers={"Accept": "application/json"})
    with opener.open(request, timeout=25) as response:
        raw = response.read(MAX_BYTES + 1)
    if not isinstance(raw, bytes):
        raise ValueError("Expected byte response")
    if len(raw) > MAX_BYTES:
        raise ValueError("Catalog exceeds size limit")
    return raw


def _price(value: Any) -> Decimal:
    # Official API prices are decimal strings in USD per token. Missing != zero.
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError("Price must be a decimal string")
    try:
        price = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("Invalid price") from error
    exponent = price.as_tuple().exponent
    if (
        not price.is_finite()
        or price < 0
        or price > 1
        or not isinstance(exponent, int)
        or exponent < -30
        or exponent > 6
        or len(price.as_tuple().digits) > 24
    ):
        raise ValueError("Unsupported price")
    return price


def _check_route(route: Route, entry: dict[str, Any] | None) -> dict[str, Any]:
    issues: list[str] = []
    result: dict[str, Any] = {**route.model_dump(), "issues": issues, "catalog": None}
    if entry is None:
        issues.append("catalog_id_not_found")
        result["catalog_checks_passed"] = False
        return result
    pricing = entry.get("pricing")
    pricing = pricing if isinstance(pricing, dict) else {}
    result["catalog"] = {
        key: entry.get(key)
        for key in ("id", "context_length", "architecture", "supported_parameters", "pricing")
    }
    try:
        prompt, completion = _price(pricing.get("prompt")), _price(pricing.get("completion"))
        result["input_usd_per_million"] = str(prompt * MILLION)
        result["output_usd_per_million"] = str(completion * MILLION)
        if prompt * MILLION > INPUT_CAP:
            issues.append("input_price_above_cap")
    except ValueError:
        issues.append("missing_or_invalid_token_price")
    # Conditional/provider pricing is not proven by the cheapest default listing.
    if pricing.get("overrides") or entry.get("pricing_overrides"):
        issues.append("conditional_pricing_requires_review")
    for key, value in pricing.items():
        if key in {"prompt", "completion", "overrides"}:
            continue
        if key not in {
            "input_cache_read",
            "input_cache_write",
            "request",
            "image",
            "web_search",
            "internal_reasoning",
            "audio",
            "input_audio_cache",
            "image_output",
        }:
            issues.append("unknown_pricing_field_requires_review")
            continue
        try:
            extra = _price(value)
            if extra and key not in {"input_cache_read", "input_cache_write"}:
                issues.append("additional_charges_require_review")
            if key in {"input_cache_read", "input_cache_write"} and extra * MILLION > INPUT_CAP:
                issues.append("cache_input_price_above_cap")
        except ValueError:
            issues.append("unknown_pricing_field_requires_review")
    if route.catalog_id.startswith("openrouter/"):
        issues.append("dynamic_router_not_a_fixed_model")
    context = entry.get("context_length")
    if type(context) is not int or context < route.required_context:
        issues.append("insufficient_or_unknown_context")
    parameters = entry.get("supported_parameters")
    if route.require_structured_outputs and (
        not isinstance(parameters, list) or "structured_outputs" not in parameters
    ):
        issues.append("structured_outputs_not_advertised")
    architecture = entry.get("architecture")
    inputs = architecture.get("input_modalities") if isinstance(architecture, dict) else None
    outputs = architecture.get("output_modalities") if isinstance(architecture, dict) else None
    if not isinstance(inputs, list) or "text" not in inputs:
        issues.append("text_input_not_advertised")
    purposes = set(route.purposes)
    required_outputs = {"text"}
    if "embedding" in purposes:
        required_outputs = {"embeddings"}
        if purposes - {"embedding", "indexing"}:
            required_outputs.add("text")
    if not isinstance(outputs, list) or not all(output in outputs for output in required_outputs):
        issues.append("required_output_not_advertised")
    result["catalog_checks_passed"] = not issues
    return result


def audit(raw: bytes, inventory: Inventory, *, fetched: bool = False) -> dict[str, Any]:
    catalog = read_json(raw)
    entries = catalog.get("data") if isinstance(catalog, dict) else None
    if not isinstance(entries, list) or len(entries) > 10_000:
        raise ValueError("Expected bounded catalog data array")
    indexed: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise ValueError("Invalid catalog entry")
        identifier = entry["id"]
        if identifier in indexed:
            raise ValueError("Duplicate catalog ID")
        indexed[identifier] = entry
    routes = [_check_route(route, indexed.get(route.catalog_id)) for route in inventory.routes]
    now = datetime.now(UTC).isoformat()
    return {
        "schema_version": 1,
        "checked_at": now,
        "retrieved_at": now if fetched else None,
        "catalog_source": CATALOG_URL if fetched else "operator_supplied_file",
        "catalog_sha256": hashlib.sha256(raw).hexdigest(),
        "inventory_sha256": hashlib.sha256(inventory.model_dump_json().encode()).hexdigest(),
        "input_cap_usd_per_million": str(INPUT_CAP),
        "all_routes_declared": inventory.all_routes_declared,
        "all_catalog_checks_passed": inventory.all_routes_declared
        and all(route["catalog_checks_passed"] for route in routes),
        "gateway_identity_verified": False,
        "provider_variant_price_verified": False,
        "model_quality_verified": False,
        "routes": routes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--catalog", type=Path)
    source.add_argument("--fetch", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        inventory = Inventory.model_validate(read_json(read_file(args.inventory)))
        # Check before GET; final exclusive create also protects against races.
        if args.output.exists():
            raise ValueError("Output already exists")
        raw = fetch_catalog() if args.fetch else read_file(args.catalog)
        report = audit(raw, inventory, fetched=args.fetch)
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
    except (OSError, ValueError, ValidationError, urllib.error.URLError, RecursionError):
        # Do not echo arbitrary manifest contents, paths, credentials or remote body.
        print("Catalog audit failed: check input, network and output path.", file=sys.stderr)
        return 2
    print("Catalog checks passed." if report["all_catalog_checks_passed"] else "Needs review.")
    return 0 if report["all_catalog_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
