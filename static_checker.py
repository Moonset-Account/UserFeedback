"""Compatibility spelling for the static checker module."""

from static_check import *  # noqa: F401,F403
from static_check import audit_flow, check, check_flow  # noqa: F401

__all__ = ["audit_flow", "check", "check_flow"]
