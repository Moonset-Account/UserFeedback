"""Descriptive top-level alias for the assurance recognition package."""

from assurance import *  # noqa: F401,F403
from assurance.engine import RecognitionEngine, recognize

AssuranceRecognitionEngine = RecognitionEngine

__all__ = ["RecognitionEngine", "AssuranceRecognitionEngine", "recognize"]
