"""Stage 10 groundwork: approval queue, quality gate, kill switch and audit log.

Posting is off by default. The only X write path is x_transport.py, behind every switch.
"""

from trend_intelligence.publishing.gate import GateResult, run_gate
from trend_intelligence.publishing.publisher import (
    DryRunTransport,
    Publisher,
    PublishOutcome,
    Transport,
    engage_kill_switch,
    kill_switch_engaged,
    release_kill_switch,
)
from trend_intelligence.publishing.queue import (
    Draft,
    QueueError,
    approve,
    create_draft,
    get_draft,
    list_drafts,
    reject,
    status_of,
)

__all__ = [
    "Draft", "DryRunTransport", "GateResult", "PublishOutcome", "Publisher", "QueueError",
    "Transport", "approve", "create_draft", "engage_kill_switch", "get_draft",
    "kill_switch_engaged", "list_drafts", "reject", "release_kill_switch", "run_gate",
    "status_of",
]
