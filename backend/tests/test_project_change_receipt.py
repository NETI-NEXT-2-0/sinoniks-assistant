from copy import deepcopy

from test_sinoniks_contract19 import data_project

from partner_agent.project_change_receipt import change_receipt


def test_added_server_without_flow_is_reported_explicitly():
    before = data_project()
    after = deepcopy(before)
    after["systems"].append({"id": "garage", "name": "Сервер автомастерской",
                             "network_id": "office"})
    changes, text = change_receipt(before, after)
    assert changes["systems"]["added"] == ["garage"]
    assert changes["new_systems_without_exchange"] == ["garage"]
    assert "Новые обмены не сохранены" in text
    assert "Сервер автомастерской" in text


def test_adding_a_flow_is_distinct_from_updating_existing_udp():
    before = data_project()
    after = deepcopy(before)
    after["flows"].append({"id": "tcp-new", "protocol": "TCP"})
    changes, text = change_receipt(before, after)
    assert changes["flows"] == {"added": ["tcp-new"], "updated": [], "removed": []}
    assert "Обмены добавлены: TCP" in text
    assert "Новые обмены не сохранены" not in text


def test_server_assigned_provenance_does_not_count_as_changed_requirement():
    before = data_project()
    after = deepcopy(before)
    after["flows"][0]["sources"] = [{"kind": "message", "reference": "new"}]
    changes, text = change_receipt(before, after)
    assert changes["flows"]["updated"] == []
    assert text == ""
