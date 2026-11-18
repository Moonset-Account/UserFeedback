"""
Optional precise re-check for the community privilege-escalation auditor.

The default community auditor (``parliament.community_auditors.privilege_escalation``)
collapses allowed actions into a set and raises a finding when a combination such
as ``iam:AttachRolePolicy`` + ``sts:AssumeRole`` appears anywhere in the policy.
It cannot tell whether the "modify the role" step and the "use the role" step
actually land on the *same* role.

This module provides the optional precise re-check.  The caller supplies:

- a finite list of concrete role ARNs,
- fixed principal attributes (ex. ``{"aws:PrincipalArn": "..."}``),
- per-step request condition values (``{"modify": {...}, "assume": {...}}``).

For every role the two steps are evaluated separately against the identity
policy, so an allow on "modify role A" is never spliced together with an allow
on "assume role B".

Semantics:

- Action and Resource matching reuse the existing ``*`` glob handling.
- Conditions support ``StringEquals``, ``Bool`` and ``Null``.  Multiple values
  of the same key are OR-ed; different keys and different operators are AND-ed.
  A missing context key fails ``StringEquals``/``Bool`` comparisons and is
  evaluated as key-absence for ``Null``.
- An applicable explicit ``Deny`` always wins over ``Allow``.
- Unsupported condition operators, ``NotAction`` and ``NotResource`` are kept
  as *unknown*: they are neither treated as unconditional allows nor silently
  ignored.  A determinate deny is not washed out by unrelated unknowns, and a
  possibly-applicable unknown ``Deny`` is not masked by a known ``Allow``.

The result only describes what the given identity policy permits under the
given context.  It does NOT evaluate role trust policies, service control
policies, permissions boundaries or session policies, so a "supported" status
is not a complete exploitability conclusion.
"""

import fnmatch

from . import is_glob_match
from .misc import make_list

MODIFY_ACTIONS = ("iam:AttachRolePolicy", "iam:PutRolePolicy")
ASSUME_ACTIONS = ("sts:AssumeRole",)

STEPS = (("modify", MODIFY_ACTIONS), ("assume", ASSUME_ACTIONS))

SUPPORTED_CONDITION_OPERATORS = {"stringequals", "bool", "null"}

SCOPE_NOTE = (
    "Identity-policy-only evaluation under the caller-supplied context. "
    "A 'supported' status means the identity policy alone permits the "
    "modify+assume combination on this role; it does not establish that the "
    "role trust policy, service control policies, permissions boundaries or "
    "session policies also permit exploitation."
)

# Three-valued logic: True / False / UNKNOWN
UNKNOWN = None


def _action_pattern_matches(pattern, action):
    """Existing star matching for actions, ex. "iam:*RolePolicy" or "*"."""
    pattern = pattern.lower()
    if pattern in ("*", "*:*"):
        return True
    return fnmatch.fnmatchcase(action.lower(), pattern)


def _resource_pattern_matches(pattern, resource_arn):
    """Existing star matching for resources (glob intersection with a concrete ARN)."""
    return is_glob_match(pattern, resource_arn)


def _eval_condition_entry(operator, key, values, context):
    """
    Evaluate one operator/key/values entry against the context.
    Returns True, False or UNKNOWN (unsupported operator).
    """
    op = operator.lower()
    if op not in SUPPORTED_CONDITION_OPERATORS:
        return UNKNOWN

    present = key.lower() in context

    if op == "null":
        # Null checks key (non-)existence. Values are OR-ed like any other key.
        for v in values:
            expected_absent = str(v).lower() == "true"
            if (not present) == expected_absent:
                return True
        return False

    if not present:
        # Missing key: StringEquals / Bool comparisons fail.
        return False

    ctx_value = context[key.lower()]
    if op == "stringequals":
        # Values of the same key are OR-ed.
        return any(ctx_value == str(v) for v in values)
    # bool
    return any(ctx_value.lower() == str(v).lower() for v in values)


def _eval_condition_block(stmt_json, context):
    """
    Evaluate the Condition element of a statement.
    Different operators and different keys are AND-ed (three-valued).
    Returns True, False or UNKNOWN.
    """
    if "Condition" not in stmt_json:
        return True

    saw_unknown = False
    for condition in stmt_json["Condition"]:
        operator = condition[0]
        condition_block = condition[1]
        for entry in condition_block:
            key = entry[0]
            values = [v.value for v in make_list(entry[1])]
            result = _eval_condition_entry(operator, key, values, context)
            if result is False:
                return False
            if result is UNKNOWN:
                saw_unknown = True
    return UNKNOWN if saw_unknown else True


def _statement_relevance(stmt_json, action, resource_arn, context):
    """
    Determine how a statement relates to this (action, resource, context).
    Returns "applies", "unknown" or "not_applicable".
    """
    # Action / NotAction
    if "Action" in stmt_json:
        patterns = [a.value for a in make_list(stmt_json["Action"])]
        if not any(_action_pattern_matches(p, action) for p in patterns):
            return "not_applicable"
        action_unknown = False
    elif "NotAction" in stmt_json:
        patterns = [a.value for a in make_list(stmt_json["NotAction"])]
        if any(_action_pattern_matches(p, action) for p in patterns):
            # The action is explicitly excluded, so the statement cannot apply.
            return "not_applicable"
        # NotAction is not positively evaluated; keep it as unknown.
        action_unknown = True
    else:
        # Malformed statement (no Action/NotAction); already flagged by the linter.
        return "not_applicable"

    # Resource / NotResource
    if "Resource" in stmt_json:
        patterns = [r.value for r in make_list(stmt_json["Resource"])]
        if not any(_resource_pattern_matches(p, resource_arn) for p in patterns):
            return "not_applicable"
        resource_unknown = False
    elif "NotResource" in stmt_json:
        patterns = [r.value for r in make_list(stmt_json["NotResource"])]
        if any(_resource_pattern_matches(p, resource_arn) for p in patterns):
            # The resource is explicitly excluded, so the statement cannot apply.
            return "not_applicable"
        # NotResource is not positively evaluated; keep it as unknown.
        resource_unknown = True
    else:
        # Malformed statement (no Resource/NotResource); already flagged by the linter.
        return "not_applicable"

    condition = _eval_condition_block(stmt_json, context)
    if condition is False:
        return "not_applicable"
    if condition is UNKNOWN or action_unknown or resource_unknown:
        return "unknown"
    return "applies"


def _statement_source(index, stmt):
    sid = getattr(stmt, "sid", None)
    if sid:
        return sid
    return "Statement[{}]".format(index)


def _eval_action(statements, action, resource_arn, context):
    """
    Evaluate a single action against a concrete resource ARN.
    Returns a dict with the verdict and the allowing / denying / unknown
    statement sources.
    """
    allows = []
    denies = []
    unknowns = []
    for index, stmt in enumerate(statements):
        relevance = _statement_relevance(stmt.stmt, action, resource_arn, context)
        if relevance == "not_applicable":
            continue
        source = _statement_source(index, stmt)
        effect = "Allow" if stmt.effect_allow else "Deny"
        if relevance == "applies":
            if stmt.effect_allow:
                allows.append(source)
            else:
                denies.append(source)
        else:
            unknowns.append({"source": source, "effect": effect})

    unknown_denies = [u for u in unknowns if u["effect"] == "Deny"]
    if denies:
        # An applicable explicit Deny is determinate and is not washed out
        # by unrelated unknown statements.
        verdict = "denied"
    elif allows and not unknown_denies:
        verdict = "allowed"
    elif allows or unknowns:
        # A possibly-applicable unknown Deny is not masked by a known Allow,
        # and an unknown Allow keeps an otherwise implicit deny undetermined.
        verdict = "unknown"
    else:
        # Nothing allows the action: implicit deny.
        verdict = "denied"

    return {"verdict": verdict, "allow": allows, "deny": denies, "unknown": unknowns}


def _combine_step(action_results):
    """
    Combine the per-action results of one step.  The step is allowed if any of
    its actions is allowed, denied only if every action is denied, and unknown
    otherwise.
    """
    verdicts = [r["verdict"] for r in action_results.values()]
    if any(v == "allowed" for v in verdicts):
        verdict = "allowed"
    elif all(v == "denied" for v in verdicts):
        verdict = "denied"
    else:
        verdict = "unknown"

    allow = []
    deny = []
    unknown = []
    for r in action_results.values():
        for source in r["allow"]:
            if source not in allow:
                allow.append(source)
        for source in r["deny"]:
            if source not in deny:
                deny.append(source)
        for u in r["unknown"]:
            if u not in unknown:
                unknown.append(u)

    return {
        "verdict": verdict,
        "allow": allow,
        "deny": deny,
        "unknown": unknown,
        "actions": action_results,
    }


def _combine_role(steps):
    modify = steps["modify"]["verdict"]
    assume = steps["assume"]["verdict"]
    if modify == "allowed" and assume == "allowed":
        return "supported"
    if modify == "denied" or assume == "denied":
        return "blocked"
    return "undetermined"


def _build_context(principal_attributes, step_conditions, step_name):
    context = {str(k).lower(): str(v) for k, v in (principal_attributes or {}).items()}
    for k, v in (step_conditions or {}).get(step_name, {}).items():
        context[str(k).lower()] = str(v)
    return context


def precise_recheck(policy, role_arns, principal_attributes=None, step_conditions=None):
    """
    Precisely re-check the AttachRolePolicy/PutRolePolicy + AssumeRole
    privilege-escalation combination for a finite list of concrete roles.

    policy: a parliament Policy (analyzed, or analyzable, identity policy)
    role_arns: non-empty list of concrete role ARN strings
    principal_attributes: dict of fixed principal attribute key/values
    step_conditions: {"modify": {...}, "assume": {...}} per-step request
        condition values

    Returns a dict with a per-role status of "supported", "blocked" or
    "undetermined", plus per-step allow/deny/unknown statement sources.
    """
    if (
        not isinstance(role_arns, (list, tuple))
        or len(role_arns) == 0
        or not all(isinstance(r, str) for r in role_arns)
    ):
        raise ValueError(
            "role_arns must be a non-empty list of concrete role ARN strings"
        )

    if not policy.statements and policy.policy_json is not None:
        # Ensure statements exist; skip auditor hooks, we only need parsing.
        policy.analyze(ignore_private_auditors=True)

    roles = {}
    for role_arn in role_arns:
        steps = {}
        for step_name, actions in STEPS:
            context = _build_context(principal_attributes, step_conditions, step_name)
            action_results = {}
            for action in actions:
                action_results[action] = _eval_action(
                    policy.statements, action, role_arn, context
                )
            steps[step_name] = _combine_step(action_results)
        roles[role_arn] = {"status": _combine_role(steps), "steps": steps}

    return {"roles": roles, "scope": SCOPE_NOTE}
