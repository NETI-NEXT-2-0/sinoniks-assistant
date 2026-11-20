import pytest

from partner_agent.sinoniks import DesignInput, clarification_questions, plan


def test_conceptual_plan_reports_each_network_parameter_without_guessing():
    exchange = {
        "sender_side": "A",
        "sender": "Телеметрия",
        "receiver": "Хранилище",
        "service": "Доставка показаний",
        "policy": "Только показания",
    }
    requirements = DesignInput.model_validate(
        {
            "network_a": "Контур сбора",
            "network_b": "Контур отчётов",
            "firewall": "B",
            "exchanges": [
                {**exchange, "mode": "diode", "destination_port": 9000},
                {**exchange, "mode": "transport", "protocol": "TCP"},
                {**exchange, "mode": "transport", "protocol": "UDP", "destination_port": 9001},
                {**exchange, "mode": "filetransfer"},
            ],
        }
    )
    assert clarification_questions(requirements) == []

    result = plan(requirements)
    lines = result.explanation.splitlines()
    assert "Протокол — уточнить. Порт получателя — 9000." in result.explanation
    assert "Протокол — TCP. Порт получателя — уточнить." in result.explanation
    assert "Протокол — UDP. Порт получателя — 9001." in result.explanation
    file_line = next(line for line in lines if "filetransfer" in line)
    assert "Порт" not in file_line and "Протокол" not in file_line
    assert any("неуказанным протоколом или портом — 2" in warning for warning in result.warnings)
    assert result.counts.network_slot_pairs == 3
    assert result.counts.file_transfer_policies == 1
    assert result.counts.evaluation_rule_count is None


def test_text_links_network_names_and_rule_back_to_input_requirements():
    exchange = {
        "sender_side": "B",
        "sender": "Сборщик",
        "receiver": "Архив",
        "service": "Отчёты",
        "mode": "filetransfer",
        "policy": "Только PDF",
    }
    request = DesignInput.model_validate(
        {
            "network_a": "Архивный контур",
            "network_b": "Контур сбора",
            "firewall": "both",
            "exchanges": [
                {**exchange, "requirement_id": "req-1"},
                {**exchange, "requirement_id": "req-2"},
            ],
        }
    )
    design = plan(request)
    rule = design.rules[0]
    assert "Контур A — Архивный контур. Контур B — Контур сбора." in design.explanation
    assert "Исходных требований — 2. Логических обменов после удаления точных дублей — 1." in (
        design.explanation
    )
    detail = next(line for line in design.explanation.splitlines() if f"[{rule.id}]" in line)
    assert "Требования — req-1, req-2." in detail


@pytest.mark.parametrize("second_name", ["СЕВЕРНЫЙ   КОНТУР", "Северныи\u0306 контур"])
def test_equivalent_network_labels_need_disambiguation(second_name):
    request = DesignInput(network_a="Северный контур", network_b=second_name)

    question = next(
        question for question in clarification_questions(request) if question.field == "network_b"
    )

    assert question.reason == "conflict"
