#!/usr/bin/env python3
"""CLI entry point for the temporary diagnostic probe window controller.

All state lives under ``--state-dir`` (default ``./state``) so the whole
lifecycle can be replayed offline against the simulated fleet.  Every
command prints one JSON document to stdout.

Commands:
  init-device   register a simulated device (config version + probe values)
  open          request a diagnostic probe window (version-gated)
  tick          process expired windows as of --now
  offline       mark a device offline
  reconnect     mark a device online and drain its expired windows first
  human-edit    simulate an out-of-band human change on a device
  receipt       re-deliver a previously issued device receipt (idempotent)
  status        dump windows, devices and receipts
"""

import argparse
import json
import sys

from controller import JsonStore, ProbeWindowController, SimulatedDeviceWorld


def build_parser():
    parser = argparse.ArgumentParser(
        description="Temporary diagnostic probe window controller (simulated fleet).")
    parser.add_argument("--state-dir", default="./state",
                        help="directory holding controller/device state (default ./state)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-device", help="register a simulated device")
    p.add_argument("--device", required=True)
    p.add_argument("--version", required=True, help="managed config version, e.g. v7")
    p.add_argument("--probe", action="append", default=[], metavar="NAME=VALUE",
                   help="initial probe value; repeatable")
    p.add_argument("--offline", action="store_true", help="register the device offline")

    p = sub.add_parser("open", help="request a diagnostic probe window")
    p.add_argument("--device", required=True)
    p.add_argument("--probe", required=True)
    p.add_argument("--value", required=True, help="probe value to write while open")
    p.add_argument("--work-order", required=True)
    p.add_argument("--approved-version", required=True,
                   help="device config version approved on the work order")
    p.add_argument("--start", required=True, help="ISO-8601 window start")
    p.add_argument("--end", required=True, help="ISO-8601 window end (expiry)")
    p.add_argument("--now", default=None, help="ISO-8601 current time (default: system clock)")

    p = sub.add_parser("tick", help="process expired windows")
    p.add_argument("--now", default=None)

    p = sub.add_parser("offline", help="mark a device offline")
    p.add_argument("--device", required=True)

    p = sub.add_parser("reconnect", help="device reconnects; expired windows handled first")
    p.add_argument("--device", required=True)
    p.add_argument("--now", default=None)

    p = sub.add_parser("human-edit", help="simulate an out-of-band human change")
    p.add_argument("--device", required=True)
    p.add_argument("--probe", required=True)
    p.add_argument("--value", required=True)

    p = sub.add_parser("receipt", help="re-deliver a stored device receipt")
    p.add_argument("--receipt-id", required=True)

    sub.add_parser("status", help="dump windows, devices and receipts")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    store = JsonStore(args.state_dir)
    controller = ProbeWindowController(store)
    world = SimulatedDeviceWorld(store)

    if args.command == "init-device":
        probes = {}
        for item in args.probe:
            name, sep, value = item.partition("=")
            if not sep:
                raise SystemExit("invalid --probe %r, expected NAME=VALUE" % item)
            probes[name] = value
        out = {"registered": world.register(args.device, args.version, probes,
                                            online=not args.offline)}
    elif args.command == "open":
        out = controller.open_window(
            device_id=args.device, probe=args.probe, value=args.value,
            work_order=args.work_order, approved_version=args.approved_version,
            start=args.start, end=args.end, now=args.now)
    elif args.command == "tick":
        out = {"actions": controller.process_due(now=args.now)}
    elif args.command == "offline":
        world.set_online(args.device, False)
        out = {"device": args.device, "online": False}
    elif args.command == "reconnect":
        out = {"device": args.device,
               "actions": controller.on_reconnect(args.device, now=args.now)}
    elif args.command == "human-edit":
        out = {"device": world.human_edit(args.device, args.probe, args.value)}
    elif args.command == "receipt":
        receipts = store.load("receipts.json", {})
        if args.receipt_id not in receipts:
            out = {"receipt_id": args.receipt_id, "applied": False,
                   "reason": "unknown-receipt"}
        else:
            out = controller.apply_receipt(receipts[args.receipt_id]["receipt"])
    elif args.command == "status":
        out = controller.status()
    else:  # pragma: no cover - argparse enforces a valid command
        raise SystemExit("unknown command %r" % args.command)

    json.dump(out, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
