import hashlib
import json
from collections import defaultdict, deque
from dataclasses import dataclass
from itertools import zip_longest
from typing import Literal

from partner_agent.sinoniks_contract19 import (
    Automatic19,
    CountRange19,
    FileFlow19,
    FileSide19,
    Requirements19,
)

Profile = tuple[object, ...]


@dataclass(frozen=True)
class FileIssue:
    flow_id: str
    reason: str
    network_id: str | None = None


@dataclass
class FileLane:
    source: FileSide19
    destination: FileSide19
    pull: Automatic19 | None
    push: Automatic19 | None
    flows: list[FileFlow19]
    provisional: bool

    @property
    def occupied(self) -> set[str]:
        return {side.network_id for side, setting in (
            (self.source, self.pull), (self.destination, self.push),
        ) if setting is not None}

    def checks_by_network(self) -> dict[str, list[dict[str, object]] | None]:
        return {
            side.network_id: None if checks is None else [json.loads(check) for check in checks]
            for side in (self.source, self.destination)
            for checks in [_checks(side)]
        }

    def as_dict(self) -> dict[str, object]:
        sources = {(item.kind, item.reference, item.revision): item for flow in self.flows
                   for item in flow.sources}
        return {
            "from_network_id": self.source.network_id,
            "to_network_id": self.destination.network_id,
            "checks_by_network": self.checks_by_network(),
            "methods_by_network": {side.network_id: self.methods_on(side.network_id)
                                   for side in (self.source, self.destination)},
            "automations": {
                side.network_id: setting.model_dump(mode="json")
                for side, setting in ((self.source, self.pull), (self.destination, self.push))
                if setting is not None
            },
            "received_files_shared": self.destination.received_files_shared,
            "access_groups": {side.network_id: side.access_group for side in (
                self.source, self.destination,
            )},
            "flow_ids": sorted(flow.id for flow in self.flows),
            "requirement_ids": sorted({
                item for flow in self.flows for item in flow.requirement_ids
            }),
            "purposes": sorted({flow.purpose for flow in self.flows if flow.purpose is not None}),
            "policy": self.flows[0].policy,
            "sources": [sources[key].model_dump(mode="json") for key in sorted(sources)],
            "sender_ids": sorted({system for flow in self.flows for system in (
                flow.sender_ids if self.source.network_id == flow.sender_network_id
                else flow.receiver_ids
            )}),
            "receiver_ids": sorted({system for flow in self.flows for system in (
                flow.receiver_ids if self.source.network_id == flow.sender_network_id
                else flow.sender_ids
            )}),
            "provisional": self.provisional,
        }

    def methods_on(self, network_id: str) -> dict[str, object]:
        sides = [side for flow in self.flows for side in flow.sides
                 if side.network_id == network_id]
        return {
            "manual": _known_switch({side.manual for side in sides}),
            "usb": _known_switch({side.usb for side in sides}),
            "manual_protocol": next((side.manual_protocol for side in sides
                                    if side.manual_protocol is not None), None),
            "actor_ids": sorted({actor for side in sides for actor in side.actor_ids}),
        }

    def manual_method_on(self, network_id: str) -> Literal["SFTP", "unspecified"] | None:
        methods = self.methods_on(network_id)
        if methods["manual"] is not True:
            return None
        if methods["manual_protocol"] == "SFTP":
            return "SFTP"
        return None if methods["usb"] is True else "unspecified"


@dataclass(frozen=True)
class FileRule:
    id: str
    lanes: list[FileLane]

    @property
    def flow_ids(self) -> list[str]:
        return sorted({flow.id for lane in self.lanes for flow in lane.flows})

    @property
    def direction(self) -> Literal["forward", "both"]:
        return "both" if len(self.lanes) == 2 else "forward"

    @property
    def primary_lane(self) -> FileLane:
        first = self.lanes[0]
        if len(self.lanes) == 2:
            reverse = self.lanes[1]
            reverse_ids = {flow.id for flow in reverse.flows}
            if any(flow.id in reverse_ids and flow.sender_network_id == reverse.source.network_id
                   for flow in first.flows):
                return reverse
        return first

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id, "direction": self.direction,
            "directions": [lane.as_dict() for lane in self.lanes],
        }

    def check_directions(self) -> list[dict[str, object]]:
        return [
            {"from_network_id": lane.source.network_id,
             "to_network_id": lane.destination.network_id,
             "checks_by_network": lane.checks_by_network()}
            for lane in self.lanes
        ]

    def access_directions(self) -> list[dict[str, object]]:
        return [
            {"from_network_id": lane.source.network_id,
             "to_network_id": lane.destination.network_id,
             "access_group": lane.destination.access_group,
             "received_files_shared": lane.destination.received_files_shared}
            for lane in self.lanes
        ]


@dataclass(frozen=True)
class FileUserDetails:
    actor_ids: tuple[str, ...]
    manual_lower: int
    manual_upper: int | None
    upper_unknown_reasons: tuple[str, ...]


@dataclass(frozen=True)
class FileCalculation:
    rules: list[FileRule]
    issues: list[FileIssue]
    minimum: int | None
    lower_bound: int
    declared_pulls: int
    file_users_count: int | None
    file_users_range: CountRange19 | None
    user_details: FileUserDetails


def _settings(side: FileSide19, operation: str) -> list[Automatic19]:
    return [setting for setting in side.automations if setting.operation == operation]


def _known_switch(states: set[bool | None]) -> bool | None:
    if True in states:
        return True
    return None if None in states else False


def _checks(side: FileSide19) -> tuple[str, ...] | None:
    if side.checks is None:
        return None
    return tuple(sorted({check.model_dump_json() for check in side.checks
                         if check.kind != "unsupported"}))


def _profile(lane: FileLane) -> Profile:
    flow = lane.flows[0]
    sides = sorted((lane.source, lane.destination), key=lambda side: side.network_id)
    return (
        flow.policy,
        tuple((side.network_id, _checks(side), side.access_group) for side in sides),
        flow.id if lane.provisional or (
            lane.destination.received_files_shared is False
            and lane.destination.access_group is None
        ) else None,
    )


def _method_present(side: FileSide19, automations: list[Automatic19]) -> bool:
    return bool(automations) or side.manual is True or side.usb is True


def _lanes(
    flow: FileFlow19, outside_network_id: str | None, usb_ids: set[str],
    explicit_check_gaps: set[str],
) -> tuple[
    list[FileLane], list[FileIssue],
]:
    if flow.sender_network_id is None or flow.receiver_network_id is None or flow.direction is None:
        return [], [FileIssue(flow.id, "file_direction_unknown")]
    sides = {side.network_id: side for side in flow.sides}
    source = sides.get(flow.sender_network_id)
    destination = sides.get(flow.receiver_network_id)
    if source is None or destination is None:
        return [], [FileIssue(flow.id, "file_methods_unknown")]
    issues = []
    for endpoint_ids, side in ((flow.sender_ids, source), (flow.receiver_ids, destination)):
        if usb_ids.isdisjoint(endpoint_ids):
            continue
        if side.usb is False:
            issues.append(FileIssue(flow.id, "usb_conflicts_with_file_method", side.network_id))
        else:
            sides[side.network_id] = side.model_copy(update={"usb": True})
    source, destination = sides[source.network_id], sides[destination.network_id]
    flow = flow.model_copy(update={"sides": list(sides.values())})
    directions = [(source, destination)]
    if flow.direction == "both":
        directions.append((destination, source))
    if flow.direction == "forward" and (
        _settings(source, "PUSH") or _settings(destination, "PULL")
    ):
        issues.append(FileIssue(flow.id, "automation_conflicts_with_file_direction"))
    for side in sides.values():
        if side.checks is None:
            reason = ("file_check_scope_unconfirmed" if flow.has_declared_checks()
                      and side.network_id not in explicit_check_gaps else "file_checks_unknown")
            issues.append(FileIssue(flow.id, reason, side.network_id))
        elif any(check.kind == "unsupported" for check in side.checks):
            issues.append(FileIssue(flow.id, "unsupported_file_check", side.network_id))
        if side.usb is True and side.network_id != outside_network_id:
            issues.append(FileIssue(flow.id, "usb_requires_node_a", side.network_id))
        if any(setting.protocol is None or setting.storage_id is None
               for setting in side.automations):
            issues.append(FileIssue(flow.id, "file_automation_details_unknown", side.network_id))
    lanes = []
    for sender, receiver in directions:
        pulls, pushes = _settings(sender, "PULL"), _settings(receiver, "PUSH")
        for side, automations in ((sender, pulls), (receiver, pushes)):
            if not _method_present(side, automations):
                issues.append(FileIssue(
                    flow.id, "file_method_unknown_or_unavailable", side.network_id,
                ))
        if receiver.received_files_shared is None:
            issues.append(FileIssue(flow.id, "file_visibility_unknown", receiver.network_id))
        settings: list[tuple[Automatic19 | None, Automatic19 | None]]
        if pulls and pushes and (len(pulls) > 1 or len(pushes) > 1):
            issues.append(FileIssue(flow.id, "file_automation_pairing_unknown"))
            settings = [(pull, None) for pull in pulls]
            settings.extend((None, push) for push in pushes)
        else:
            settings = list(zip_longest(pulls, pushes)) or [(None, None)]
        for pull, push in settings:
            lanes.append(FileLane(sender, receiver, pull, push, [flow], False))
    for lane in lanes:
        lane.provisional = bool(issues)
    return lanes, issues


def _compatible(left: FileLane, right: FileLane) -> bool:
    return not (left.occupied & right.occupied) and not (left.pull and right.pull)


def _pair(left: list[FileLane], right: list[FileLane]) -> dict[int, int]:
    matched_right: dict[int, int] = {}
    matched_left: dict[int, int] = {}
    for start in range(len(left)):
        previous = {}
        pending = deque([start])
        visited = {start}
        while pending:
            index = pending.popleft()
            free = None
            for other, candidate in enumerate(right):
                if other in previous or not _compatible(left[index], candidate):
                    continue
                previous[other] = index
                if other not in matched_right:
                    free = other
                    break
                existing = matched_right[other]
                if existing not in visited:
                    visited.add(existing)
                    pending.append(existing)
            if free is not None:
                while True:
                    current_left = previous[free]
                    old = matched_left.get(current_left)
                    matched_right[free] = current_left
                    matched_left[current_left] = free
                    if old is None:
                        break
                    free = old
                break
    return matched_right


def _merge_direction(lanes: list[FileLane]) -> list[FileLane]:
    visibility_groups: dict[bool | None, list[FileLane]] = defaultdict(list)
    for lane in lanes:
        visibility_groups[lane.destination.received_files_shared].append(lane)
    results = []
    for group in visibility_groups.values():
        automatic = [lane for lane in group if lane.occupied]
        manual = [lane for lane in group if not lane.occupied]
        if manual:
            anchor = automatic[0] if automatic else manual.pop(0)
            for lane in manual:
                anchor.flows.extend(lane.flows)
            if not automatic:
                automatic.append(anchor)
        results.extend(automatic)
    return results


def _rules(buckets: dict[Profile, list[FileLane]]) -> list[list[FileLane]]:
    result: list[list[FileLane]] = []
    for lanes in buckets.values():
        sides = sorted((lanes[0].source.network_id, lanes[0].destination.network_id))
        left, right = [
            _merge_direction([lane for lane in lanes if lane.source.network_id == side])
            for side in sides
        ]
        paired = _pair(left, right)
        result.extend([left[a], right[b]] for b, a in paired.items())
        matched_left = set(paired.values())
        result.extend([lane] for index, lane in enumerate(left) if index not in matched_left)
        result.extend([lane] for index, lane in enumerate(right) if index not in paired)
    return result


def has_unidentified_manual_users(request: Requirements19) -> bool:
    people = {actor.id for actor in request.actors if not actor.is_collective()}
    return any(
        len(flow.sides) != 2 or any(
            people.isdisjoint(side.actor_ids)
            and (side.manual is not False or side.usb is not False)
            for side in flow.sides
        ) for flow in request.flows if isinstance(flow, FileFlow19)
    )


def _user_counts(
    request: Requirements19, flows: list[FileFlow19], pulls: int, incomplete: bool,
) -> tuple[int | None, CountRange19 | None, FileUserDetails]:
    used_actors = {
        actor for flow in flows for side in flow.sides for actor in side.actor_ids
    }
    actors = tuple(sorted(actor.id for actor in request.actors
                          if actor.id in used_actors and not actor.is_collective()))
    if not flows:
        return 0, None, FileUserDetails(actors, 0, 0, ())
    declared = request.manual_users
    manual_lower = max(len(actors), declared.lower if declared else 0)
    if declared is not None:
        manual_upper = declared.upper
    else:
        manual_upper = None if has_unidentified_manual_users(request) else manual_lower
    reasons = []
    if manual_upper is None:
        reasons.append("manual_upper_unknown")
    if incomplete:
        reasons.append("file_rules_incomplete")
    details = FileUserDetails(actors, manual_lower, manual_upper, tuple(reasons))
    lower = manual_lower + pulls
    upper = manual_upper + pulls if manual_upper is not None and not incomplete else None
    if upper == lower:
        return lower, None, details
    interval = CountRange19(lower=lower, upper=upper) if lower or upper is not None else None
    return None, interval, details


def calculate_file_rules(
    request: Requirements19, *, outside_network_id: str | None,
) -> FileCalculation:
    flows = sorted((flow for flow in request.flows if isinstance(flow, FileFlow19)),
                   key=lambda flow: flow.id)
    issues = []
    buckets: dict[Profile, list[FileLane]] = defaultdict(list)
    usb_ids = {system.id for system in request.systems if system.kind == "usb"}
    documented_gaps = {note.path for note in request.unknowns}
    check_gaps = {
        flow.id: {side.network_id for side_index, side in enumerate(flow.sides)
                  if f"/flows/{flow_index}/sides/{side_index}/checks" in documented_gaps}
        for flow_index, flow in enumerate(request.flows) if isinstance(flow, FileFlow19)
    }
    for flow in flows:
        lanes, current_issues = _lanes(flow, outside_network_id, usb_ids, check_gaps[flow.id])
        issues.extend(current_issues)
        for lane in lanes:
            buckets[_profile(lane)].append(lane)
    matched = _rules(buckets)
    rules: list[FileRule] = []
    duplicate_counts: dict[str, int] = defaultdict(int)
    for group in matched:
        directions = [lane.as_dict() for lane in group]
        identity = json.dumps(directions, ensure_ascii=False, sort_keys=True)
        number = duplicate_counts[identity]
        duplicate_counts[identity] += 1
        digest = hashlib.sha256(f"{number}:{identity}".encode()).hexdigest()[:24]
        rules.append(FileRule(f"file-{digest}", group))
    pulls = sum(lane.pull is not None for group in matched for lane in group)
    isolated_groups: dict[str, set[str]] = defaultdict(set)
    for lanes in matched:
        for lane in lanes:
            destination = lane.destination
            if destination.received_files_shared is False and destination.access_group is not None:
                isolated_groups[destination.network_id].add(destination.access_group)
    lower_bound = max(
        int(bool(flows)), pulls, max(map(len, isolated_groups.values()), default=0),
        sum(not any(lane.provisional for lane in group) for group in matched),
    )
    users, user_range, user_details = _user_counts(request, flows, pulls, bool(issues))
    return FileCalculation(
        rules, list(dict.fromkeys(issues)), None if issues else len(rules),
        lower_bound, pulls, users, user_range, user_details,
    )
