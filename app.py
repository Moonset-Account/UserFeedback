#!/usr/bin/env python3
"""CLI entry point for the secure help-center draft boundary.

Callers pass --root, --workspace and a logical document ID; the trusted
per-workspace index locates the file.  Disk paths are never accepted.

Exit codes:
    0  success
    1  unexpected error
    2  unknown workspace / document
    3  forbidden (not a workspace member / not the language editor)
    4  version conflict
    5  unsafe path (symlink component or non-regular target)
    6  stale reference (index was reloaded)
    64 usage error
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import secure_docs

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNKNOWN = 2
EXIT_FORBIDDEN = 3
EXIT_CONFLICT = 4
EXIT_UNSAFE = 5
EXIT_STALE = 6
EXIT_USAGE = 64

_ERROR_EXIT_CODES = (
    (secure_docs.UnknownWorkspaceError, EXIT_UNKNOWN),
    (secure_docs.UnknownDocumentError, EXIT_UNKNOWN),
    (secure_docs.NotWorkspaceMemberError, EXIT_FORBIDDEN),
    (secure_docs.NotLanguageEditorError, EXIT_FORBIDDEN),
    (secure_docs.VersionConflictError, EXIT_CONFLICT),
    (secure_docs.UnsafePathError, EXIT_UNSAFE),
    (secure_docs.StaleReferenceError, EXIT_STALE),
)


def _emit(payload):
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _fail(exc):
    _emit({"ok": False, "error": type(exc).__name__, "message": str(exc)})
    for cls, code in _ERROR_EXIT_CODES:
        if isinstance(exc, cls):
            return code
    return EXIT_ERROR


def _build_store(args):
    users = secure_docs.UserDirectory.from_file(
        os.path.join(args.root, "users.json"))
    return secure_docs.SecureDocStore(args.root, users)


def cmd_read(args):
    store = _build_store(args)
    try:
        handle = store.open_document(args.user, args.workspace, args.doc)
        content = handle.read(args.user)
        version = handle.current_version()
    except secure_docs.SecureDocsError as exc:
        return _fail(exc)
    _emit({
        "ok": True,
        "workspace": args.workspace,
        "doc": args.doc,
        "language": handle.language(),
        "version": version,
        "content": content,
    })
    return EXIT_OK


def cmd_write(args):
    store = _build_store(args)
    if args.content_file is not None:
        with open(args.content_file, "r", encoding="utf-8") as fh:
            content = fh.read()
    else:
        content = args.content if args.content is not None else ""
    try:
        handle = store.open_document(args.user, args.workspace, args.doc)
        new_version = handle.write(args.user, content, args.expected_version)
    except secure_docs.SecureDocsError as exc:
        return _fail(exc)
    _emit({
        "ok": True,
        "workspace": args.workspace,
        "doc": args.doc,
        "version": new_version,
    })
    return EXIT_OK


def cmd_reload(args):
    store = _build_store(args)
    try:
        index = store.reload_index(args.workspace)
    except secure_docs.SecureDocsError as exc:
        return _fail(exc)
    _emit({
        "ok": True,
        "workspace": args.workspace,
        "generation": index.generation,
        "documents": sorted(index.documents),
    })
    return EXIT_OK


def build_parser():
    parser = argparse.ArgumentParser(
        prog="app.py",
        description="Secure help-center draft boundary "
                    "(workspace + logical document ID, no disk paths).")
    parser.add_argument("--root", required=True,
                        help="directory that holds users.json and workspaces")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p, need_doc=True):
        p.add_argument("--workspace", required=True)
        p.add_argument("--user", required=True)
        if need_doc:
            p.add_argument("--doc", required=True,
                           help="logical document ID from the index")

    p_read = sub.add_parser("read", help="read a draft (membership required)")
    add_common(p_read)
    p_read.set_defaults(func=cmd_read)

    p_write = sub.add_parser(
        "write", help="write a draft (language editor + expected version)")
    add_common(p_write)
    p_write.add_argument("--expected-version", type=int, required=True)
    p_write.add_argument("--content", default=None)
    p_write.add_argument("--content-file", default=None)
    p_write.set_defaults(func=cmd_write)

    p_reload = sub.add_parser("reload", help="reload a workspace index "
                                             "(invalidates old handles)")
    p_reload.add_argument("--workspace", required=True)
    p_reload.add_argument("--user", default="system")
    p_reload.set_defaults(func=cmd_reload)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
