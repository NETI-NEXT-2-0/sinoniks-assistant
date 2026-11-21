"""Contest tariff arithmetic, unknowns and safe provider metadata."""

import pytest
from pydantic import ValidationError

from partner_agent.accounting_routes import summarize
from partner_agent.infra.settings import Settings
from partner_agent.runtime.accounting import tariff_snapshot
from partner_agent.runtime.accounting_math import priced_usage, usage_record
from partner_agent.runtime.provider import reservation_cost_usd


def test_case_example_cache_and_reasoning_are_not_double_charged():
    usage = usage_record({"input_tokens": 10000, "output_tokens": 2000,
                          "input_tokens_details": {"cached_tokens": 7000},
                          "output_tokens_details": {"reasoning_tokens": 1000}, "cost": 0.002})
    price = priced_usage(usage, "0.10", "0.40")
    assert price["estimated_cost_usd"] == "0.0018"
    assert usage["provider_cost_usd"] == "0.002"
    assert usage["reasoning_tokens"] == 1000
    assert usage["cached_tokens"] == 7000


def test_missing_conflicting_or_invalid_usage_is_unknown():
    for raw in (None, {}, {"input_tokens": -1}, {"input_tokens": True},
                {"input_tokens": 1, "prompt_tokens": 2}):
        assert priced_usage(usage_record(raw), "0.1", "0.4")["estimated_cost_usd"] is None
    assert priced_usage(usage_record({"input_tokens": 1, "output_tokens": 1}),
                        None, None)["estimated_cost_usd"] is None


def test_undeclared_zero_tariff_is_not_free():
    settings = Settings(_env_file=None, database_url="postgresql+psycopg://u:p@localhost/db")
    assert tariff_snapshot(settings)["input_usd_per_million"] is None
    settings.model_pricing_declared = True
    assert tariff_snapshot(settings)["input_usd_per_million"] == "0.0"


def test_micro_cost_and_unknown_attempt_cannot_disappear_from_total():
    usage = usage_record({"input_tokens": 1, "output_tokens": 1})
    costs = priced_usage(usage, "0.018", "0.09")
    assert costs["estimated_cost_usd"] == "1.08E-7"
    call = {**costs, "usage": usage, "status": "failed", "model_id": "synthetic"}
    assert summarize([call, call])["estimated_cost_usd"] == "2.16E-7"
    unknown = {**call, "estimated_cost_usd": None}
    assert summarize([call, unknown])["estimated_cost_usd"] is None
    assert summarize([call, unknown])["unknown_cost_calls"] == 1
    assert summarize([])["estimated_cost_usd"] == "0"


def test_usage_retains_numeric_details_without_arbitrary_text():
    usage = usage_record({"prompt_tokens": 42, "cost": float("nan"),
                          "private_answer": "do not store", "details": {"reasoning": 2}})
    assert "private_answer" not in usage["raw"]
    assert "cost" not in usage["raw"]
    assert usage["raw"]["details"]["reasoning"] == 2


@pytest.mark.parametrize("price", [-1, float("inf"), float("nan")])
def test_tariffs_must_be_finite_nonnegative(price):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="postgresql+psycopg://u:p@localhost/db",
                 model_input_usd_per_million=price)


def test_contest_mode_fails_closed_on_missing_or_expensive_declaration():
    values = dict(database_url="postgresql+psycopg://u:p@localhost/db",
                  runtime_profile="openai_compatible", model_endpoint="https://test.invalid/v1",
                  model_api_key="synthetic", model_name="synthetic", model_contest_mode=True)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)
    values.update(model_catalog_id="synthetic/catalog", model_provider_variant="synthetic/route",
                  model_price_snapshot="synthetic-date", model_input_usd_per_million=0.1)
    assert Settings(_env_file=None, **values).model_contest_mode is True
    capped = values | {"model_request_cap_usd": 0.025, "model_output_usd_per_million": 0.30,
                       "model_input_usd_per_million": 0.09}
    with pytest.raises(ValidationError, match="declared route context window"):
        Settings(_env_file=None, **capped)
    route = Settings(_env_file=None, **capped, model_context_window_tokens=262144)
    assert reservation_cost_usd(route) == 0.024493
    values["model_input_usd_per_million"] = 0.100001
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)
