"""Shared synthetic fixture builders for the test suite."""

import os

from acl_diag.evaluator import load_inventory, load_snapshot
from acl_diag.membership import load_subject_directory
from acl_diag.models import ACE, ALLOW, DENY, MODIFY_FILE, REPLACE_ENTRY

EXAMPLE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples",
    "scenario_a",
)


def example_inputs():
    inventory = load_inventory(os.path.join(EXAMPLE_DIR, "inventory.json"))
    subjects = load_subject_directory(os.path.join(EXAMPLE_DIR, "subjects_v1.json"))
    snapshot = load_snapshot(os.path.join(EXAMPLE_DIR, "snapshot.json"))
    return inventory, subjects, snapshot


def subjects_v2():
    return load_subject_directory(os.path.join(EXAMPLE_DIR, "subjects_v2.json"))


def ace(ace_type, principal, rights, inherited, source):
    if isinstance(rights, str):
        rights = frozenset((rights,))
    else:
        rights = frozenset(rights)
    return ACE(ace_type, principal, rights, inherited, source)
