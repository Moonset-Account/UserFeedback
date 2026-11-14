"""Temporary diagnostic probe window controller over a simulated device fleet.

A work order may ask to open a readable diagnostic probe on a device for a
bounded time window.  The controller:

* only opens the probe when the device's current config version equals the
  version approved on the work order;
* records the probe's original value and the unique written version token;
* on expiry restores the original value only while the device still carries
  this window's written version token, otherwise escalates to manual handling;
* defers restore while a device is offline and processes already-expired
  windows first when the device reconnects;
* persists every window so a controller restart recovers unclosed windows;
* applies each device receipt at most once (duplicate receipts are no-ops).

Only the Python 3 standard library is used.  All state lives in a JSON state
directory so the whole flow can be driven offline by tests and the CLI.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone

# Window states.
OPENING = "OPENING"                    # write issued, waiting for device ack
OPEN = "OPEN"                          # probe open inside its window
RESTORING = "RESTORING"                # restore write issued, waiting for ack
RESTORE_PENDING = "RESTORE_PENDING"    # expired but device unreachable
CLOSED_RESTORED = "CLOSED_RESTORED"    # original value restored (terminal)
ESCALATED = "ESCALATED"                # human change detected (terminal)

TERMINAL_STATES = (CLOSED_RESTORED, ESCALATED)


def parse_ts(value):
    """Parse a naive ISO-8601 timestamp (seconds precision is enough)."""
    return datetime.fromisoformat(value)


def now_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat()


class DeviceOfflineError(Exception):
    """Raised when a write is attempted against an offline device."""


class UnknownDeviceError(Exception):
    """Raised when a device id is not registered in the simulated fleet."""


class JsonStore:
    """Tiny atomic JSON document store rooted at a state directory."""

    def __init__(self, state_dir):
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)

    def path(self, name):
        return os.path.join(self.state_dir, name)

    def load(self, name, default):
        path = self.path(name)
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def save(self, name, data):
        fd, tmp = tempfile.mkstemp(dir=self.state_dir, prefix=".tmp-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
            os.replace(tmp, self.path(name))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


class SimulatedDeviceWorld:
    """File-backed simulated fleet.

    Each device record looks like::

        {
          "device_id": "dev-1",
          "online": true,
          "version": "v7",              # managed config baseline version
          "seq": 0,                     # human-edit counter
          "probes": {"temp": "20"},     # current probe values
          "marks":  {"temp": "managed-win-..."}  # per-probe written version token
        }

    Controller writes never touch ``version``; only simulated human edits do
    (``human-<n>``).  Controller writes stamp ``marks[probe]`` with the
    window's unique written-version token so later tampering is detectable.
    """

    def __init__(self, store):
        self.store = store

    def _load(self):
        return self.store.load("devices.json", {})

    def _save(self, devices):
        self.store.save("devices.json", devices)

    def register(self, device_id, version, probes=None, online=True):
        devices = self._load()
        devices[device_id] = {
            "device_id": device_id,
            "online": bool(online),
            "version": version,
            "seq": 0,
            "probes": dict(probes or {}),
            "marks": {},
        }
        self._save(devices)
        return dict(devices[device_id])

    def get(self, device_id):
        devices = self._load()
        if device_id not in devices:
            raise UnknownDeviceError(device_id)
        return dict(devices[device_id])

    def set_online(self, device_id, online):
        devices = self._load()
        if device_id not in devices:
            raise UnknownDeviceError(device_id)
        devices[device_id]["online"] = bool(online)
        self._save(devices)

    def write_probe(self, device_id, probe, value, mark):
        """Controller write: set probe value and its written-version token."""
        devices = self._load()
        if device_id not in devices:
            raise UnknownDeviceError(device_id)
        device = devices[device_id]
        if not device["online"]:
            raise DeviceOfflineError(device_id)
        device["probes"][probe] = value
        device["marks"][probe] = mark
        self._save(devices)

    def human_edit(self, device_id, probe, value):
        """Simulate an out-of-band human change on the device.

        Bumps the device config version to ``human-<n>`` and re-marks the
        probe, so any open window on this device can detect the interference.
        """
        devices = self._load()
        if device_id not in devices:
            raise UnknownDeviceError(device_id)
        device = devices[device_id]
        device["seq"] = int(device.get("seq", 0)) + 1
        token = "human-{}".format(device["seq"])
        device["probes"][probe] = value
        device["marks"][probe] = token
        device["version"] = token
        self._save(devices)
        return dict(device)


class ProbeWindowController:
    """State machine for temporary diagnostic probe windows."""

    def __init__(self, store):
        self.store = store
        self.world = SimulatedDeviceWorld(store)

    # -- persistence helpers -------------------------------------------------

    def _windows(self):
        return self.store.load("windows.json", {})

    def _save_windows(self, windows):
        self.store.save("windows.json", windows)

    def _receipts(self):
        return self.store.load("receipts.json", {})

    def _save_receipts(self, receipts):
        self.store.save("receipts.json", receipts)

    def _log_request(self, entry):
        log = self.store.load("requests_log.json", [])
        log.append(entry)
        self.store.save("requests_log.json", log)

    @staticmethod
    def _slug(text):
        return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(text))

    @staticmethod
    def _transition(window, to_state, ts, reason, receipt_id=None):
        entry = {"ts": ts, "from": window["state"], "to": to_state, "reason": reason}
        if receipt_id:
            entry["receipt_id"] = receipt_id
        window["history"].append(entry)
        window["state"] = to_state

    def _window_id(self, windows, device_id, probe, work_order, start, end):
        base = "win-{}-{}-{}".format(
            self._slug(work_order), self._slug(device_id), self._slug(probe))
        existing = windows.get(base)
        if existing is not None and existing["state"] not in TERMINAL_STATES:
            # A live window already covers this device/probe/work-order.
            return None
        if existing is None:
            return base
        # The previous window with this id is closed; derive a unique suffix.
        suffix = hashlib.sha1(
            "{}|{}|{}".format(base, start, end).encode("utf-8")).hexdigest()[:8]
        candidate = "{}-{}".format(base, suffix)
        while candidate in windows:
            suffix = hashlib.sha1(
                "{}|{}".format(candidate, len(windows)).encode("utf-8")).hexdigest()[:8]
            candidate = "{}-{}".format(base, suffix)
        return candidate

    # -- open ----------------------------------------------------------------

    def _reject(self, request, reason, now, extra=None):
        entry = dict(request)
        entry.update({"accepted": False, "reason": reason, "ts": now})
        if extra:
            entry.update(extra)
        self._log_request(entry)
        result = {"accepted": False, "reason": reason}
        if extra:
            result.update(extra)
        return result

    def open_window(self, device_id, probe, value, work_order,
                    approved_version, start, end, now=None):
        """Try to open a diagnostic probe window.

        Only succeeds when the device is online and its current config
        version equals ``approved_version``.  On success the original probe
        value and the unique written-version token are recorded on the
        window, the probe is written on the device, and the window becomes
        OPEN once the device receipt is applied.
        """
        now = now or now_iso()
        request = {
            "device_id": device_id, "probe": probe, "value": value,
            "work_order": work_order, "approved_version": approved_version,
            "start": start, "end": end,
        }
        if parse_ts(end) <= parse_ts(start):
            return self._reject(request, "invalid-window", now)
        try:
            device = self.world.get(device_id)
        except UnknownDeviceError:
            return self._reject(request, "unknown-device", now)
        if not device["online"]:
            return self._reject(request, "device-offline", now)
        if device["version"] != approved_version:
            return self._reject(request, "version-mismatch", now,
                                extra={"actual_version": device["version"]})

        windows = self._windows()
        window_id = self._window_id(windows, device_id, probe, work_order, start, end)
        if window_id is None:
            return self._reject(request, "window-already-open", now)

        written_version = "managed-{}".format(window_id)
        window = {
            "window_id": window_id,
            "device_id": device_id,
            "probe": probe,
            "value": value,
            "work_order": work_order,
            "approved_version": approved_version,
            "original_value": device["probes"].get(probe),
            "original_mark": device["marks"].get(probe),
            "written_version": written_version,
            "start": start,
            "end": end,
            "state": OPENING,
            "history": [{
                "ts": now, "from": None, "to": OPENING,
                "reason": "request-approved", "work_order": work_order,
            }],
        }
        receipt = {
            "receipt_id": "rcpt-{}-open".format(window_id),
            "kind": "open-ack",
            "window_id": window_id,
            "device_id": device_id,
            "probe": probe,
        }
        # Persist the window before touching the device (write-ahead), then
        # issue the device write and confirm it through the receipt path.
        windows[window_id] = window
        self._save_windows(windows)
        self.world.write_probe(device_id, probe, value, written_version)
        ack = self.apply_receipt(receipt, now=now)
        self._log_request(dict(request, accepted=True, window_id=window_id, ts=now))
        return {
            "accepted": True,
            "window": self._windows()[window_id],
            "receipt": ack,
        }

    # -- receipts (idempotent) ------------------------------------------------

    def apply_receipt(self, receipt, now=None):
        """Apply a device receipt at most once.

        The first delivery drives the state migration; any duplicate of the
        same ``receipt_id`` is reported as a no-op and never causes a second
        transition.
        """
        now = now or now_iso()
        receipts = self._receipts()
        receipt_id = receipt["receipt_id"]
        if receipt_id in receipts and receipts[receipt_id].get("processed"):
            return {"receipt_id": receipt_id, "applied": False, "duplicate": True}

        receipts[receipt_id] = {"receipt": dict(receipt), "processed": False}
        windows = self._windows()
        window = windows.get(receipt.get("window_id"))
        result = {"receipt_id": receipt_id, "applied": False}
        if window is None:
            result["reason"] = "unknown-window"
        else:
            kind = receipt.get("kind")
            if kind == "open-ack" and window["state"] == OPENING:
                self._transition(window, OPEN, now, "device-ack-open", receipt_id)
                result.update(applied=True, transition="{}->{}".format(OPENING, OPEN))
            elif kind == "restore-ack" and window["state"] == RESTORING:
                self._transition(window, CLOSED_RESTORED, now,
                                 "device-ack-restore", receipt_id)
                result.update(applied=True,
                              transition="{}->{}".format(RESTORING, CLOSED_RESTORED))
            else:
                result["reason"] = "stale-receipt"
            windows[window["window_id"]] = window
            self._save_windows(windows)
        receipts[receipt_id]["processed"] = True
        self._save_receipts(receipts)
        return result

    # -- expiry / restore ------------------------------------------------------

    def process_due(self, now=None, device_id=None):
        """Process expired windows, earliest expiry first.

        Optionally restricted to one device (used on reconnect so the
        reconnected device's already-expired windows are handled first).
        """
        now = now or now_iso()
        windows = self._windows()
        due = [w for w in windows.values()
               if w["state"] in (OPEN, RESTORE_PENDING)
               and parse_ts(w["end"]) <= parse_ts(now)
               and (device_id is None or w["device_id"] == device_id)]
        due.sort(key=lambda w: (parse_ts(w["end"]), w["window_id"]))
        actions = []
        for window in due:
            actions.append(self._expire_window(window["window_id"], now))
        return actions

    def _expire_window(self, window_id, now):
        windows = self._windows()
        window = windows[window_id]
        device = self.world.get(window["device_id"])

        if not device["online"]:
            if window["state"] != RESTORE_PENDING:
                self._transition(window, RESTORE_PENDING, now,
                                 "device-offline-await-restore")
                windows[window_id] = window
                self._save_windows(windows)
            return {"window_id": window_id, "action": "deferred-offline",
                    "state": window["state"]}

        version_intact = (
            device["version"] == window["approved_version"]
            and device["marks"].get(window["probe"]) == window["written_version"]
        )
        if version_intact:
            # Device still carries this window's written version: restore the
            # original value and confirm through the receipt path.
            self.world.write_probe(window["device_id"], window["probe"],
                                   window["original_value"],
                                   window["original_mark"])
            self._transition(window, RESTORING, now, "restore-write-issued")
            windows[window_id] = window
            self._save_windows(windows)
            receipt = {
                "receipt_id": "rcpt-{}-restore".format(window_id),
                "kind": "restore-ack",
                "window_id": window_id,
                "device_id": window["device_id"],
                "probe": window["probe"],
            }
            self.apply_receipt(receipt, now=now)
            final = self._windows()[window_id]
            return {"window_id": window_id, "action": "restored",
                    "state": final["state"]}

        # Someone touched the device after our write: hand over to a human.
        self._transition(window, ESCALATED, now, "version-changed-externally")
        windows[window_id] = window
        self._save_windows(windows)
        return {
            "window_id": window_id,
            "action": "escalated",
            "state": ESCALATED,
            "expected_version": window["written_version"],
            "current_mark": device["marks"].get(window["probe"]),
            "current_device_version": device["version"],
        }

    # -- reconnect -------------------------------------------------------------

    def on_reconnect(self, device_id, now=None):
        """Mark a device online and drain its already-expired windows first."""
        now = now or now_iso()
        self.world.set_online(device_id, True)
        return self.process_due(now=now, device_id=device_id)

    # -- introspection ---------------------------------------------------------

    def status(self):
        return {
            "windows": self._windows(),
            "devices": self.store.load("devices.json", {}),
            "receipts": self._receipts(),
        }
