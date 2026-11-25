"""Access decisions shared by private transports. Source identity is not ownership."""
from dataclasses import dataclass


class ServiceError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


def source_grants(record: dict, configured: list[str], *, legacy_single=False) -> list[str]:
    if "external_sources" not in record and legacy_single and len(configured) == 1:
        return configured
    grants = record.get("external_sources")
    return [s for s in configured if isinstance(grants, list) and s in grants]


@dataclass(frozen=True)
class Principal:
    owner: str
    sources: frozenset[str]
    scopes: frozenset[str]

    @classmethod
    def from_verified(cls, claims, record, configured):
        if not isinstance(record, dict) or record.get("enabled") is not True:
            raise ServiceError(403, "ACCESS_DENIED")
        # Both operator grants and verified token scopes are required.
        allowed = record.get("cross_cloud_scopes")
        scopes = claims.get("scope")
        return cls(claims["sub"], frozenset(source_grants(record, configured)),
                   frozenset(s for s in scopes.split() if s in allowed)
                   if isinstance(scopes, str) and isinstance(allowed, list) else frozenset())

    def require(self, scope):
        if scope not in self.scopes:
            raise ServiceError(403, "INSUFFICIENT_SCOPE")

    def source(self, source_id):
        if source_id is not None and source_id not in self.sources:
            raise ServiceError(404, "NOT_FOUND")

    def predicate(self):
        sources = sorted(self.sources)
        placeholders = ",".join("?" for _ in sources) or "NULL"
        return (f"owner_id=? AND (source_id IS NULL OR source_id IN ({placeholders}))",
                [self.owner, *sources])


def page(limit, offset):
    if type(limit) is not int or type(offset) is not int or not (1 <= limit <= 100 and 0 <= offset <= 10000):
        raise ServiceError(422, "INVALID_PAGE")
    return limit, offset
