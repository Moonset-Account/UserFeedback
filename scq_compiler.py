"""scq_compiler -- a small relational query compiler over safe conjunctive queries.

A read-only replica exposes a handful of approved relational *views*.  This
module decides whether a given query can be answered using only those views
and, if so, produces a rewriting that references the fewest views (budget: at
most 4 view references by default).

Data model
----------
* Terms are ``Var(name)`` or ``Const(value)``.
* Atoms are ``Atom(relation, args)`` with named relations.
* Queries / views are ``Query(head, body, name)`` where ``head`` is a tuple of
  output variables (each must occur in the body -- safety) and ``body`` is a
  tuple of atoms.  Bodies are interpreted under *set* semantics; duplicate
  atoms and duplicate result tuples collapse.
* A view definition is a ``Query`` whose head gives the view's output columns.
* A *rewriting* (candidate) is a ``Query`` whose body atoms reference view
  names instead of base relations.

Core algorithms
---------------
* ``expand`` unfolds a candidate into a conjunctive query over base
  relations.  Internal (projected-away) view variables are renamed fresh per
  view reference (alpha-renaming); sharing them across references is a bug
  that silently equates unrelated columns (see ``alpha_rename=False`` for a
  diagnostic mode that reproduces the mistake).
* ``contains`` / ``equivalent`` implement the homomorphism theorem for
  conjunctive queries: Q1 is contained in Q2 on all finite instances iff
  there is a homomorphism from Q2 into the frozen body of Q1.  Equivalence is
  containment in both directions, hence it holds for *all* finite instances,
  not just tested ones.
* ``find_rewritings`` enumerates MiniCon-style view coverings (MCDs),
  combines them into candidates of increasing size up to the budget, keeps
  the equivalent ones of minimal size and orders ties by a canonical
  (variable-renamed, atom-sorted) lexical form.
* ``verify_candidate`` checks a candidate both ways; when only one-way
  containment holds it builds a small base-table instance (the frozen body of
  the larger side) and pinpoints the result tuple on which the original query
  and the expanded candidate diverge.

Only the Python standard library is used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import combinations, permutations, product
from typing import Dict, Hashable, Iterable, Iterator, List, Optional, Sequence, Set, Tuple, Union

__all__ = [
    "Var", "Const", "Atom", "Query", "FrozenVar",
    "UnsafeQueryError", "check_safe",
    "parse_query", "parse_views", "format_query",
    "evaluate", "expand", "contains", "equivalent",
    "compute_mcds", "MCD", "find_rewritings", "RewriteResult",
    "verify_candidate", "Verification", "Counterexample",
    "canonical_form", "format_instance", "format_cell",
]

DEFAULT_BUDGET = 4


# --------------------------------------------------------------------- terms

@dataclass(frozen=True)
class Var:
    """A query variable."""
    name: str

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.name


@dataclass(frozen=True)
class Const:
    """A constant.  ``value`` may be any hashable Python object."""
    value: Hashable

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return repr(self.value)


Term = Union[Var, Const]


@dataclass(frozen=True)
class Atom:
    """A named relation applied to a tuple of terms."""
    relation: str
    args: Tuple[Term, ...]

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"{self.relation}({', '.join(map(str, self.args))})"


@dataclass(frozen=True)
class Query:
    """A safe conjunctive query (or view definition, or view rewriting)."""
    head: Tuple[Var, ...]
    body: Tuple[Atom, ...]
    name: str = "Q"


@dataclass(frozen=True)
class FrozenVar:
    """Cell marker used when a query body is frozen into an instance.

    Distinct from every user constant, so frozen variables can never collide
    with real data.
    """
    name: str

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"?{self.name}"


class UnsafeQueryError(ValueError):
    """Raised when a query violates safety (output var missing from body)."""


# ------------------------------------------------------------------ helpers

def _dedupe(items: Iterable) -> tuple:
    return tuple(dict.fromkeys(items))


def check_safe(query: Query) -> Query:
    """Validate safety: non-empty body; every output variable occurs in it."""
    if not query.body:
        raise UnsafeQueryError(f"{query.name}: body must not be empty")
    body_vars: Set[str] = set()
    for atom in query.body:
        for term in atom.args:
            if isinstance(term, Var):
                body_vars.add(term.name)
    for out in query.head:
        if not isinstance(out, Var):
            raise UnsafeQueryError(f"{query.name}: head entries must be variables, got {out!r}")
        if out.name not in body_vars:
            raise UnsafeQueryError(
                f"{query.name}: output variable {out.name!r} does not appear in the body")
    return query


def _subst(term: Term, mapping: Dict[str, Term]) -> Term:
    if isinstance(term, Var):
        return mapping.get(term.name, term)
    return term


def _subst_atom(atom: Atom, mapping: Dict[str, Term]) -> Atom:
    return Atom(atom.relation, tuple(_subst(t, mapping) for t in atom.args))


# ------------------------------------------------------------- homomorphism

_MISSING = object()


def _homomorphisms(source: Sequence[Atom],
                   target: Sequence[Atom],
                   seed: Optional[Dict[str, Term]] = None) -> Iterator[Dict[str, Term]]:
    """Yield all homomorphisms from ``source`` atoms into ``target`` atoms.

    A homomorphism maps each source variable to a target term; source
    constants must match identical target constants.  Every source atom must
    land on some target atom (set semantics: several source atoms may share
    one target atom).
    """
    index: Dict[str, List[Atom]] = {}
    for atom in target:
        index.setdefault(atom.relation, []).append(atom)
    assignment: Dict[str, Term] = dict(seed or {})
    order = sorted(source, key=lambda a: len(index.get(a.relation, ())))

    def backtrack(i: int) -> Iterator[Dict[str, Term]]:
        if i == len(order):
            yield dict(assignment)
            return
        atom = order[i]
        for cand in index.get(atom.relation, ()):
            if len(cand.args) != len(atom.args):
                continue
            bound: List[str] = []
            ok = True
            for t, ct in zip(atom.args, cand.args):
                if isinstance(t, Const):
                    if not (isinstance(ct, Const) and ct.value == t.value):
                        ok = False
                        break
                else:
                    cur = assignment.get(t.name, _MISSING)
                    if cur is _MISSING:
                        assignment[t.name] = ct
                        bound.append(t.name)
                    elif cur != ct:
                        ok = False
                        break
            if ok:
                yield from backtrack(i + 1)
            for name in bound:
                del assignment[name]

    yield from backtrack(0)


# ------------------------------------------------------------ freeze/thaw

def _freeze(query: Query) -> Tuple[List[Atom], Dict[str, Set[tuple]]]:
    """Turn a query body into frozen atoms plus the matching base instance."""
    atoms: List[Atom] = []
    instance: Dict[str, Set[tuple]] = {}
    for atom in _dedupe(query.body):
        cells = tuple(
            FrozenVar(t.name) if isinstance(t, Var) else t.value
            for t in atom.args
        )
        instance.setdefault(atom.relation, set()).add(cells)
        atoms.append(Atom(atom.relation, tuple(Const(c) for c in cells)))
    return atoms, instance


# ---------------------------------------------------------------- evaluation

def evaluate(query: Query, instance: Dict[str, Iterable[tuple]]) -> Set[tuple]:
    """Evaluate a CQ over base relations on ``instance`` (set semantics).

    ``instance`` maps relation names to iterables of tuples of cell values.
    Returns the set of result tuples (one entry per head variable, in order).
    """
    check_safe(query)
    atoms = list(query.body)
    results: Set[tuple] = set()
    assignment: Dict[str, Hashable] = {}

    def backtrack(i: int) -> None:
        if i == len(atoms):
            results.add(tuple(assignment[v.name] for v in query.head))
            return
        atom = atoms[i]
        for tup in instance.get(atom.relation, ()):
            if len(tup) != len(atom.args):
                continue
            bound: List[str] = []
            ok = True
            for term, cell in zip(atom.args, tup):
                if isinstance(term, Const):
                    if term.value != cell:
                        ok = False
                        break
                else:
                    cur = assignment.get(term.name, _MISSING)
                    if cur is _MISSING:
                        assignment[term.name] = cell
                        bound.append(term.name)
                    elif cur != cell:
                        ok = False
                        break
            if ok:
                backtrack(i + 1)
            for name in bound:
                del assignment[name]

    backtrack(0)
    return results


# ------------------------------------------------------- containment / equiv

def contains(q1: Query, q2: Query) -> bool:
    """True iff ``q1(I) <= q2(I)`` for *every* finite instance ``I``.

    By the homomorphism theorem this holds iff there is a homomorphism from
    the body of ``q2`` into the frozen body of ``q1`` that maps the head of
    ``q2`` onto the (frozen) head of ``q1``.
    """
    check_safe(q1)
    check_safe(q2)
    if len(q1.head) != len(q2.head):
        return False
    frozen_atoms, _ = _freeze(q1)
    seed: Dict[str, Term] = {}
    for h2, h1 in zip(q2.head, q1.head):
        cell = Const(FrozenVar(h1.name))
        if h2.name in seed:
            if seed[h2.name] != cell:
                return False
        else:
            seed[h2.name] = cell
    return any(_homomorphisms(list(q2.body), frozen_atoms, seed))


def equivalent(q1: Query, q2: Query) -> bool:
    """True iff ``q1`` and ``q2`` return the same tuples on every finite instance."""
    return contains(q1, q2) and contains(q2, q1)


# ------------------------------------------------------------------ expansion

def expand(candidate: Query,
           views: Dict[str, Query],
           alpha_rename: bool = True) -> Query:
    """Unfold a view-based candidate into a CQ over base relations.

    Each view atom is replaced by the view's body; output columns are bound to
    the atom's terms.  Internal (projected-away) view variables are renamed
    fresh *per reference* when ``alpha_rename`` is true (the correct default),
    so two references to the same view never share an internal column.

    ``alpha_rename=False`` is a diagnostic mode reproducing the classic bug
    where same-named internal columns of different references get equated;
    the resulting query is strictly more restrictive and silently loses
    tuples (see ``verify_candidate``).
    """
    body: List[Atom] = []
    for idx, atom in enumerate(candidate.body):
        view = views.get(atom.relation)
        if view is None:
            raise ValueError(f"unknown view {atom.relation!r} in candidate")
        if len(atom.args) != len(view.head):
            raise ValueError(
                f"view {atom.relation!r} takes {len(view.head)} columns, "
                f"got {len(atom.args)}")
        subst: Dict[str, Term] = {}
        for hv, t in zip(view.head, atom.args):
            subst[hv.name] = t
        head_names = {h.name for h in view.head}
        for batom in view.body:
            for t in batom.args:
                if isinstance(t, Var) and t.name not in head_names and t.name not in subst:
                    if alpha_rename:
                        subst[t.name] = Var(f"__{atom.relation}_{idx}_{t.name}")
                    else:
                        subst[t.name] = Var(t.name)  # diagnostic: shared name
        for batom in view.body:
            body.append(_subst_atom(batom, subst))
    return Query(candidate.head, _dedupe(body), candidate.name)


# ---------------------------------------------------------------------- MCDs

@dataclass(frozen=True)
class MCD:
    """A MiniCon-style view covering: one view reference whose expansion maps
    homomorphically into the query body, covering ``covered`` atom indices."""
    view: str
    head_terms: Tuple[Term, ...]
    covered: frozenset


def compute_mcds(query: Query, views: Dict[str, Query]) -> List[MCD]:
    """All distinct view references that can cover at least one query atom."""
    body = _dedupe(query.body)
    mcds: Set[MCD] = set()
    for vname, view in views.items():
        check_safe(view)
        vbody = list(_dedupe(view.body))
        for vb in vbody:
            for qi, qa in enumerate(body):
                if vb.relation != qa.relation or len(vb.args) != len(qa.args):
                    continue
                seed: Dict[str, Term] = {}
                ok = True
                for t, qt in zip(vb.args, qa.args):
                    if isinstance(t, Const):
                        if not (isinstance(qt, Const) and qt.value == t.value):
                            ok = False
                            break
                    else:
                        if t.name in seed:
                            if seed[t.name] != qt:
                                ok = False
                                break
                        else:
                            seed[t.name] = qt
                if not ok:
                    continue
                for theta in _homomorphisms(vbody, list(body), seed):
                    head_terms = tuple(_subst(h, theta) for h in view.head)
                    covered = frozenset(
                        i for i, qa in enumerate(body)
                        if any(_subst_atom(b, theta) == qa for b in vbody)
                    )
                    mcds.add(MCD(vname, head_terms, covered))
    return sorted(mcds, key=lambda m: (m.view, tuple(map(str, m.head_terms))))


# ------------------------------------------------------------- normalization

def canonical_form(query: Query) -> str:
    """Canonical string: variables renamed in scan order, atoms sorted.

    Invariant under input variable names and body atom order, so it can be
    used for the lexicographic tie-break between equally small rewritings.
    """
    head = list(query.head)
    groups: Dict[str, List[Atom]] = {}
    for atom in _dedupe(query.body):
        groups.setdefault(atom.relation, []).append(atom)
    rels = sorted(groups)
    best: Optional[str] = None
    for combo in product(*(permutations(groups[r]) for r in rels)):
        atoms = [a for grp in combo for a in grp]
        renaming: Dict[str, str] = {}

        def ren(t: Term) -> str:
            if isinstance(t, Const):
                return f"={t.value!r}"
            if t.name not in renaming:
                renaming[t.name] = f"x{len(renaming)}"
            return renaming[t.name]

        hparts = [ren(h) for h in head]
        aparts = [a.relation + "(" + ",".join(ren(t) for t in a.args) + ")" for a in atoms]
        s = "|".join(hparts) + ":" + ";".join(aparts)
        if best is None or s < best:
            best = s
    return best or ""


# ------------------------------------------------------------ rewriting search

@dataclass
class RewriteResult:
    """Outcome of ``find_rewritings``.

    ``rewritings`` holds every equivalent rewriting of minimal view-reference
    count, sorted by canonical form (so ``best`` is the lexicographically
    smallest).  Empty when no rewriting exists within the budget.
    """
    rewritings: Tuple[Query, ...]
    budget: int

    @property
    def best(self) -> Optional[Query]:
        return self.rewritings[0] if self.rewritings else None

    @property
    def num_refs(self) -> Optional[int]:
        return len(self.best.body) if self.best is not None else None

    def __bool__(self) -> bool:
        return bool(self.rewritings)


def find_rewritings(query: Query,
                    views: Dict[str, Query],
                    budget: int = DEFAULT_BUDGET) -> RewriteResult:
    """Find equivalent rewritings of ``query`` using at most ``budget`` view refs.

    Enumerates MCD combinations of increasing size; the first size that yields
    an equivalent rewriting wins (minimality of the reference count).  Ties
    are ordered by canonical form, so ``result.best`` is deterministic.
    """
    check_safe(query)
    qbody = _dedupe(query.body)
    target = Query(query.head, qbody, query.name)
    mcds = compute_mcds(target, views)
    all_idx = frozenset(range(len(qbody)))
    for size in range(1, budget + 1):
        found: Dict[str, Query] = {}
        for combo in combinations(mcds, size):
            covered = frozenset().union(*(m.covered for m in combo))
            if covered != all_idx:
                continue
            body = _dedupe(Atom(m.view, m.head_terms) for m in combo)
            if len(body) != size:
                continue  # duplicate view atoms collapse under set semantics
            rw = Query(target.head, body, target.name)
            try:
                check_safe(rw)
            except UnsafeQueryError:
                continue
            if equivalent(target, expand(rw, views)):
                found.setdefault(canonical_form(rw), rw)
        if found:
            return RewriteResult(tuple(found[k] for k in sorted(found)), budget)
    return RewriteResult((), budget)


# ---------------------------------------------------------------- verification

@dataclass
class Counterexample:
    """A small base-table instance on which query and candidate diverge.

    ``direction`` is ``"query_not_subset_of_candidate"`` (the candidate lost
    tuples the query returns) or ``"candidate_not_subset_of_query"`` (the
    candidate returns tuples the query does not).  ``witness_tuple`` is a
    concrete result tuple in the symmetric difference; cells shown as
    ``FrozenVar('x')`` stand for distinct domain values.
    """
    direction: str
    instance: Dict[str, Set[tuple]]
    witness_tuple: tuple

    def explain(self) -> str:
        if self.direction == "query_not_subset_of_candidate":
            what = "returned by the original query but missing from the candidate"
        else:
            what = "returned by the candidate but not by the original query"
        tup = "(" + ", ".join(format_cell(c) for c in self.witness_tuple) + ")"
        return (f"[{self.direction}] witness tuple {tup} is {what}.\n"
                f"Counterexample instance:\n{format_instance(self.instance)}")


@dataclass
class Verification:
    """Result of ``verify_candidate``."""
    equivalent: bool
    query_contained_in_candidate: bool
    candidate_contained_in_query: bool
    counterexamples: Tuple[Counterexample, ...]
    expansion: Query


def _witness(q1: Query, q2: Query) -> Tuple[Dict[str, Set[tuple]], tuple]:
    """Instance on which ``q1`` returns a tuple that ``q2`` does not."""
    _, instance = _freeze(q1)
    diff = evaluate(q1, instance) - evaluate(q2, instance)
    assert diff, "no witness although containment failed"  # homomorphism theorem
    witness = min(diff, key=lambda t: tuple(format_cell(c) for c in t))
    return instance, witness


def verify_candidate(query: Query,
                     views: Dict[str, Query],
                     candidate: Query,
                     alpha_rename: bool = True) -> Verification:
    """Check ``candidate`` (over views) against ``query`` on all finite instances.

    Expands the candidate (see ``expand`` for ``alpha_rename``), then decides
    containment in both directions.  For every failing direction a small
    counterexample instance and the diverging result tuple are produced.
    """
    check_safe(query)
    expansion = expand(candidate, views, alpha_rename=alpha_rename)
    q_in_c = contains(query, expansion)
    c_in_q = contains(expansion, query)
    cexs: List[Counterexample] = []
    if not q_in_c:
        inst, tup = _witness(query, expansion)
        cexs.append(Counterexample("query_not_subset_of_candidate", inst, tup))
    if not c_in_q:
        inst, tup = _witness(expansion, query)
        cexs.append(Counterexample("candidate_not_subset_of_query", inst, tup))
    return Verification(q_in_c and c_in_q, q_in_c, c_in_q, tuple(cexs), expansion)


# -------------------------------------------------------------------- parsing

_TOKEN_RE = re.compile(r"\s*(:-|[(),]|'[^']*'|-?\d+|[A-Za-z_][A-Za-z0-9_]*)")


def _tokenize(text: str) -> List[str]:
    tokens: List[str] = []
    pos = 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise SyntaxError(f"cannot parse at {text[pos:]!r}")
        tokens.append(m.group(1))
        pos = m.end()
    return tokens


class _Parser:
    def __init__(self, tokens: List[str]):
        self.toks = tokens
        self.i = 0

    def peek(self) -> Optional[str]:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def next(self) -> str:
        if self.i >= len(self.toks):
            raise SyntaxError("unexpected end of input")
        tok = self.toks[self.i]
        self.i += 1
        return tok

    def expect(self, tok: str) -> None:
        got = self.next()
        if got != tok:
            raise SyntaxError(f"expected {tok!r}, got {got!r}")

    def parse_term(self) -> Term:
        tok = self.next()
        if tok.startswith("'"):
            return Const(tok[1:-1])
        if tok.lstrip("-").isdigit():
            return Const(int(tok))
        if tok[0].isupper() or tok[0] == "_":
            return Var(tok)
        return Const(tok)

    def parse_atom(self) -> Tuple[str, Tuple[Term, ...]]:
        rel = self.next()
        if rel in (":-", "(", ")", ","):
            raise SyntaxError(f"expected relation name, got {rel!r}")
        self.expect("(")
        args: List[Term] = []
        if self.peek() != ")":
            args.append(self.parse_term())
            while self.peek() == ",":
                self.next()
                args.append(self.parse_term())
        self.expect(")")
        return rel, tuple(args)


def parse_query(text: str) -> Query:
    """Parse ``Name(X, Y) :- R(X, 'c'), S(Y, X)`` into a :class:`Query`.

    Term conventions: identifiers starting with an uppercase letter or ``_``
    are variables; ``'quoted'`` strings, integers and lowercase identifiers
    are constants.  The head functor becomes the query name.
    """
    text = text.split("#", 1)[0].strip().rstrip(".")
    if ":-" not in text:
        raise SyntaxError("expected 'Name(...) :- Body'")
    p = _Parser(_tokenize(text))
    name, head = p.parse_atom()
    for h in head:
        if not isinstance(h, Var):
            raise SyntaxError(f"head entries must be variables, got {h!r}")
    p.expect(":-")
    body: List[Atom] = []
    while True:
        rel, args = p.parse_atom()
        body.append(Atom(rel, args))
        if p.peek() == ",":
            p.next()
            continue
        break
    if p.peek() is not None:
        raise SyntaxError(f"trailing input: {p.peek()!r}")
    return Query(tuple(head), _dedupe(body), name)


def parse_views(text: str) -> Dict[str, Query]:
    """Parse several view definitions (one per line) into a name->Query map."""
    views: Dict[str, Query] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        q = parse_query(line)
        if q.name in views:
            raise ValueError(f"duplicate view definition {q.name!r}")
        views[q.name] = q
    return views


# ------------------------------------------------------------------ printing

def format_cell(cell: Hashable) -> str:
    if isinstance(cell, FrozenVar):
        return f"?{cell.name}"
    return repr(cell)


def format_query(query: Query) -> str:
    head = f"{query.name}({', '.join(h.name for h in query.head)})"
    body = ", ".join(str(a) for a in query.body)
    return f"{head} :- {body}"


def format_instance(instance: Dict[str, Iterable[tuple]]) -> str:
    lines: List[str] = []
    for rel in sorted(instance):
        for tup in sorted(instance[rel], key=lambda t: tuple(format_cell(c) for c in t)):
            lines.append(f"{rel}({', '.join(format_cell(c) for c in tup)})")
    return "\n".join(lines)
