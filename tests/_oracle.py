"""Brute-force reference oracle used only by the test-suite.

It enumerates *complete legal event traces* (no state merging) for small
pipelines, independently from the production engine, and inserts them into a
trace trie.  The optimal witness pair under the problem's ordering is then
read off the trie without any quadratic trace comparison:

1. maximize the longest common prefix: a trie node whose leaves contain at
   least two distinct outputs, taken at maximum depth;
2. among ties, minimize the sorted pair of full event strings: at each
   candidate node take the lexicographically smallest leaf trace per
   distinct output and pick the smallest cross-output sorted pair.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from build_semantics.model import Pipeline, State, event_string


@dataclass
class _TrieNode:
    children: Dict[str, "_TrieNode"] = field(default_factory=dict)
    # output tuple -> lexicographically smallest complete leaf trace below
    best_leaf: Dict[Tuple[Any, ...], Tuple[str, ...]] = field(default_factory=dict)
    trace_at_leaf: Optional[Tuple[str, ...]] = None


def enumerate_traces(pipeline: Pipeline, root: State) -> List[Tuple[str, ...]]:
    traces: List[Tuple[str, ...]] = []

    def walk(state: State, prefix: List[str]) -> None:
        if state.is_complete(pipeline):
            traces.append(tuple(prefix))
            return
        for event in state.enabled_events(pipeline):
            nxt = state.apply(event, pipeline)
            prefix.append(event_string(event, pipeline))
            walk(nxt, prefix)
            prefix.pop()

    walk(root, [])
    return traces


def _replay_output(pipeline: Pipeline, root: State, trace: Sequence[str]) -> Tuple[Any, ...]:
    state = root
    for text in trace:
        kind, rest = text.split("(", 1)
        event = (kind, pipeline.index[rest[:-1]])
        state = state.apply(event, pipeline)
    return state.output_tuple(pipeline)


def lcp_length(a: Sequence[str], b: Sequence[str]) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _node_pair(node: _TrieNode) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    leaves = sorted(node.best_leaf.values())
    outputs = sorted(node.best_leaf, key=lambda o: tuple(repr(x) for x in o))
    best: Optional[Tuple[Tuple[str, ...], Tuple[str, ...]]] = None
    for i in range(len(outputs)):
        for j in range(i + 1, len(outputs)):
            pair = tuple(sorted((node.best_leaf[outputs[i]],
                                 node.best_leaf[outputs[j]])))
            if best is None or pair < best:
                best = pair
    assert best is not None
    return best


def optimal_witness_pair(
    pipeline: Pipeline, root: State
) -> Tuple[Tuple[str, ...], Tuple[str, ...], int]:
    trie = _TrieNode()
    ambiguous: List[Tuple[int, _TrieNode]] = []
    ambiguous_ids: set = set()

    for trace in enumerate_traces(pipeline, root):
        output = _replay_output(pipeline, root, trace)
        node = trie
        # Every prefix node gains this leaf's output (via its full trace).
        for depth, label in enumerate(trace, start=1):
            node = node.children.setdefault(label, _TrieNode())
            previous = node.best_leaf.get(output)
            if previous is None or trace < previous:
                node.best_leaf[output] = trace
            if len(node.best_leaf) >= 2 and id(node) not in ambiguous_ids:
                ambiguous_ids.add(id(node))
                ambiguous.append((depth, node))

    if not ambiguous:
        raise AssertionError("oracle: pipeline is deterministic")

    def sort_key(item: Tuple[int, _TrieNode]):
        depth, node = item
        return (-depth, _node_pair(node))

    depth, node = sorted(ambiguous, key=sort_key)[0]
    t1, t2 = _node_pair(node)
    assert lcp_length(t1, t2) == depth
    return t1, t2, depth
