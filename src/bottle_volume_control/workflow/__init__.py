"""Phase models and lazy exports for the workflow and its adapters.

Importing an event or context must not eagerly load the pipeline: communication
adapters also consume these models and may be imported before the pipeline.
"""
from importlib import import_module

from .context import ACTIVE, TERMINAL, Phase, PhaseContext, PhaseHandler
from .events import (
    CmdPause, CmdStart, CmdStop,
    CompletionConfirmed, Event, ExposureDone,
    LiquidUpdated, RecheckFailed, ScaleScored,
    TubeDetected, TubeLost,
)


_LAZY_EXPORTS = {
    "ACTIONS": (".state_machine", "ACTIONS"),
    "GUARDS": (".state_machine", "GUARDS"),
    "LIQ_REACH_THR_PX": (".state_machine", "LIQ_REACH_THR_PX"),
    "Row": (".state_machine", "Row"),
    "StateMachine": (".state_machine", "StateMachine"),
    "TRANSITIONS": (".state_machine", "TRANSITIONS"),
    "Pipeline": (".pipeline", "Pipeline"),
    "StubPhaseHandler": (".pipeline", "StubPhaseHandler"),
    "make_stub_handlers": (".pipeline", "make_stub_handlers"),
    "Camera": ("..camera.device", "Camera"),
    "UpperLink": ("..control.link", "UpperLink"),
    "to_wire_status": ("..control.link", "to_wire_status"),
    "VizRecorder": ("..diagnostics.recorder", "VizRecorder"),
    "CompletedPhase": (".phases", "CompletedPhase"),
    "DosingPhase": (".phases", "DosingPhase"),
    "IdlePhase": (".phases", "IdlePhase"),
    "LockScalePhase": (".phases", "LockScalePhase"),
    "RecheckFailPhase": (".phases", "RecheckFailPhase"),
    "SearchTubePhase": (".phases", "SearchTubePhase"),
    "SKIP_TUBE_HEIGHT_MIN": (".phases", "SKIP_TUBE_HEIGHT_MIN"),
    "make_handlers": (".phases", "make_handlers"),
}

__all__ = [
    "Phase", "PhaseContext", "PhaseHandler", "ACTIVE", "TERMINAL",
    "Event", "CmdStart", "CmdPause", "CmdStop", "TubeDetected", "TubeLost",
    "ExposureDone", "ScaleScored", "LiquidUpdated", "CompletionConfirmed",
    "RecheckFailed", *_LAZY_EXPORTS,
]


def __getattr__(name):
    try:
        module_name, attribute_name = _LAZY_EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
