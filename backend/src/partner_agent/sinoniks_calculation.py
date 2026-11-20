from dataclasses import asdict, dataclass

from partner_agent.sinoniks_contract19 import Requirements19
from partner_agent.sinoniks_data_rules import RULE_LIMIT, DataCalculation, calculate_data_rules
from partner_agent.sinoniks_file_rules import FileCalculation, calculate_file_rules


@dataclass(frozen=True)
class RuleCalculation:
    data: DataCalculation
    files: FileCalculation

    @property
    def lower_bound(self) -> int:
        return self.data.confirmed_network_lower_bound + self.files.lower_bound

    @property
    def minimum(self) -> int | None:
        data, files = self.data.network_minimum, self.files.minimum
        return data + files if data is not None and files is not None else None

    @property
    def capacity_exceeded(self) -> bool:
        return self.lower_bound > RULE_LIMIT


def solve_project(
    request: Requirements19, *, protected_network_id: str | None = None,
) -> RuleCalculation:
    data = calculate_data_rules(request, protected_network_id=protected_network_id)
    files = calculate_file_rules(
        request, outside_network_id=data.placement.node_a_network_id if data.placement else None,
    )
    return RuleCalculation(data, files)


def calculate_rules(
    request: Requirements19, *, protected_network_id: str | None = None,
) -> dict[str, object]:
    result = solve_project(request, protected_network_id=protected_network_id)
    data, files = result.data, result.files
    return {
        "placement": asdict(data.placement) if data.placement else None,
        "data_rules": [rule.as_dict() for rule in data.rules],
        "file_rules": [rule.as_dict() for rule in files.rules],
        "rules_count": result.minimum,
        "rules_lower_bound": result.lower_bound,
        "capacity_exceeded": result.capacity_exceeded,
        "data_issues": [asdict(issue) for issue in data.issues],
        "file_issues": [asdict(issue) for issue in files.issues],
        "file_users_count": files.file_users_count,
        "file_users_range": (files.file_users_range.model_dump(mode="json")
                             if files.file_users_range is not None else None),
        "declared_pull_users": files.declared_pulls,
    }
