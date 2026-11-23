"""deopt: state recovery for a fallback interpreter behind inlined guarded IR."""

from .interpreter import Frame, Interpreter, Runtime, UncaughtIRException
from .ir import (
    BindExc,
    Call,
    Const,
    Emit,
    EmitConst,
    Function,
    GetField,
    GuardType,
    Handler,
    IRException,
    IRObject,
    Jump,
    Move,
    New,
    Raise,
    Return,
    SetField,
)
from .materialize import MISSING, Materializer, PendingStore, RecoveryError
from .recovery import (
    EXCEPTION,
    NORMAL,
    DeoptFrame,
    GuardFailure,
    RecoveredState,
    recover,
)
from .sources import ConstSource, ReconstructSource, RegisterSource, Source

__all__ = [
    "BindExc", "Call", "Const", "Emit", "EmitConst", "Frame", "Function",
    "GetField", "GuardType", "Handler", "IRException", "IRObject",
    "Interpreter", "Jump", "MISSING", "Materializer", "Move", "New",
    "PendingStore", "Raise", "RecoveryError", "RecoveredState",
    "ReconstructSource", "RegisterSource", "ConstSource", "Return", "Runtime",
    "SetField", "Source", "UncaughtIRException",
    "DeoptFrame", "GuardFailure", "recover", "NORMAL", "EXCEPTION",
]
