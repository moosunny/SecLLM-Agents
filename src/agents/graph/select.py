from __future__ import annotations

from collections.abc import Sequence

from agents.graph.guards import ModificationInput
from agents.graph.state import GraphState
from agents.schema import AgentModuleResponse, CodeModification, Timings


def finalize_response(state: GraphState) -> AgentModuleResponse:
    selected = state["selected"]
    if selected is None:
        raise RuntimeError("fix branch completed without a selected candidate")
    request = state["request"]
    modifications = [
        CodeModification.model_validate(modification)
        for modification in _ordered_modifications(selected["modifications"])
    ]
    return AgentModuleResponse(
        user_id=request.user_id,
        module_kind=request.module_kind,
        request_idx=request.request_idx,
        desc=selected["desc"],
        code_modifications=modifications,
        timings=Timings.model_validate(state["timings"]),
    )


def _ordered_modifications(
    modifications: Sequence[ModificationInput],
) -> list[ModificationInput]:
    return sorted(
        modifications,
        key=lambda modification: (
            int(modification.get("startLine") or 0),
            0 if modification.get("type") == "delete" else 1,
        ),
    )
