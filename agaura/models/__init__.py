"""SQLAlchemy models (SPEC §8). Importing this package registers every table on ``Base``."""

from agaura.models.account import EmailToken, User, UserSession
from agaura.models.assembly import Assembly, CodeBatch, VoterCredential
from agaura.models.base import (
    AccessMode,
    AssemblyStatus,
    Base,
    BatchStatus,
    CredentialKind,
    EmailTokenKind,
    PollStatus,
    utcnow,
)
from agaura.models.delegate import Delegate
from agaura.models.log import AuditEntry, OutboxEmail
from agaura.models.note import Note
from agaura.models.poll import Ballot, Option, Participation, Poll, Result

__all__ = [
    "AccessMode",
    "Assembly",
    "AssemblyStatus",
    "AuditEntry",
    "Ballot",
    "Base",
    "BatchStatus",
    "CodeBatch",
    "CredentialKind",
    "Delegate",
    "EmailToken",
    "EmailTokenKind",
    "Note",
    "Option",
    "OutboxEmail",
    "Participation",
    "Poll",
    "PollStatus",
    "Result",
    "User",
    "UserSession",
    "VoterCredential",
    "utcnow",
]
