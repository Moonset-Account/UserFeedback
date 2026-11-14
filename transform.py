"""Core transformation logic for the outbound alert JSONL converter.

Two alert sources (gateway / endpoint) each have a field policy that
declares:
  - keep:     field paths copied verbatim into the outbound record
  - replace:  string field paths replaced by an HMAC-SHA-256 codename,
              each bound to a "purpose" (e.g. "account")
  - required: field paths that must resolve to at least one value,
              otherwise the line is skipped

Field paths are dotted (``user.name``).  When a path segment resolves to
a list, the remaining path is applied to every element, so objects inside
arrays are handled by exactly the same rules as top-level objects.

Codenames are HMAC-SHA-256 keyed by trusted key material (looked up by
tenant + key version from the outbound task) over
``tenant \\0 key_version \\0 purpose \\0 value``.  The field path is NOT
part of the digest input, so the same account produces the same codename
across sources that use different field paths, as long as tenant, key
version and purpose match.  Changing tenant, key version or purpose
changes the codename.
"""

import hashlib
import hmac
from collections import namedtuple


class PolicyError(Exception):
    """Raised when a policy document is invalid."""


class LineError(Exception):
    """Raised when an input line must be skipped; str(exc) is the reason."""


Policy = namedtuple("Policy", ["source", "keep", "replace", "required"])
# keep:     list of (raw_path, segments)
# replace:  list of (segments, purpose, raw_path)
# required: list of (raw_path, segments)

CODENAME_PREFIX = "cod"
CODENAME_HEX_LEN = 24


def parse_path(path):
    """Split a dotted field path into segments, validating its shape."""
    if not isinstance(path, str) or not path.strip():
        raise PolicyError("invalid field path: %r" % (path,))
    segments = path.split(".")
    for seg in segments:
        if not seg:
            raise PolicyError("invalid field path: %r" % (path,))
    return segments


def extract(node, segments, prefix=()):
    """Yield (index_path, value) for every match of ``segments`` under node.

    Dicts are descended by key; lists are fanned out element-wise with the
    element index appended to the path.  Scalars with a remaining path
    match nothing.
    """
    if not segments:
        yield prefix, node
        return
    if isinstance(node, list):
        for index, item in enumerate(node):
            for hit in extract(item, segments, prefix + (index,)):
                yield hit
        return
    if isinstance(node, dict):
        key = segments[0]
        if key in node:
            for hit in extract(node[key], segments[1:], prefix + (key,)):
                yield hit
        return


def extract_empty_lists(node, segments, prefix=()):
    """Yield index paths of empty lists encountered while walking segments.

    Used to preserve the structure of a declared array field whose value
    is an empty list (no element values exist to extract).
    """
    if not segments:
        return
    if isinstance(node, list):
        if not node:
            yield prefix
            return
        for index, item in enumerate(node):
            for hit in extract_empty_lists(item, segments, prefix + (index,)):
                yield hit
        return
    if isinstance(node, dict):
        key = segments[0]
        if key in node:
            for hit in extract_empty_lists(node[key], segments[1:],
                                           prefix + (key,)):
                yield hit


def has_path(root, index_path):
    """True when ``index_path`` already resolves to something in ``root``."""
    node = root
    for seg in index_path:
        if isinstance(seg, int):
            if not isinstance(node, list) or seg >= len(node):
                return False
            node = node[seg]
        else:
            if not isinstance(node, dict) or seg not in node:
                return False
            node = node[seg]
    return True


def insert(root, index_path, value):
    """Place ``value`` into ``root`` at ``index_path``, building containers.

    String segments address dict keys, integer segments address list
    positions.  Missing containers are created on demand.
    """
    node = root
    for i, seg in enumerate(index_path):
        last = i == len(index_path) - 1
        nxt = None if last else (
            [] if isinstance(index_path[i + 1], int) else {}
        )
        if isinstance(seg, int):
            if not isinstance(node, list):
                raise PolicyError(
                    "conflicting paths: expected array at %r" % (index_path[:i],)
                )
            while len(node) <= seg:
                node.append(None)
            if last:
                node[seg] = value
            else:
                if node[seg] is None:
                    node[seg] = nxt
                node = node[seg]
        else:
            if not isinstance(node, dict):
                raise PolicyError(
                    "conflicting paths: expected object at %r" % (index_path[:i],)
                )
            if last:
                node[seg] = value
            else:
                if node.get(seg) is None:
                    node[seg] = nxt
                node = node[seg]


def load_policy(data):
    """Validate a policy document and return a Policy tuple."""
    if not isinstance(data, dict):
        raise PolicyError("policy must be a JSON object")
    source = data.get("source")
    if source is not None and not isinstance(source, str):
        raise PolicyError("policy 'source' must be a string")

    keep_raw = data.get("keep", [])
    replace_raw = data.get("replace", [])
    required_raw = data.get("required", [])
    for name, value in (("keep", keep_raw), ("replace", replace_raw),
                        ("required", required_raw)):
        if not isinstance(value, list):
            raise PolicyError("policy '%s' must be a list" % name)

    keep = []
    for path in keep_raw:
        keep.append((path, parse_path(path)))

    replace = []
    for rule in replace_raw:
        if not isinstance(rule, dict):
            raise PolicyError("each replace rule must be an object")
        path = rule.get("path")
        purpose = rule.get("purpose")
        if not isinstance(purpose, str) or not purpose:
            raise PolicyError("replace rule for %r needs a non-empty 'purpose'"
                              % (path,))
        replace.append((parse_path(path), purpose, path))

    required = []
    for path in required_raw:
        required.append((path, parse_path(path)))

    overlap = {raw for raw, _ in keep} & {raw for _, _, raw in replace}
    if overlap:
        raise PolicyError("paths declared both keep and replace: %s"
                          % sorted(overlap))

    return Policy(source=source, keep=keep, replace=replace, required=required)


def make_codename(key_bytes, tenant, key_version, purpose, value):
    """Derive the codename for ``value`` under the given context.

    The digest input deliberately excludes the field path: identical
    account strings under the same tenant/key-version/purpose map to the
    same codename no matter which source or path they came from.
    """
    msg = "\x00".join([tenant, key_version, purpose, value]).encode("utf-8")
    digest = hmac.new(key_bytes, msg, hashlib.sha256).hexdigest()
    return "%s_%s" % (CODENAME_PREFIX, digest[:CODENAME_HEX_LEN])


def transform_record(obj, policy, key_bytes, tenant, key_version):
    """Build one outbound record from a parsed input object.

    Returns a dict constructed ONLY from policy-declared fields.
    Raises LineError (with a stable reason string) when the line must be
    skipped: non-object line, missing required field, or a non-string
    value at a replace path.
    """
    if not isinstance(obj, dict):
        raise LineError("line is not a JSON object")

    for raw, segments in policy.required:
        for _path, _value in extract(obj, segments):
            break
        else:
            raise LineError("missing required field: %s" % raw)

    out = {}
    for raw, segments in policy.keep:
        for index_path, value in extract(obj, segments):
            insert(out, index_path, value)

    for segments, purpose, raw in policy.replace:
        for index_path, value in extract(obj, segments):
            if not isinstance(value, str):
                raise LineError("non-string value at replace path: %s" % raw)
            insert(out, index_path,
                   make_codename(key_bytes, tenant, key_version,
                                 purpose, value))

    # Preserve declared array fields whose value is an empty list.
    declared = [segments for _raw, segments in policy.keep]
    declared += [segments for segments, _purpose, _raw in policy.replace]
    for segments in declared:
        for index_path in extract_empty_lists(obj, segments):
            if not has_path(out, index_path):
                insert(out, index_path, [])
    return out
