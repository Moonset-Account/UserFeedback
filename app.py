#!/usr/bin/env python3
"""Contacts CSV importer CLI.

Usage:
    python3 app.py [--chunk-size N] [FILE]
    python3 app.py [--chunk-size N] -          # read stdin (also the default)

Reads a contacts CSV document (see importer.py for the format), optionally
in fixed-size chunks, and prints the import result as JSON:

    {
      "contacts": [{"name": ..., "email": ..., "phone": ..., "note": ...}, ...],
      "errors":   [{"record": <logical record no>, "category": ..., "message": ...}, ...]
    }

Exit codes: 0 = stream processed (row-level rejections are data, reported
in "errors"); 2 = usage error; 3 = I/O or UTF-8 decoding failure.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from importer import ContactImporter

USAGE = __doc__.strip()


def main(argv):
    args = argv[1:]
    chunk_size = 65536
    path = None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-h", "--help"):
            print(USAGE)
            return 0
        if arg == "--chunk-size":
            i += 1
            if i >= len(args):
                print("error: --chunk-size needs a value", file=sys.stderr)
                return 2
            try:
                chunk_size = int(args[i])
            except ValueError:
                print("error: --chunk-size must be an integer", file=sys.stderr)
                return 2
            if chunk_size < 1:
                print("error: --chunk-size must be >= 1", file=sys.stderr)
                return 2
        elif arg.startswith("-") and arg != "-":
            print("error: unknown option %r" % arg, file=sys.stderr)
            return 2
        else:
            if path is not None:
                print("error: more than one input file given", file=sys.stderr)
                return 2
            path = arg
        i += 1

    try:
        if path is None or path == "-":
            raw = sys.stdin.buffer.read()
            text = raw.decode("utf-8")
            chunks = (text[j:j + chunk_size] for j in range(0, len(text), chunk_size))
        else:
            with open(path, "rb") as fh:
                raw = fh.read()
            text = raw.decode("utf-8")
            chunks = (text[j:j + chunk_size] for j in range(0, len(text), chunk_size))
    except OSError as exc:
        print("error: cannot read input: %s" % exc, file=sys.stderr)
        return 3
    except UnicodeDecodeError as exc:
        print("error: input is not valid UTF-8: %s" % exc, file=sys.stderr)
        return 3

    importer = ContactImporter()
    for chunk in chunks:
        importer.feed(chunk)
    importer.close()

    json.dump(importer.result(), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
