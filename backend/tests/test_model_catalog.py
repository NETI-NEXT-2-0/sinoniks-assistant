import copy
import hashlib
import json
from contextlib import closing
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from partner_agent.model_catalog import (
    CATALOG_URL,
    Inventory,
    audit,
    fetch_catalog,
    main,
    read_json,
)


def inventory(**changes: Any) -> Inventory:
    return Inventory.model_validate(
        {
            "schema_version": 1,
            "all_routes_declared": True,
            "routes": [
                {
                    "route_id": "primary",
                    "catalog_id": "fixture/model",
                    "served_model": "operator-alias",
                    "provider_variant": "operator-declaration-only",
                    "purposes": ["chat", "extraction"],
                    "required_context": 32000,
                    "require_structured_outputs": True,
                }
            ],
            **changes,
        }
    )


def entry() -> dict[str, Any]:
    return {
        "id": "fixture/model",
        "context_length": 32000,
        "pricing": {"prompt": "0.0000001", "completion": "0.0000003"},
        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
        "supported_parameters": ["structured_outputs"],
    }


def result(model: dict[str, Any]) -> dict[str, Any]:
    return audit(json.dumps({"data": [model]}).encode(), inventory())


def test_exact_boundary_and_evidence_are_not_gateway_certification() -> None:
    raw = json.dumps({"data": [entry()]}).encode()
    report = audit(raw, inventory())
    assert report["all_catalog_checks_passed"]
    assert report["routes"][0]["input_usd_per_million"] == "0.1000000"
    assert report["routes"][0]["output_usd_per_million"] == "0.3000000"
    assert report["catalog_sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["retrieved_at"] is None
    assert not report["gateway_identity_verified"]
    assert not report["provider_variant_price_verified"]
    assert not report["model_quality_verified"]


@pytest.mark.parametrize("price", [None, "NaN", "Infinity", "-1", 0, True, "", "1e-1000"])
def test_unknown_and_invalid_prices_never_count_as_free(price: Any) -> None:
    model = entry()
    model["pricing"]["prompt"] = price
    report = result(model)
    assert not report["all_catalog_checks_passed"]
    assert "missing_or_invalid_token_price" in report["routes"][0]["issues"]


def test_slightly_above_cap_fails_without_float_rounding() -> None:
    model = entry()
    model["pricing"]["prompt"] = "0.000000100000000000000001"
    assert "input_price_above_cap" in result(model)["routes"][0]["issues"]


def test_explicit_free_prices_are_distinct_from_missing() -> None:
    model = entry()
    model["pricing"] = {"prompt": "0", "completion": "0"}
    assert result(model)["all_catalog_checks_passed"]
    del model["pricing"]["completion"]
    assert not result(model)["all_catalog_checks_passed"]


@pytest.mark.parametrize(
    ("field", "value", "issue"),
    [
        ("context_length", 31999, "insufficient_or_unknown_context"),
        ("context_length", True, "insufficient_or_unknown_context"),
        ("supported_parameters", ["response_format"], "structured_outputs_not_advertised"),
        ("architecture", {"input_modalities": None}, "text_input_not_advertised"),
        (
            "architecture",
            {"input_modalities": ["text"], "output_modalities": [{}]},
            "required_output_not_advertised",
        ),
    ],
)
def test_capabilities_fail_closed(field: str, value: Any, issue: str) -> None:
    model = entry()
    model[field] = value
    assert issue in result(model)["routes"][0]["issues"]


@pytest.mark.parametrize(
    ("extra", "issue"),
    [
        ({"overrides": [{"prompt": "0.1"}]}, "conditional_pricing_requires_review"),
        ({"request": "0.01"}, "additional_charges_require_review"),
        ({"new_unrecognized_charge": {}}, "unknown_pricing_field_requires_review"),
        ({"input_cache_write": "0.0000002"}, "cache_input_price_above_cap"),
    ],
)
def test_default_price_cannot_hide_additional_pricing(extra: dict[str, Any], issue: str) -> None:
    model = entry()
    model["pricing"].update(extra)
    assert issue in result(model)["routes"][0]["issues"]


def test_all_declared_auxiliary_routes_are_checked_by_exact_id() -> None:
    declared = inventory().model_dump()
    auxiliary = copy.deepcopy(declared["routes"][0])
    auxiliary.update(route_id="aux", catalog_id="fixture/model:free", purposes=["auxiliary"])
    declared["routes"].append(auxiliary)
    report = audit(json.dumps({"data": [entry()]}).encode(), Inventory.model_validate(declared))
    assert not report["all_catalog_checks_passed"]
    assert report["routes"][0]["catalog_checks_passed"]
    assert report["routes"][1]["issues"] == ["catalog_id_not_found"]
    report = audit(json.dumps({"data": [entry()]}).encode(), inventory(all_routes_declared=False))
    assert not report["all_catalog_checks_passed"]


def test_duplicate_catalog_and_manifest_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="Duplicate catalog ID"):
        audit(json.dumps({"data": [entry(), entry()]}).encode(), inventory())
    route = inventory().routes[0].model_dump()
    with pytest.raises(ValidationError):
        inventory(routes=[route, route])
    with pytest.raises(ValueError, match="Duplicate JSON key"):
        audit(b'{"data":[],"data":[]}', inventory())


def test_dynamic_router_is_not_free_fixed_model() -> None:
    declared = inventory().model_dump()
    declared["routes"][0]["catalog_id"] = "openrouter/auto"
    model = entry()
    model.update(id="openrouter/auto", pricing={"prompt": "0", "completion": "0"})
    report = audit(json.dumps({"data": [model]}).encode(), Inventory.model_validate(declared))
    assert "dynamic_router_not_a_fixed_model" in report["routes"][0]["issues"]


@pytest.mark.parametrize("purposes", [["embedding"], ["embedding", "indexing"]])
def test_embedding_output_and_manifest_strictness(purposes: list[str]) -> None:
    declared = inventory().model_dump()
    declared["routes"][0].update(purposes=purposes, require_structured_outputs=False)
    model = entry()
    report = audit(json.dumps({"data": [model]}).encode(), Inventory.model_validate(declared))
    assert not report["all_catalog_checks_passed"]
    model["architecture"]["output_modalities"] = ["embeddings"]
    assert audit(json.dumps({"data": [model]}).encode(), Inventory.model_validate(declared))[
        "all_catalog_checks_passed"
    ]
    declared["api_key"] = "must-never-be-accepted"
    with pytest.raises(ValidationError):
        Inventory.model_validate(declared)


def test_cli_is_offline_by_default_and_does_not_overwrite(tmp_path: Path) -> None:
    manifest = tmp_path / "inventory.json"
    catalog = tmp_path / "catalog.json"
    output = tmp_path / "evidence.json"
    manifest.write_text(inventory().model_dump_json())
    catalog.write_text(json.dumps({"data": [entry()]}))
    args = ["--inventory", str(manifest), "--catalog", str(catalog), "--output", str(output)]
    assert main(args) == 0
    original = output.read_bytes()
    assert main(args) == 2
    assert output.read_bytes() == original
    catalog.write_text(json.dumps({"data": []}))
    output.unlink()
    assert main(args) == 1
    assert not json.loads(output.read_bytes())["all_catalog_checks_passed"]


def test_fetch_is_only_an_explicit_public_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from partner_agent import model_catalog

    calls: list[str] = []

    def fetch() -> bytes:
        calls.append(CATALOG_URL)
        return json.dumps({"data": [entry()]}).encode()

    monkeypatch.setattr(model_catalog, "fetch_catalog", fetch)
    manifest, output = tmp_path / "inventory.json", tmp_path / "evidence.json"
    manifest.write_text(inventory().model_dump_json())
    assert main(["--inventory", str(manifest), "--fetch", "--output", str(output)]) == 0
    assert calls == [CATALOG_URL]
    report = json.loads(output.read_bytes())
    assert report["catalog_source"] == CATALOG_URL
    assert report["retrieved_at"]
    assert main(["--inventory", str(manifest), "--fetch", "--output", str(output)]) == 2
    assert len(calls) == 1


def test_fetch_transport_has_no_authorization_or_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from partner_agent import model_catalog

    opener = Mock()
    opener.open.return_value = closing(BytesIO(b'{"data":[]}'))
    build = Mock(return_value=opener)
    monkeypatch.setattr(model_catalog.urllib.request, "build_opener", build)
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-be-sent")
    assert fetch_catalog() == b'{"data":[]}'
    request = opener.open.call_args.args[0]
    assert request.full_url == CATALOG_URL
    assert request.get_method() == "GET"
    assert request.header_items() == [("Accept", "application/json")]
    assert opener.open.call_args.kwargs == {"timeout": 25}
    handler = build.call_args.args[0]
    assert handler.redirect_request(None, None, 302, "redirect", {}, "https://example.org") is None


@pytest.mark.parametrize("raw", [b'{"data":NaN}', b'{"data":Infinity}', b"[]"])
def test_malformed_catalog_is_rejected(raw: bytes) -> None:
    with pytest.raises(ValueError):
        audit(raw, inventory())


def test_size_bound_and_finite_exponents(monkeypatch: pytest.MonkeyPatch) -> None:
    from partner_agent import model_catalog

    monkeypatch.setattr(model_catalog, "MAX_BYTES", 16)
    with pytest.raises(ValueError, match="size limit"):
        read_json(b" " * 17)
    model = entry()
    model["pricing"]["prompt"] = "0e999999999"
    monkeypatch.setattr(model_catalog, "MAX_BYTES", 8000)
    assert not result(model)["all_catalog_checks_passed"]
