"""Workflow content-provenance analysis module.

The module consumes a JSON job graph and propagates content origins along
every feasible trigger-to-release path.  See README.md for the graph schema
and the semantics of trust / source-constraint tracking.
"""

from .engine import analyze_graph, analyze_graph_dict
from .graph import GraphError, load_graph

__all__ = ["analyze_graph", "analyze_graph_dict", "load_graph", "GraphError"]
