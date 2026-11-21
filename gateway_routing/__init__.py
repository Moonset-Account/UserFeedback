"""Gateway path-template routing with conflict-checked batch publication."""

from .errors import (
    PathError,
    PublishError,
    RouteConflictError,
    RoutingError,
    TemplateError,
)
from .router import Conflict, Route, RouteMatch, RouteTable, Router
from .templates import Template, TemplatePair, analyze_templates, parse_template, split_path

__all__ = [
    "Conflict",
    "PathError",
    "PublishError",
    "Route",
    "RouteConflictError",
    "RouteMatch",
    "RouteTable",
    "Router",
    "RoutingError",
    "Template",
    "TemplateError",
    "TemplatePair",
    "analyze_templates",
    "parse_template",
    "split_path",
]

__version__ = "1.0.0"
