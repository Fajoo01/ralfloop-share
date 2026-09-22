"""ABC relational domain.

The package keeps observable facts, derived interpretations and strategy snapshots
separate.  MCP transport lives in :mod:`ralfloop_agent.abc_relation.mcp` while
storage and scoring stay usable without MCP.
"""

from .models import RelationEvent, RelationSnapshot
from .service import RelationService
from .store import RelationStore

__all__ = ["RelationEvent", "RelationSnapshot", "RelationService", "RelationStore"]
