"""Coverage matrix toolkit.

Cross-references four records after a shared-library patch:
entry-point list, module call graph, patch checkpoints and
observed test traces, and classifies every entry point as
observed_covered / statically_reachable / unattributable
(/ no_reachable_checkpoint).
"""

__all__ = ["model", "loader", "analyzer", "report"]
