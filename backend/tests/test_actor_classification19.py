"""Physical-person licensing is typed, not guessed from an actor's display name."""

from partner_agent.sinoniks_contract19 import Actor19, Requirements19
from partner_agent.sinoniks_file_rules import calculate_file_rules


def test_explicit_classification_overrides_misleading_names():
    assert Actor19(id="dept", name="Инженерный отдел", classification="group").is_collective()
    assert Actor19(
        id="dept2", name="Сотрудники отдела эксплуатации", classification="group",
    ).is_collective()
    assert not Actor19(id="ivanov", name="Группа Иванов", classification="person").is_collective()
    assert not Actor19(
        id="engineer", name="Инженер Иванов", classification="person",
    ).is_collective()


def test_legacy_actor_dump_preserves_existing_shape_and_prefix_fallback():
    legacy = Actor19(id="dept", name="Отдел 1")
    assert legacy.is_collective()
    assert legacy.model_dump(mode="json") == {"id": "dept", "name": "Отдел 1"}
    assert Actor19(id="operator", name=None).model_dump(mode="json") == {
        "id": "operator", "name": None,
    }


def _project(actors):
    return Requirements19.model_validate({
        "id": "project", "networks": [
            {"id": "office", "name": "Офис", "trust": "open"},
            {"id": "plant", "name": "Цех", "trust": "closed"},
        ],
        "actors": actors,
        "systems": [{"id": "plant-store", "kind": "storage", "network_id": "plant"}],
        "flows": [{
            "id": "files", "kind": "file", "direction": "forward",
            "sender_network_id": "office", "receiver_network_id": "plant",
            "receiver_ids": ["plant-store"],
            "sides": [
                {"network_id": "office", "manual": True, "manual_protocol": "SFTP",
                 "actor_ids": [actor["id"] for actor in actors], "checks": []},
                {"network_id": "plant", "manual": False, "usb": False,
                 "automations": [{"operation": "PUSH", "protocol": "SMB",
                                  "storage_id": "plant-store"}],
                 "checks": [], "received_files_shared": False,
                 "access_group": "engineering"},
            ],
        }],
    })


def test_group_is_not_a_licensed_person_and_unknown_headcount_stays_unknown():
    request = _project([{
        "id": "dept", "name": "Инженерный отдел", "classification": "group",
    }])
    result = calculate_file_rules(request, outside_network_id="office")
    assert result.user_details.actor_ids == ()
    assert result.user_details.manual_upper is None
    assert result.file_users_count is None


def test_explicit_person_is_counted_even_if_name_starts_with_group():
    request = _project([{
        "id": "ivanov", "name": "Группа Иванов", "classification": "person",
    }])
    result = calculate_file_rules(request, outside_network_id="office")
    assert result.user_details.actor_ids == ("ivanov",)
    assert result.user_details.manual_lower == 1
    assert result.user_details.manual_upper == 1
    assert result.file_users_count == 1
