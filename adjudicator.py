"""Compatibility facade for callers that prefer the module name ``adjudicator``.

The implementation lives in the :mod:`event_order` package.
"""

from event_order import *  # noqa: F401,F403
from event_order import __all__ as _public_names
from event_order.core import OrderAdjudicator, adjudicate_order, build_adjudicator

adjudicate = adjudicate_order
resolve_order = adjudicate_order
EventOrderResolver = OrderAdjudicator
OrderResolver = OrderAdjudicator

__all__ = [
    *_public_names,
    "adjudicate",
    "resolve_order",
    "EventOrderResolver",
    "OrderResolver",
]
