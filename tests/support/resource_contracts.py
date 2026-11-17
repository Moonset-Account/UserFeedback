"""Shared support for the resource-derivation combinatorial tests.

This module intentionally implements its own *small, independent* domain model
for a finite set of S3 bucket/object resources.  The oracle below mirrors only
the resource-derivation contract that Parliament's current implementation
documents/implements (see ``parliament/policy.py::Policy.get_allowed_resources``
and ``parliament/statement.py::Statement.get_resources_for_privilege``):

* Statements are grouped per *exact resource string*; the library does not
  intersect arbitrary ARN glob patterns.
* A resource is allowed only when at least one relevant statement has
  Effect=Allow and no relevant unconditional (Condition-less) Deny names the
  same resource string.
* An unconditional Deny on the bare "*" resource short-circuits the whole
  result to an empty list.
* A Deny carrying a Condition is not treated as a negation that holds in every
  context, so it does not suppress Allows.

The model is deliberately limited to concrete resource ARNs plus the bare
"*".  Anything beyond that (intersecting two ARN glob patterns, NotResource,
NotAction, policy variables) is explicitly outside the promised contract and
is covered as an uncommitted boundary in the tests instead.
"""

import copy
import fnmatch
import json


# ---------------------------------------------------------------------------
# Finite S3 domain: two buckets, two object resources each.  Names are
# synthetic placeholders only - no real account or bucket data is used.
# ---------------------------------------------------------------------------
BUCKETS = ["b-alpha", "b-beta"]
OBJECTS = [
    "arn:aws:s3:::b-alpha/logs/a.txt",
    "arn:aws:s3:::b-alpha/logs/a2.txt",
    "arn:aws:s3:::b-beta/data/b.csv",
    "arn:aws:s3:::b-beta/data/b2.csv",
]
BUCKET_ARNS = ["arn:aws:s3:::{}".format(b) for b in BUCKETS]
ALL_CONCRETE = sorted(OBJECTS + BUCKET_ARNS)

# The privilege under analysis.  Per the bundled iam_definition.json,
# s3:GetObject applies to the "object*" resource type (and the empty "*"
# resource type), never to bucket ARNs without a slash.
PRIVILEGE_PREFIX = "s3"
PRIVILEGE_NAME = "GetObject"

# Action representations exercised by the combinatorial tests.  All of these
# cover s3:GetObject in the current action-expansion table; they differ only in
# how explicitly they name it.
EXPLICIT_ACTION = "s3:GetObject"
WILDCARD_SERVICE_ACTION = "s3:*"
WILDCARD_GLOB_ACTION = "s3:Get*"
WILDCARD_EVERYTHING_ACTION = "*"
ACTIONS_COVERING_GETOBJECT = [
    EXPLICIT_ACTION,
    WILDCARD_SERVICE_ACTION,
    WILDCARD_GLOB_ACTION,
    WILDCARD_EVERYTHING_ACTION,
]

MFA_CONDITION = {"Bool": {"aws:MultiFactorAuthPresent": "true"}}


class DStmt(object):
    """A tiny record describing one statement of the finite-domain policies.

    ``resources`` is a plain Python list of resource strings.  A statement
    using the single-element list form and one using a scalar are the same
    statement logically; builders in the test module render both shapes.
    """

    def __init__(self, sid, effect, action, resources, condition=None):
        self.sid = sid
        self.effect = effect
        self.action = action
        self.resources = list(resources)
        self.condition = condition

    def is_deny(self):
        return self.effect == "Deny"

    def is_allow(self):
        return self.effect == "Allow"

    def has_condition(self):
        return self.condition is not None


# ---------------------------------------------------------------------------
# Independent action-coverage rule.  Kept deliberately simple and separate
# from parliament.expand_action: the test vocabulary is fixed and finite.
# ---------------------------------------------------------------------------
def oracle_action_covers(action, privilege_prefix, privilege_name):
    if action == "*" or action == "*:*":
        return True
    if ":" not in action:
        return False
    prefix, fragment = action.split(":", 1)
    if prefix != privilege_prefix:
        return False
    if fragment == "*":
        return True
    return fnmatch.fnmatchcase(privilege_name.lower(), fragment.lower())


def render_policy(dstmts, scalar_action=False, scalar_resource=False):
    """Render DStmt records into a JSON policy string Parliament can analyze.

    scalar_action / scalar_resource force the single-element scalar JSON form
    so the tests can prove both representations derive identical resources.
    """
    rendered = []
    for index, d in enumerate(dstmts):
        action_value = d.action
        if scalar_action:
            action_field = action_value
        else:
            action_field = [action_value]

        if scalar_resource and len(d.resources) == 1:
            resource_field = d.resources[0]
        else:
            resource_field = list(d.resources)

        stmt = {
            "Sid": d.sid,
            "Effect": d.effect,
            "Action": action_field,
            "Resource": resource_field,
        }
        if d.condition is not None:
            stmt["Condition"] = copy.deepcopy(d.condition)
        rendered.append(stmt)

    return json.dumps(
        {"Version": "2012-10-17", "Statement": rendered},
        indent=2,
        sort_keys=False,
    )


# ---------------------------------------------------------------------------
# Independent oracle for the finite domain.  Input is a list[DStmt]; the
# output is the expected set of allowed concrete resource strings plus the
# expected early-empty ("*" deny) behavior.
# ---------------------------------------------------------------------------
def oracle_allowed_resources(
    dstmts, privilege_prefix=PRIVILEGE_PREFIX, privilege_name=PRIVILEGE_NAME
):
    """Return ``(allowed_set, star_unconditional_deny)``.

    ``allowed_set`` contains the concrete resource strings the implementation
    should report.  ``star_unconditional_deny`` is True when a Condition-less
    Deny references "*", in which case Parliament returns [] regardless of any
    Allow statements.
    """
    allow_resources = set()
    denied_resources = set()
    star_unconditional_deny = False
    star_conditional_deny = False

    for d in dstmts:
        if not oracle_action_covers(
            d.action, privilege_prefix, privilege_name
        ):
            continue
        for resource in d.resources:
            if resource == "*":
                if d.is_deny() and not d.has_condition():
                    star_unconditional_deny = True
                elif d.is_deny() and d.has_condition():
                    star_conditional_deny = True
                elif d.is_allow():
                    # An Allow on "*" is not a concrete domain member; the
                    # oracle only predicts concrete resource strings.
                    continue
                continue
            if d.is_allow():
                allow_resources.add(resource)
            elif d.is_deny() and not d.has_condition():
                denied_resources.add(resource)
            # A conditional Deny on a concrete resource is intentionally not
            # treated as a universal negation.

    if star_unconditional_deny:
        return set(), True

    allowed = allow_resources - denied_resources
    # The second element reports only whether a *Condition-less* Deny on "*"
    # forces an early empty result.  A conditional Deny on "*" does not, so the
    # flag remains False here even when such a statement is present.
    return allowed, False


def oracle_references(
    dstmts, privilege_prefix=PRIVILEGE_PREFIX, privilege_name=PRIVILEGE_NAME
):
    """Predict the resource -> statement-index grouping used by get_references.

    Only exact resource strings are keys - mirroring the implementation's
    dict keyed on the literal resource text.  Returns {resource: [sid, ...]}.
    """
    refs = {}
    for d in dstmts:
        if not oracle_action_covers(
            d.action, privilege_prefix, privilege_name
        ):
            continue
        for resource in d.resources:
            refs.setdefault(resource, []).append(d.sid)
    return refs


# ---------------------------------------------------------------------------
# Deterministic statement-level ddmin reducer.
#
# The "failure sample" here is a synthetic over-permissive-looking policy
# containing an unconditional s3:GetObject Deny on one concrete object while
# sibling objects remain allowed, plus several unrelated statements.  Reducing
# it must keep (a) the same S3 *object* resource type and (b) the same
# unconditional-deny relationship, while dropping statements that are not
# needed to exhibit it.
# ---------------------------------------------------------------------------
def _property_holds(dstmts, target_resource):
    """The property the reducer must preserve through every reduction."""
    allowed, star_deny = oracle_allowed_resources(dstmts)
    if star_deny:
        # A bare-* deny changes the shape of the relationship; the target
        # property is specifically a concrete object-level deny.
        return False
    has_unconditional_object_deny = any(
        d.is_deny()
        and not d.has_condition()
        and target_resource in d.resources
        and all(r != "*" for r in d.resources)
        for d in dstmts
    )
    if not has_unconditional_object_deny:
        return False
    # The deny must still leave at least one sibling object allowed, i.e. the
    # reduced policy exhibits "deny one object, allow the rest".
    sibling_objects = [o for o in OBJECTS if o != target_resource]
    if not any(o in allowed for o in sibling_objects):
        return False
    # Every retained resource that looks like an S3 object ARN must stay an
    # object ARN: resource types may not silently change during reduction.
    for d in dstmts:
        for resource in d.resources:
            if resource.startswith("arn:aws:s3:::") and "/" not in resource:
                return False
    return True


def ddmin_statements(dstmts, target_resource):
    """Classic ddmin over statement indices, deterministic given the input.

    Returns the reduced list[DStmt].  The relative order of the surviving
    statements is the same as in the original policy (no reordering).
    """
    current = list(dstmts)
    if not _property_holds(current, target_resource):
        raise ValueError("Initial policy does not satisfy the reduction property")

    n = 2
    while len(current) >= 2:
        chunk_size = len(current) // n
        if chunk_size == 0:
            break
        reduced_further = False
        for i in range(n):
            start = i * chunk_size
            end = len(current) if i == n - 1 else start + chunk_size
            candidate = current[:start] + current[end:]
            if _property_holds(candidate, target_resource):
                current = candidate
                n = max(n - 1, 2)
                reduced_further = True
                break
        if not reduced_further:
            if n >= len(current):
                break
            n = min(n * 2, len(current))

    # Explicit 1-minimality check: removing any single remaining statement must
    # break the property, otherwise the ddmin loop missed a reduction.
    for i in range(len(current)):
        candidate = current[:i] + current[i + 1 :]
        if _property_holds(candidate, target_resource):
            raise AssertionError(
                "Reducer output is not 1-minimal: statement {} is removable".format(
                    current[i].sid
                )
            )
    return current


def build_reduction_input(target_resource):
    """The synthetic, reducible failure sample (statement order is fixed)."""
    sibling = next(o for o in OBJECTS if o != target_resource)
    other_sibling = next(
        o for o in OBJECTS if o not in (target_resource, sibling)
    )
    return [
        DStmt(
            "BroadAllow",
            "Allow",
            EXPLICIT_ACTION,
            [target_resource, sibling, other_sibling],
        ),
        DStmt(
            "UnrelatedQueue",
            "Allow",
            "sqs:SendMessage",
            ["arn:aws:sqs:us-east-1:123456789012:q-one"],
        ),
        DStmt(
            "UnrelatedObjectAllow",
            "Allow",
            EXPLICIT_ACTION,
            ["arn:aws:s3:::b-gamma/scratch/c.txt"],
        ),
        DStmt(
            "TargetDeny",
            "Deny",
            EXPLICIT_ACTION,
            [target_resource],
        ),
        DStmt(
            "SecondObjectDeny",
            "Deny",
            EXPLICIT_ACTION,
            [other_sibling],
        ),
        DStmt(
            "ConditionalSiblingDeny",
            "Deny",
            EXPLICIT_ACTION,
            [sibling],
            condition=MFA_CONDITION,
        ),
        DStmt(
            "UnrelatedWildcardAllow",
            "Allow",
            WILDCARD_SERVICE_ACTION,
            ["*"],
        ),
    ]
