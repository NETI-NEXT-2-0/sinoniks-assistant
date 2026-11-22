"""Late usage never becomes a second attempt or silently replaces known billing facts."""

import json
from uuid import uuid4

import pytest

from partner_agent.runtime.accounting_math import priced_usage, usage_record
from partner_agent.runtime.usage_reconciliation import (
    MAX_IMPORT_BYTES,
    UsageReconciliationError,
    load_import,
    reconciled_usage,
    reconciliation_receipt,
)


def test_reconciliation_adds_missing_fields_without_charging_cache_or_reasoning_twice():
    initial = usage_record({"input_tokens": 10000})
    updated = reconciled_usage(initial, {
        "input_tokens": 10000, "output_tokens": 2000, "cost": .002,
        "input_tokens_details": {"cached_tokens": 7000},
        "output_tokens_details": {"reasoning_tokens": 1000},
        "private_text": "must not persist",
    })
    assert priced_usage(updated, "0.1", "0.4")["estimated_cost_usd"] == "0.0018"
    assert updated["provider_cost_usd"] == "0.002"
    assert "private_text" not in updated["raw"]
    assert reconciled_usage(updated, {"input_tokens": 10000, "cost": .002}) == updated


@pytest.mark.parametrize("raw", [
    {}, {"input_tokens": -1}, {"input_tokens": True}, {"input_tokens": 1.5},
    {"input_tokens": 2, "prompt_tokens": 3}, {"cost": float("nan")},
    {"cost": float("inf")}, {"output_tokens": "20"},
])
def test_invalid_or_ambiguous_values_cannot_reconcile(raw):
    with pytest.raises(UsageReconciliationError):
        reconciled_usage({}, raw)


@pytest.mark.parametrize("raw", [
    {"input_tokens": 11}, {"prompt_tokens": 11}, {"cost": .003},
    {"input_tokens": 10, "input_tokens_details": {"cached_tokens": 7}},
])
def test_known_values_cannot_be_rewritten(raw):
    previous = usage_record({"input_tokens": 10, "cost": .002,
                             "input_tokens_details": {"cached_tokens": 5}})
    with pytest.raises(UsageReconciliationError, match="usage_conflict"):
        reconciled_usage(previous, raw)


def test_import_limit_duplicate_keys_and_duplicate_calls(tmp_path):
    path = tmp_path / "private.json"
    path.write_bytes(b" " * (MAX_IMPORT_BYTES + 1))
    with pytest.raises(UsageReconciliationError, match="import_too_large"):
        load_import(path)
    path.write_text('{"source":"provider_export","source":"provider_export"}')
    with pytest.raises(UsageReconciliationError):
        load_import(path)
    row = {"call_id": str(uuid4()), "response_id": "synthetic", "model_id": "synthetic",
           "usage": {"input_tokens": 10}}
    path.write_text(json.dumps({"schema_version": 1, "source": "provider_export",
                                "calls": [row, row]}))
    with pytest.raises(UsageReconciliationError, match="duplicate_call_id"):
        load_import(path)


def test_receipts_preserve_all_previous_imports_and_enforce_a_bound():
    previous = {}
    for index in range(20):
        receipt = reconciliation_receipt(previous, f"{index:064x}")
        assert len(receipt["history"]) == index
        if index:
            assert receipt["history"][0]["import_sha256"] == "0" * 64
        previous = {"reconciliation": receipt}
    with pytest.raises(UsageReconciliationError, match="reconciliation_history_limit"):
        reconciliation_receipt(previous, "f" * 64)
