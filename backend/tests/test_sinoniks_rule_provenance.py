import pytest
from pydantic import ValidationError

from partner_agent.sinoniks import DesignInput, plan
from partner_agent.sinoniks_export import export_files


def test_explicit_requirements_and_sources_have_stable_export_order():
    source_document = {"kind": "document", "reference": "Ограничения обмена", "revision": 3}
    source_message = {"kind": "message", "reference": "Описание обмена", "revision": 1}
    flow = {
        "sender": "Хранилище отчётов",
        "receiver": "Архив",
        "sender_side": "A",
        "mode": "filetransfer",
        "service": "Отчёты",
        "policy": "Согласованные PDF",
    }
    first = {
        **flow,
        "requirement_id": "archive-2",
        "sources": [source_message, source_document, source_document],
    }
    second = {**flow, "requirement_id": "archive-1", "sources": [source_document]}
    third = {
        **flow,
        "requirement_id": "archive-csv",
        "policy": "Согласованные CSV",
        "sources": [source_message, source_message],
    }
    network = {"network_a": "Внешняя", "network_b": "Внутренняя", "firewall": "B"}
    original = plan(DesignInput.model_validate({**network, "exchanges": [first, second, third]}))
    reordered = plan(
        DesignInput.model_validate(
            {
                **network,
                "exchanges": [
                    third,
                    second,
                    {**first, "sources": list(reversed(first["sources"]))},
                ],
            }
        )
    )
    clean = plan(
        DesignInput.model_validate(
            {
                **network,
                "exchanges": [
                    {**first, "sources": [source_document, source_message]},
                    second,
                    {**third, "sources": [source_message]},
                ],
            }
        )
    )
    assert export_files(original) == export_files(reordered) == export_files(clean)
    assert len(original.rules) == 2
    grouped = next(rule for rule in original.rules if rule.policy == flow["policy"])
    assert grouped.requirement_ids == ["archive-1", "archive-2"]
    assert [source.model_dump() for source in grouped.sources] == [source_document, source_message]
    assert sum(len(rule.sources) for rule in original.rules) == 3


@pytest.mark.parametrize("reference", ["page\x00number", "page\udcffnumber"])
def test_invalid_source_reference_is_reported_at_the_input_field(reference):
    with pytest.raises(ValidationError) as failure:
        DesignInput.model_validate(
            {
                "exchanges": [{"sources": [{"kind": "document", "reference": reference}]}],
            }
        )
    assert failure.value.errors()[0]["loc"] == ("exchanges", 0, "sources", 0, "reference")
