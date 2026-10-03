"""Receipt-bound systemd lifecycle control. No unit installation or live activation."""
import argparse
import json
import os

from . import container_host as host

SCHEMA = "records-systemd-invocation-v1"
MODE = host.encoded({"schema": "records-systemd-managed-v1"})
FENCE = host.encoded({"schema": "records-systemd-hold-v1", "status": "held"})
BINDING = "invocation-active.json"


def _id(value):
    host.require(isinstance(value, str) and host.JOB.fullmatch(value))


def _optional(directory, name):
    try:
        return directory.read(name, 8192)[0]
    except FileNotFoundError:
        return None


def _validate(raw, profile):
    value = host.decoded(raw, 8192)
    host.require(isinstance(value, dict) and set(value) ==
                 {"schema", "invocation_id", "job_id", "profile_sha256"}
                 and value["schema"] == SCHEMA and value["profile_sha256"] == profile.sha256)
    _id(value["invocation_id"])
    _id(value["job_id"])
    return value


def _owned(journal, invocation_id):
    _id(invocation_id)
    directory = journal.directory
    raw = directory.read("invocation-" + invocation_id + ".json", 8192)[0]
    value = _validate(raw, journal.profile)
    host.require(value["invocation_id"] == invocation_id
                 and directory.read("reservation-" + value["job_id"] + ".json", 8192)[0] == raw)
    return raw, value


def _binding(journal):
    raw = journal.directory.read(BINDING, 8192)[0]
    value = _validate(raw, journal.profile)
    host.require(_owned(journal, value["invocation_id"])[0] == raw)
    return raw, value


def _exists(journal, job_id):
    try:
        os.stat(job_id, dir_fd=journal.directory.fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _closure(value):
    return host.encoded(dict(value, schema="records-systemd-not-allocated-v1",
                             status="not_allocated"))


def _closed(journal, value):
    raw = _optional(journal.directory, "closed-" + value["invocation_id"] + ".json")
    if raw is None:
        return False
    host.require(raw == _closure(value) and not _exists(journal, value["job_id"]))
    return True


def _terminal(receipt):
    return (receipt["phase"] == "finished" and not receipt["hold"] and receipt["quiescent"]
            and receipt["worker_removed"]
            and (receipt["reporter_id"] is None or receipt["reporter_removed"]))


def _prior_done(journal, value):
    if _closed(journal, value):
        return True
    receipt = journal.read(value["job_id"])
    host.require(receipt["profile_sha256"] == value["profile_sha256"])
    return _terminal(receipt)


def _mode(journal):
    raw = _optional(journal.directory, "systemd-managed.json")
    if raw is None:
        host.require(_optional(journal.directory, BINDING) is None)
        return False
    host.require(raw == MODE)
    return True


def admission(journal, requested_job_id=None, *, preassigned=False):
    """Called under allocation/admission locks; metadata never supplies a callback."""
    host.require(_optional(journal.directory, "systemd-hold.json") is None)
    if not _mode(journal):
        return
    raw, active = _binding(journal)
    reservation = (None if requested_job_id is None else
                   _optional(journal.directory, "reservation-" + requested_job_id + ".json"))
    if reservation is not None:
        value = _validate(reservation, journal.profile)
        host.require(value["job_id"] == requested_job_id and reservation == raw
                     and _owned(journal, value["invocation_id"])[0] == raw
                     and not _closed(journal, value))
        return
    host.require(not preassigned and _prior_done(journal, active))


def _fence(journal):
    """Persist a separate admission HOLD, never modify/adopt another job receipt."""
    try:
        with host.locked(journal.directory, "cancel-admit.lock", wait=65):
            with host.locked(journal.directory, "journal.lock", wait=1):
                if _optional(journal.directory, "systemd-hold.json") is None:
                    journal.directory.write("systemd-hold.json", FENCE, fresh=True)
    except BaseException:
        pass  # Unreadable/incomplete managed state also rejects admission.
    return host._output("held")


def _prepare(journal, invocation_id):
    _id(invocation_id)
    with host.locked(journal.directory, "launch.lock"):
        with host.locked(journal.directory, "journal.lock", wait=1):
            journal.profile.check()
            host.require(_optional(journal.directory, "systemd-hold.json") is None
                         and _optional(journal.directory, "invocation-" + invocation_id + ".json") is None)
            if _mode(journal):
                host.require(_prior_done(journal, _binding(journal)[1]))
            try:
                active = journal.active()
            except FileNotFoundError:
                active = None
            if active is not None:
                receipt = journal.read(active["job_id"])
                host.require(active["profile_sha256"] == journal.profile.sha256
                             and receipt["profile_sha256"] == journal.profile.sha256
                             and _terminal(receipt))
            value = {"schema": SCHEMA, "invocation_id": invocation_id,
                     "job_id": os.urandom(16).hex(), "profile_sha256": journal.profile.sha256}
            raw = host.encoded(value)
            # Mode then pending pointer: partial preparation is fail-closed.
            if _optional(journal.directory, "systemd-managed.json") is None:
                journal.directory.write("systemd-managed.json", MODE, fresh=True)
            journal.directory.write(BINDING, raw)
            journal.directory.write("invocation-" + invocation_id + ".json", raw, fresh=True)
            journal.directory.write("reservation-" + value["job_id"] + ".json", raw, fresh=True)
            return value["job_id"]


def run(profile_path, invocation_id, *, invoke=host.call_argv, containment=None):
    _id(invocation_id)
    with host.Profile(profile_path) as profile:
        journal = host.Journal(profile)
        try:
            try:
                job_id = _prepare(journal, invocation_id)
                result = host.run(profile_path, job_id=job_id, invoke=invoke, containment=containment)
                profile.check()
                return result
            except BaseException:
                return _fence(journal)
        finally:
            journal.close()


def stop_post(profile_path, invocation_id, *, invoke=host.call_argv, containment=None):
    """Finalize only this sealed invocation; no timestamp/PID ownership inference."""
    _id(invocation_id)
    with host.Profile(profile_path) as profile:
        journal = host.Journal(profile)
        try:
            try:
                with host.locked(journal.directory, "cancel-admit.lock", wait=65):
                    with host.locked(journal.directory, "journal.lock", wait=1):
                        profile.check()
                        host.require(_optional(journal.directory, "systemd-hold.json") is None
                                     and _mode(journal))
                        raw, value = _owned(journal, invocation_id)
                        if _closed(journal, value):
                            return host._output("not_allocated")
                        if not _exists(journal, value["job_id"]):
                            host.require(_binding(journal)[0] == raw)
                            try:
                                active = journal.active()
                            except FileNotFoundError:
                                active = None
                            if active is not None:
                                prior = journal.read(active["job_id"])
                                host.require(active["job_id"] != value["job_id"]
                                             and active["profile_sha256"] == profile.sha256
                                             and prior["profile_sha256"] == profile.sha256
                                             and _terminal(prior))
                            # Serialized with allocate. A late run cannot reopen this reservation.
                            journal.directory.write("closed-" + invocation_id + ".json",
                                                    _closure(value), fresh=True)
                            return host._output("not_allocated")
                        receipt = journal.read(value["job_id"])
                        host.require(receipt["profile_sha256"] == profile.sha256)
                        if not _terminal(receipt):
                            host.require(_binding(journal)[0] == raw)
                # The immutable ID guard is rechecked INSIDE serialized accepted stop,
                # including after admission.lock acquisition, not only here.
                result = host.stop(profile_path, expected_job_id=value["job_id"],
                                   invoke=invoke, containment=containment)
                if result["status"] == "held":
                    with host.locked(journal.directory, "journal.lock", wait=1):
                        active = journal.active()
                        host.require(active["job_id"] == value["job_id"]
                                     and active["profile_sha256"] == profile.sha256)
                return result
            except BaseException:
                return _fence(journal)
        finally:
            journal.close()


class Parser(argparse.ArgumentParser):
    def error(self, _):
        raise host.Rejected()


def main(arguments=None):
    try:
        parser = Parser(add_help=False)
        commands = parser.add_subparsers(dest="action", required=True, parser_class=Parser)
        for name in ("run", "stop-post"):
            command = commands.add_parser(name, add_help=False)
            command.add_argument("--profile", required=True)
            command.add_argument("--invocation-id", required=True)
        args = parser.parse_args(arguments)
        result = (run if args.action == "run" else stop_post)(args.profile, args.invocation_id)
    except BaseException:
        result = host._output("invalid_state")
    print(json.dumps(result, sort_keys=True), flush=True)
    return {"completed": 0, "postrun_failed": 10, "not_allocated": 11,
            "held": 32, "invalid_state": 30}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
