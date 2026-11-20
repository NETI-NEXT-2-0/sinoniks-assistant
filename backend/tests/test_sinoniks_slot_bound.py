from partner_agent.sinoniks import DesignInput, plan


def test_known_incompatible_settings_set_a_lower_bound_for_network_slots():
    session = {
        "sender_side": "A",
        "sender": "Контроллер",
        "receiver": "Мониторинг",
        "mode": "transport",
        "service": "Телеметрия",
        "policy": "Только чтение",
        "protocol": "TCP",
        "destination_port": 443,
    }
    result = plan(
        DesignInput.model_validate(
            {
                "network_a": "Производство",
                "network_b": "Мониторинг",
                "firewall": "B",
                "exchanges": [
                    session,
                    {**session, "receiver": "Резервный мониторинг"},
                    {**session, "sender_side": "B"},
                    {**session, "mode": "diode"},
                    {**session, "protocol": "UDP"},
                    {**session, "destination_port": 444},
                    {**session, "protocol": None, "destination_port": 9443},
                    {**session, "destination_port": None},
                    {
                        **session,
                        "mode": "filetransfer",
                        "protocol": None,
                        "destination_port": None,
                    },
                ],
            }
        )
    )
    assert result.counts.network_slot_pairs == 8
    assert result.counts.file_transfer_policies == 1
    assert result.counts.evaluation_rule_count is None
    assert "Нижняя оценка числа сетевых пар слотов по известным настройкам — 6." in (
        result.explanation
    )
    assert "diode A → B — 1, transport A → B — 4, transport B → A — 1." in result.explanation


def test_slot_bound_pairs_compatible_partial_settings():
    result = plan(
        DesignInput.model_validate(
            {
                "network_a": "Производство",
                "network_b": "Мониторинг",
                "firewall": "A",
                "exchanges": [
                    {
                        "sender_side": "A",
                        "sender": "Контроллер",
                        "receiver": "Мониторинг",
                        "mode": "diode",
                        "service": "Показания",
                        "policy": "Только телеметрия",
                        **partial,
                    }
                    for partial in (
                        {"protocol": "UDP"},
                        {"destination_port": 9333},
                        {"protocol": "TCP"},
                        {"destination_port": 9222},
                        {},
                    )
                ],
            }
        )
    )
    assert result.counts.network_slot_pairs == 5
    assert "Нижняя оценка числа сетевых пар слотов по известным настройкам — 2." in (
        result.explanation
    )
