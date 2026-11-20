from dataclasses import dataclass
from uuid import UUID

# curator is retained as a compatibility alias for the unified administrator role.
MANAGER_ROLES = ("admin", "curator")


@dataclass(frozen=True)
class Principal:
    id: UUID
    role: str
    disabled: bool = False


@dataclass(frozen=True)
class Resource:
    owner_id: UUID | None = None
    allowed_actions: frozenset[str] = frozenset()
    denied_actions: frozenset[str] = frozenset()


def authorize(principal: Principal, action: str, resource: Resource) -> bool:
    """Fail closed; admin manages accounts but acquires no content-reading privilege.

    Resource grants must be resolved from authoritative PostgreSQL by the caller.
    Membership alone must never populate allowed_actions.
    """
    known_actions = {
        "admin.users",
        "admin.groups",
        "admin.jobs",
        "admin.budget",
        "chat.read",
        "chat.write",
        "private_file.read",
        "knowledge.answer",
        "knowledge.excerpt",
        "knowledge.browse",
        "original.download",
        "document.manage",
        "acl.manage",
    }
    if action not in known_actions or principal.disabled or action in resource.denied_actions:
        return False
    if action in {"admin.users", "admin.groups", "admin.jobs", "admin.budget"}:
        return principal.role in MANAGER_ROLES
    if action in {"chat.read", "chat.write", "private_file.read"}:
        return principal.id == resource.owner_id
    return action in resource.allowed_actions
