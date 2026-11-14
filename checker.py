"""Static checker for SQL expression trees (SEC-20261114-030).

Input: JSON expression trees exported by a query builder. Node types:
  - {"type": "literal", "value": "..."}        static string literal
  - {"type": "var", "name": "..."}             variable (possibly external input)
  - {"type": "concat", "parts": [...]}         string concatenation
  - {"type": "param", "index": N}              parameter slot (bound value)
  - {"type": "cond", "branches": [{"label": L, "value": expr}, ...],
     "else": expr}                             conditional branch (optional else)
  - {"type": "exec", "id": ..., "location": ..., "structure": expr,
     "bindings": [expr, ...]}                  execution node

Rules:
  - external_inputs: variable names that carry external (untrusted) input
  - allowed_identifiers: constant strings allowed as dynamic identifiers

Semantics:
  - Each exec node is analysed independently.
  - In the structure expression, a literal appearing directly (not under a
    cond branch) is static SQL text and safe. Any value selected at runtime
    (a var anywhere, or a literal chosen via a cond branch) is a *dynamic
    identifier*: vars are always flagged (tainted; external inputs noted),
    branch-selected literals must come from allowed_identifiers.
  - Any feasible cond branch that constitutes a risk is reported with its
    branch path.
  - Bindings are bound values: any expression there is safe, but the number
    of bindings must equal the number of param slots in the structure;
    mismatches are listed separately.
"""


class CheckError(Exception):
    """Raised for malformed expression trees or rules."""


def _node_type(node):
    if not isinstance(node, dict) or "type" not in node:
        raise CheckError("node must be an object with a 'type' field: %r" % (node,))
    return node["type"]


def collect_atoms(node, path, atoms):
    """Collect atomic contributions of an expression.

    Each atom: {"kind": "const"|"tainted"|"param", "value", "source",
                "path": [branch labels], "dynamic": bool}
    `path` is the list of cond-branch labels taken to reach the atom.
    """
    t = _node_type(node)
    if t == "literal":
        atoms.append({
            "kind": "const",
            "value": node.get("value", ""),
            "source": None,
            "path": list(path),
            "dynamic": bool(path),
        })
    elif t == "var":
        atoms.append({
            "kind": "tainted",
            "value": None,
            "source": node.get("name"),
            "path": list(path),
            "dynamic": True,
        })
    elif t == "concat":
        parts = node.get("parts")
        if not isinstance(parts, list):
            raise CheckError("concat node requires a 'parts' list")
        for part in parts:
            collect_atoms(part, path, atoms)
    elif t == "param":
        atoms.append({
            "kind": "param",
            "value": node.get("index"),
            "source": None,
            "path": list(path),
            "dynamic": False,
        })
    elif t == "cond":
        branches = node.get("branches")
        if not isinstance(branches, list) or not branches:
            raise CheckError("cond node requires a non-empty 'branches' list")
        for i, br in enumerate(branches):
            if not isinstance(br, dict) or "value" not in br:
                raise CheckError("cond branch requires a 'value' expression")
            label = br.get("label", "branch[%d]" % i)
            collect_atoms(br["value"], path + [label], atoms)
        if "else" in node:
            collect_atoms(node["else"], path + ["else"], atoms)
    elif t == "exec":
        # Nested execution nodes are analysed on their own; opaque here.
        return
    else:
        raise CheckError("unknown node type: %r" % (t,))


def count_params(node):
    """Count parameter slots in a structure expression."""
    t = _node_type(node)
    if t == "param":
        return 1
    if t == "concat":
        return sum(count_params(p) for p in node.get("parts", []))
    if t == "cond":
        total = sum(count_params(br["value"]) for br in node.get("branches", []))
        if "else" in node:
            total += count_params(node["else"])
        return total
    if t in ("literal", "var", "exec"):
        return 0
    raise CheckError("unknown node type: %r" % (t,))


def find_execs(node, out):
    """Recursively collect all exec nodes in a tree."""
    if isinstance(node, list):
        for item in node:
            find_execs(item, out)
        return
    if not isinstance(node, dict):
        raise CheckError("expression tree must be an object or list")
    t = _node_type(node)
    if t == "exec":
        out.append(node)
        # Still descend: exec nodes may nest inside structure/bindings.
        if "structure" in node:
            find_execs_in_expr(node["structure"], out)
        for b in node.get("bindings", []):
            find_execs_in_expr(b, out)
        return
    find_execs_in_expr(node, out)


def find_execs_in_expr(node, out):
    if isinstance(node, list):
        for item in node:
            find_execs_in_expr(item, out)
        return
    if not isinstance(node, dict):
        return
    t = node.get("type")
    if t == "exec":
        find_execs(node, out)
    elif t == "concat":
        for p in node.get("parts", []):
            find_execs_in_expr(p, out)
    elif t == "cond":
        for br in node.get("branches", []):
            find_execs_in_expr(br.get("value"), out)
        if "else" in node:
            find_execs_in_expr(node["else"], out)


def analyze_exec(exec_node, rules):
    """Analyse a single execution node; returns its report entry."""
    external = set(rules.get("external_inputs", []))
    allowed = set(rules.get("allowed_identifiers", []))

    structure = exec_node.get("structure")
    if structure is None:
        raise CheckError("exec node %r requires a 'structure' expression"
                         % (exec_node.get("id"),))
    bindings = exec_node.get("bindings", [])
    if not isinstance(bindings, list):
        raise CheckError("exec node %r: 'bindings' must be a list"
                         % (exec_node.get("id"),))

    atoms = []
    collect_atoms(structure, [], atoms)

    findings = []
    for atom in atoms:
        branch = atom["path"] or None
        if atom["kind"] == "tainted":
            src = atom["source"]
            findings.append({
                "type": "tainted_structure",
                "source": src,
                "external_input": src in external,
                "branch": branch,
                "message": ("variable %r used as query structure"
                            + (" (external input)" if src in external
                               else " (not a constant from the allowed set)"))
                           % (src,),
            })
        elif atom["kind"] == "const" and atom["dynamic"]:
            if atom["value"] not in allowed:
                findings.append({
                    "type": "identifier_not_allowed",
                    "value": atom["value"],
                    "branch": branch,
                    "message": ("dynamically selected identifier %r is not in "
                                "the allowed constant set") % (atom["value"],),
                })

    slots = count_params(structure)
    binding_check = {
        "param_slots": slots,
        "bindings": len(bindings),
        "ok": slots == len(bindings),
    }

    return {
        "id": exec_node.get("id"),
        "location": exec_node.get("location"),
        "status": "fail" if (findings or not binding_check["ok"]) else "pass",
        "findings": findings,
        "binding_check": binding_check,
    }


def analyze(tree, rules):
    """Analyse a whole tree (exec node, list, or nested); returns a report."""
    for key in ("external_inputs", "allowed_identifiers"):
        if key in rules and not isinstance(rules[key], list):
            raise CheckError("rules field %r must be a list" % key)

    execs = []
    if isinstance(tree, dict) and tree.get("type") != "exec" and "executions" in tree:
        find_execs(tree["executions"], execs)
    else:
        find_execs(tree, execs)

    executions = [analyze_exec(e, rules) for e in execs]
    mismatches = [
        {"id": e["id"], "location": e["location"],
         "param_slots": e["binding_check"]["param_slots"],
         "bindings": e["binding_check"]["bindings"]}
        for e in executions if not e["binding_check"]["ok"]
    ]
    risk_count = sum(len(e["findings"]) for e in executions)
    return {
        "summary": {
            "exec_count": len(executions),
            "risk_count": risk_count,
            "mismatch_count": len(mismatches),
        },
        "executions": executions,
        "binding_mismatches": mismatches,
    }
