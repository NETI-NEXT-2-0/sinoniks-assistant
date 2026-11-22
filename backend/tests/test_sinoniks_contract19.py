"""Contract boundaries, migration honesty and stable references for the 1.9 solver."""

import json

import pytest
from pydantic import ValidationError
from test_sinoniks import full_input

from partner_agent.sinoniks import plan
from partner_agent.sinoniks_contract import Design, DesignInput
from partner_agent.sinoniks_contract19 import Requirements19
from partner_agent.sinoniks_contract19_io import assess, main, read_document, upgrade_input


def data_project():
    return {
        "schema_version": 3, "id": "project-1",
        "networks": [{"id": "office", "name": "Офис", "trust": "open"},
                     {"id": "plant", "name": "Цех", "trust": "closed"}],
        "systems": [{"id": "sender", "name": "Журналы", "network_id": "plant"},
                    {"id": "receiver", "name": "SIEM", "network_id": "office"}],
        "flows": [{"id": "logs", "kind": "data", "sender_network_id": "plant",
                   "receiver_network_id": "office", "sender_ids": ["sender"],
                   "receiver_ids": ["receiver"], "purpose": "Журналы", "mode": "one-way",
                   "protocol": "UDP", "destination_port": 514}],
    }


def file_project():
    document = data_project()
    for system in document["systems"]:
        system["kind"] = "storage"
    document["flows"] = [{
        "id": "files", "kind": "file", "sender_network_id": "plant",
        "receiver_network_id": "office", "sender_ids": ["sender"],
        "receiver_ids": ["receiver"], "purpose": "Документы", "direction": "both",
        "sides": [{"network_id": network, "manual": False, "usb": False,
                   "checks": [], "received_files_shared": True,
                   "automations": [{"operation": "PULL", "protocol": "SMB", "storage_id": storage}]}
                  for network, storage in (("plant", "sender"), ("office", "receiver"))],
    }]
    document["manual_users"] = {"lower": 0, "upper": 0}
    return document


def test_explicit_contract_roundtrip_keeps_ids_across_rename_and_reorder():
    document = data_project()
    original = Requirements19.model_validate(document)
    document["systems"].reverse()
    document["systems"][0]["name"] = "Другое имя"
    document["networks"].reverse()
    updated = Requirements19.model_validate(document)
    assert updated.flows == original.flows
    assert updated.id == original.id
    assert read_document(updated.model_dump(mode="json")) == updated
    assert assess(updated).status == "ready_for_solver"
    assert assess(updated).rules_count is None


@pytest.mark.parametrize("mutation", [
    lambda d: d["systems"][0].update(id="office"),
    lambda d: d["flows"][0].update(receiver_ids=["lost"]),
    lambda d: d["flows"][0].update(sender_ids=["sender", "sender"]),
    lambda d: d["flows"][0].update(receiver_network_id="plant"),
    lambda d: d["systems"][0].update(network_id="office"),
    lambda d: d["systems"][0].update(kind="usb", attachment="node-B", network_id=None),
    lambda d: d["systems"][0].update(kind="usb", attachment="node-A", network_id=None),
    lambda d: d["flows"][0].update(destination_port=True),
    lambda d: d["flows"][0].update(destination_port=65536),
    lambda d: d["flows"][0].update(destination_port="514"),
    lambda d: d["networks"].append({"id": "third"}),
    lambda d: d["networks"][0].update(name="Unsafe\x00name"),
    lambda d: d.update(unexpected="not ignored"),
])
def test_structural_errors_do_not_silently_change_the_request(mutation):
    document = data_project()
    mutation(document)
    with pytest.raises(ValidationError):
        Requirements19.model_validate(document)


def test_industrial_conflict_is_retained_for_clarification_not_downgraded():
    document = data_project()
    document["flows"][0]["protocol"] = "Modbus TCP"
    request = Requirements19.model_validate(document)
    assert request.flows[0].mode == "one-way"
    result = assess(request)
    assert result.status == "needs_clarification"
    assert any(item.kind == "conflict" and item.path == "/flows/0/mode" for item in result.issues)


def test_user_unknown_is_distinct_from_unasked_and_stale_notes_are_rejected():
    document = data_project()
    document["flows"][0]["protocol"] = None
    assert assess(Requirements19.model_validate(document)).status == "needs_clarification"
    document["unknowns"] = [{"path": "/flows/0/protocol", "target_id": "logs",
                             "reason": "user_unknown"}]
    request = Requirements19.model_validate(document)
    assert assess(request).status == "preliminary"
    assert request.flows[0].protocol is None
    document["unknowns"][0]["target_id"] = "someone-else"
    with pytest.raises(ValidationError, match="target ID"):
        Requirements19.model_validate(document)
    document["unknowns"][0]["target_id"] = "logs"
    document["flows"][0]["protocol"] = "UDP"
    with pytest.raises(ValidationError, match="supplied value"):
        Requirements19.model_validate(document)


def test_file_requirements_preserve_pull_on_both_sides_for_future_rule_splitting():
    request = Requirements19.model_validate(file_project())
    assert [side.automations[0].operation for side in request.flows[0].sides] == ["PULL", "PULL"]
    assert assess(request).status == "ready_for_solver"
    assert assess(request).rules_count is None  # not an illegal one-rule solution
    document = file_project()
    document["flows"][0]["sides"][0]["automations"][0]["storage_id"] = "receiver"
    with pytest.raises(ValidationError, match="own side"):
        Requirements19.model_validate(document)


def test_explicit_no_file_checks_differs_from_unknown_and_icap_is_external():
    document = file_project()
    document["flows"][0]["sides"][0]["checks"] = None
    assert assess(Requirements19.model_validate(document)).status == "needs_clarification"
    document["flows"][0]["sides"][0]["checks"] = [
        {"kind": "icap", "icap_service": "antivirus"}]
    assert assess(Requirements19.model_validate(document)).status == "ready_for_solver"
    document["flows"][0]["sides"][0]["checks"][0]["kind"] = "signature"
    with pytest.raises(ValidationError, match="external ICAP"):
        Requirements19.model_validate(document)


def test_usb_is_a_direct_file_attachment_not_a_fake_network_endpoint():
    document = file_project()
    document["systems"][0] = {"id": "sender", "kind": "usb", "attachment": "node-A"}
    document["flows"][0]["sides"][0]["automations"] = []
    document["flows"][0]["sides"][0]["usb"] = True
    request = Requirements19.model_validate(document)
    assert request.systems[0].network_id is None
    assert request.systems[0].attachment == "node-A"


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_migration_preserves_sources_and_unknowns_without_guessing_trust(version):
    document = full_input().model_dump(mode="json")
    document["schema_version"] = version
    document["exchanges"][0]["sources"] = [
        {"kind": "message", "reference": "message-1", "revision": 2}]
    original = DesignInput.model_validate(document)
    converted = upgrade_input(original, project_id="project-1")
    assert [network.trust for network in converted.networks] == [None, None]
    assert converted.legacy.firewall == original.firewall
    assert converted.flows[0].sources == original.exchanges[0].sources
    assert converted == upgrade_input(original, project_id="project-1")
    assert original.model_dump(mode="json") == document
    assert assess(converted).status == "needs_clarification"
    assert isinstance(read_document(document), DesignInput)


def test_old_file_policy_is_not_reinterpreted_as_typed_methods_or_checks():
    flow = full_input().exchanges[0].model_dump() | {
        "mode": "filetransfer", "protocol": None, "destination_port": None,
        "policy": "Антивирус"}
    converted = upgrade_input(full_input(exchanges=[flow]), project_id="project-1")
    assert converted.flows[0].sides == [] and converted.flows[0].direction is None
    assert converted.flows[0].policy == "Антивирус"
    assert not converted.assumptions


def test_saved_v2_design_is_read_without_rewriting_and_unknown_versions_fail():
    design = plan(full_input())
    saved = design.model_dump(mode="json")
    assert isinstance(read_document(saved), Design)
    assert read_document(saved).model_dump(mode="json") == saved
    for version in (True, "3", 4):
        with pytest.raises(ValueError):
            read_document({"schema_version": version})
    with pytest.raises(ValidationError):
        read_document({"schema_version": 3, "exchanges": []})


def test_cli_schema_and_migration_never_overwrite_an_archive(tmp_path, monkeypatch, capsys):
    source, target = tmp_path / "old.json", tmp_path / "new.json"
    source.write_text(full_input().model_dump_json(), encoding="utf-8-sig")
    monkeypatch.setattr("sys.argv", ["contract", "upgrade-input", str(source), str(target),
                                     "--project-id", "project-1"])
    main()
    saved = target.read_bytes()
    assert json.loads(saved)["schema_version"] == 3
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 2 and target.read_bytes() == saved
    monkeypatch.setattr("sys.argv", ["contract", "validate", str(target)])
    main()
    assert json.loads(capsys.readouterr().out)["assessment"]["rules_count"] is None
    schema = tmp_path / "schema.json"
    monkeypatch.setattr("sys.argv", ["contract", "schema", str(schema)])
    main()
    assert json.loads(schema.read_text())["properties"]["schema_version"]["const"] == 3


def test_legacy_maximum_identifier_survives_migration_without_truncation():
    document = full_input().model_dump(mode="json")
    document["exchanges"][0]["requirement_id"] = "r" * 80
    converted = upgrade_input(DesignInput.model_validate(document), project_id="project-1")
    assert converted.flows[0].requirement_ids == ["r" * 80]
    assert len(converted.flows[0].id) <= 80


def test_a_pending_legacy_kind_is_not_guessed_as_data():
    document = full_input().model_dump(mode="json")
    document["exchanges"][0]["mode"] = None
    with pytest.raises(ValueError, match="data/file kind"):
        upgrade_input(DesignInput.model_validate(document), project_id="project-1")


def test_unknown_note_cannot_move_silently_to_another_flow_on_reorder():
    document = data_project()
    document["flows"][0]["protocol"] = None
    document["flows"].append(document["flows"][0] | {"id": "different-flow"})
    document["unknowns"] = [{"path": "/flows/0/protocol", "target_id": "logs",
                             "reason": "user_unknown"}]
    Requirements19.model_validate(document)
    document["flows"].reverse()
    with pytest.raises(ValidationError, match="target ID"):
        Requirements19.model_validate(document)


@pytest.mark.parametrize("value", [True, "1", -1])
def test_manual_user_range_does_not_coerce_unknown_or_boolean_to_a_count(value):
    document = file_project()
    document["manual_users"] = {"lower": value}
    with pytest.raises(ValidationError):
        Requirements19.model_validate(document)


def test_unknown_manual_users_is_not_replaced_with_zero_or_a_rule_count():
    document = file_project()
    document["manual_users"] = None
    document["flows"][0]["sides"][0].update(manual=True, manual_protocol="SFTP")
    request = Requirements19.model_validate(document)
    assert request.manual_users is None
    assert assess(request).rules_count is None
    assert any(issue.path == "/manual_users" and issue.kind == "optional"
               for issue in assess(request).issues)


def test_automatic_file_exchange_does_not_ask_for_manual_users():
    document = file_project()
    document["manual_users"] = None
    assert assess(Requirements19.model_validate(document)).status == "ready_for_solver"


def test_ntp_does_not_ask_how_many_clients_receive_time():
    document = data_project()
    document["flows"][0].update(application="ntp", receiver_ids=[], destination_port=None)
    request = Requirements19.model_validate(document)
    assert not any(item.path.endswith("receiver_ids") for item in assess(request).issues)
    assert request.flows[0].destination_port is None  # no inferred port


def test_known_manual_actor_is_not_hidden_by_an_exact_zero_user_count():
    document = file_project()
    document["actors"] = [{"id": "engineer"}]
    side = document["flows"][0]["sides"][0]
    side.update(manual=True, manual_protocol="SFTP", actor_ids=["engineer"])
    with pytest.raises(ValidationError, match="distinct known actors"):
        Requirements19.model_validate(document)
    document["manual_users"] = {"lower": 1, "upper": 1}
    request = Requirements19.model_validate(document)
    assert request.manual_users.upper == 1
