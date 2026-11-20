from partner_agent.sinoniks_contract import Rule, Side


def network_slot_bounds(rules: list[Rule]) -> dict[tuple[str, Side], int]:
    settings_by_flow: dict[tuple[str, Side], set[tuple[str | None, int | None]]] = {}
    for rule in rules:
        if rule.mode != "filetransfer":
            settings_by_flow.setdefault((rule.mode, rule.sender_side), set()).add(
                (rule.protocol, rule.destination_port)
            )
    bounds = {}
    for flow, settings in settings_by_flow.items():
        fixed = {
            (protocol, port)
            for protocol, port in settings
            if protocol is not None and port is not None
        }
        protocols = {protocol for protocol, _ in settings if protocol is not None}
        ports = {port for _, port in settings if port is not None}
        extra_protocols = protocols - {protocol for protocol, _ in fixed}
        extra_ports = ports - {port for _, port in fixed}
        bounds[flow] = max(1, len(fixed) + max(len(extra_protocols), len(extra_ports)))
    return bounds
