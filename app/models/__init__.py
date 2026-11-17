# app/models/__init__.py
"""唯一的 ORM 模型集合。

导入本模块即注册全部表，Alembic 与 metadata.create_all 均以此为来源。
"""

from app.models.agent import Agent, AgentStatus
from app.models.api_token import ApiToken
from app.models.approval import (
    TERMINAL_APPROVAL_STATUSES,
    ApprovalRequest,
    ApprovalStatus,
    RiskLevel,
)
from app.models.artifact import Artifact
from app.models.audit import AuditEventType, AuditLog
from app.models.connector import (
    HEALTH_ERROR,
    HEALTH_OK,
    HEALTH_STATUSES,
    HEALTH_UNKNOWN,
    Connector,
    ConnectorAuthType,
    ConnectorCredential,
    ConnectorKind,
)
from app.models.context import ConversationSummary
from app.models.conversation import (
    AgentRun,
    Conversation,
    Message,
    MessageRole,
    RunStatus,
)
from app.models.custom_tool import CustomTool
from app.models.document import (
    Document,
    DocumentChunk,
    DocumentStatus,
    KnowledgeIndexState,
)
from app.models.knowledge_gov import (
    VALID_CASE_SOURCES,
    VALID_REASONS,
    VALID_VERDICTS,
    VERDICT_DOWN,
    VERDICT_UP,
    KnowledgeEvalCase,
    KnowledgeEvalRun,
    KnowledgeFeedback,
)
from app.models.mcp_registered import McpRegisteredTool
from app.models.memory import (
    Memory,
    MemoryReview,
    MemoryReviewAction,
    MemoryReviewStatus,
    MemoryStatus,
    MemoryType,
    TenantMemorySettings,
)
from app.models.model_provider import ModelProvider
from app.models.observability import ObsSpan, ObsTrace
from app.models.platform_setting import PlatformSetting
from app.models.rbac import (
    Role,
    RolePermission,
    StationGrant,
    User,
    UserRole,
)
from app.models.skill import Skill, SkillHit
from app.models.tenant import (
    Tenant,
    TenantQuota,
    TenantStatus,
    TenantToolPolicy,
)
from app.models.workflow import (
    OPEN_RUN_STATUSES,
    Workflow,
    WorkflowRun,
    WorkflowRunStatus,
)

__all__ = [
    "Agent",
    "AgentStatus",
    "ApprovalRequest",
    "ApprovalStatus",
    "RiskLevel",
    "TERMINAL_APPROVAL_STATUSES",
    "Artifact",
    "ApiToken",
    "AuditEventType",
    "AuditLog",
    "AgentRun",
    "Conversation",
    "Message",
    "MessageRole",
    "RunStatus",
    "ConversationSummary",
    "Document",
    "DocumentChunk",
    "DocumentStatus",
    "KnowledgeIndexState",
    "KnowledgeFeedback",
    "KnowledgeEvalCase",
    "KnowledgeEvalRun",
    "VERDICT_UP",
    "VERDICT_DOWN",
    "VALID_VERDICTS",
    "VALID_REASONS",
    "VALID_CASE_SOURCES",
    "Memory",
    "MemoryReview",
    "MemoryReviewAction",
    "MemoryReviewStatus",
    "TenantMemorySettings",
    "MemoryStatus",
    "MemoryType",
    "Role",
    "RolePermission",
    "StationGrant",
    "User",
    "UserRole",
    "Tenant",
    "TenantQuota",
    "TenantStatus",
    "TenantToolPolicy",
    "ModelProvider",
    "CustomTool",
    "McpRegisteredTool",
    "Connector",
    "ConnectorAuthType",
    "ConnectorCredential",
    "ConnectorKind",
    "HEALTH_ERROR",
    "HEALTH_OK",
    "HEALTH_STATUSES",
    "HEALTH_UNKNOWN",
    "ObsSpan",
    "ObsTrace",
    "PlatformSetting",
    "Skill",
    "SkillHit",
    "Workflow",
    "WorkflowRun",
    "WorkflowRunStatus",
    "OPEN_RUN_STATUSES",
]
