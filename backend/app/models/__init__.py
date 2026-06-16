"""Importing this package registers every model on ``Base.metadata``.

Alembic's env.py imports this module so autogenerate can see all tables.
"""
from app.models.api_key import APIKey, APIKeyScope, scope_satisfies
from app.models.approval import Approval, ApprovalStatus, RiskLevel
from app.models.audit_log import ActorType, AuditLog
from app.models.authorization import Authorization
from app.models.engagement import Engagement, EngagementStatus
from app.models.finding import Finding, FindingPhase, FindingStatus, Severity
from app.models.scope_item import ScopeItem, ScopeKind
from app.models.user import User

__all__ = [
    "APIKey",
    "APIKeyScope",
    "ActorType",
    "Approval",
    "ApprovalStatus",
    "AuditLog",
    "Authorization",
    "Engagement",
    "EngagementStatus",
    "Finding",
    "FindingPhase",
    "FindingStatus",
    "RiskLevel",
    "ScopeItem",
    "ScopeKind",
    "Severity",
    "User",
    "scope_satisfies",
]
