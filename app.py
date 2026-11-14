#!/usr/bin/env python3
"""CLI entry point for the logout-notification service.

Usage:
    python3 app.py SCENARIO.json [--pretty]

The scenario file is a JSON object:

{
  "sessions": [                       # local sessions known before any event
    {"app": "A", "issuer": "idp-1", "account": "alice",
     "session_id": "s1", "login_time": 100}
  ],
  "events": [                         # verified logout events, in delivery order
    {"event_id": "e1", "issuer": "idp-1", "issued_at": 200,
     "type": "session", "session_id": "s1"},
    {"event_id": "e2", "issuer": "idp-1", "issued_at": 210,
     "type": "account", "account": "alice"}
  ],
  "adapter": {                        # optional failure simulation
    "timeout_apps": ["B"],            # these apps always time out
    "failures": {"B": 1}              # app -> number of transient timeouts
  },
  "relogins": [                       # optional re-logins after events, before retry
    {"app": "B", "issuer": "idp-1", "account": "alice",
     "session_id": "s9", "login_time": 300}
  ],
  "retry": true                       # optional: retry all pending purges last
}

Output (stdout, JSON):
{
  "events": [{"event_id": ..., "duplicate": bool}, ...],
  "purges": [ <purge detail per processed event, see models.PurgeRecord> ],
  "retried": [ <event_ids retried> ],
  "active_sessions": [ ... ],
  "adapter_calls": [ ... ]
}
"""

import json
import sys

from adapters import ControllableAppAdapter
from models import LogoutEvent
from service import EventValidationError, LogoutService


def build_event(raw):
    try:
        return LogoutEvent(
            event_id=raw["event_id"],
            issuer=raw["issuer"],
            issued_at=raw["issued_at"],
            event_type=raw["type"],
            session_id=raw.get("session_id"),
            account=raw.get("account"),
        )
    except KeyError as exc:
        raise EventValidationError("event is missing field %s" % exc)


def run_scenario(scenario):
    adapter = ControllableAppAdapter()
    adapter_cfg = scenario.get("adapter", {})
    for app in adapter_cfg.get("timeout_apps", []):
        adapter.set_timeout(app, True)
    for app, count in adapter_cfg.get("failures", {}).items():
        adapter.set_failures(app, count)

    service = LogoutService(adapter)

    for raw in scenario.get("sessions", []):
        service.record_login(
            app=raw["app"],
            issuer=raw["issuer"],
            account=raw["account"],
            session_id=raw["session_id"],
            login_time=raw["login_time"],
        )

    event_results = []
    for raw in scenario.get("events", []):
        event = build_event(raw)
        purge, is_duplicate = service.process_event(event)
        event_results.append(
            {
                "event_id": event.event_id,
                "duplicate": is_duplicate,
                "purge_status": purge.status,
            }
        )

    for raw in scenario.get("relogins", []):
        adapter.simulate_relogin(
            service,
            app=raw["app"],
            issuer=raw["issuer"],
            account=raw["account"],
            session_id=raw["session_id"],
            login_time=raw["login_time"],
        )

    retried = []
    if scenario.get("retry"):
        retried = service.retry_all_pending()

    return {
        "events": event_results,
        "purges": service.purge_details(),
        "retried": retried,
        "active_sessions": [s.to_dict() for s in service.active_sessions()],
        "adapter_calls": list(adapter.calls),
    }


def main(argv):
    if len(argv) < 2:
        sys.stderr.write(__doc__)
        return 2
    pretty = "--pretty" in argv[2:]
    try:
        with open(argv[1], "r", encoding="utf-8") as handle:
            scenario = json.load(handle)
    except (OSError, ValueError) as exc:
        sys.stderr.write("error: cannot read scenario: %s\n" % exc)
        return 2
    try:
        result = run_scenario(scenario)
    except (EventValidationError, KeyError, TypeError) as exc:
        sys.stderr.write("error: invalid scenario: %s\n" % exc)
        return 2
    json.dump(result, sys.stdout, indent=2 if pretty else None, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
