"""Importing this package registers every model on ``Base.metadata``.

Alembic's env.py imports this module so autogenerate can see all tables.
"""
from app.models.agent_execution import (
    AgentExecution,
    AgentExecutionStatus,
    AgentTrigger,
)
from app.models.api_key import APIKey, APIKeyScope, scope_satisfies
from app.models.approval import Approval, ApprovalStatus, RiskLevel
from app.models.attachment import Attachment
from app.models.audit_log import ActorType, AuditLog
from app.models.authorization import Authorization
from app.models.engagement import Engagement, EngagementStatus
from app.models.finding import Finding, FindingPhase, FindingStatus, Severity
from app.models.mcp_lease import MCPLease, MCPLeaseStatus
from app.models.observation import Observation
from app.models.scope_item import ScopeItem, ScopeKind
from app.models.suggestion import (
    AgentName,
    Suggestion,
    SuggestionKind,
    SuggestionStatus,
)
from app.models.task import OwnerEligibility, Task, TaskKind, TaskStatus
from app.models.user import User
from app.models.user_provider_key import ProviderKeyKind, UserProviderKey
from app.models.workflow_template import WorkflowTemplate

__all__ = [
    "APIKey",
    "APIKeyScope",
    "ActorType",
    "AgentExecution",
    "Attachment",
    "AgentExecutionStatus",
    "AgentName",
    "AgentTrigger",
    "Approval",
    "ApprovalStatus",
    "AuditLog",
    "Authorization",
    "Engagement",
    "EngagementStatus",
    "Finding",
    "FindingPhase",
    "FindingStatus",
    "MCPLease",
    "MCPLeaseStatus",
    "Observation",
    "OwnerEligibility",
    "ProviderKeyKind",
    "RiskLevel",
    "ScopeItem",
    "ScopeKind",
    "Severity",
    "Suggestion",
    "SuggestionKind",
    "SuggestionStatus",
    "Task",
    "TaskKind",
    "TaskStatus",
    "User",
    "UserProviderKey",
    "WorkflowTemplate",
    "scope_satisfies",
]
