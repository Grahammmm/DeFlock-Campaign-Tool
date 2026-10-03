"""Receipt-bound systemd lifecycle control. No unit installation or live activation."""
import argparse
import json
import os

from . import container_host as host

SCHEMA = "records-systemd-invocation-v1"
MODE = host.encoded({"schema": "records-systemd-managed-v1"})
FENCE = host.encoded({"schema": "records-systemd-hold-v1", "status": "held"})
BINDING = "invocation-active.json"
OPERATION = "systemd-operation.json"


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


def _operation(journal):
    raw = _optional(journal.directory, OPERATION)
    if raw is None:
        return None
    value = host.decoded(raw, 8192)
    host.require(isinstance(value, dict) and set(value) ==
                 {"schema", "purpose", "invocation_id", "profile_sha256"}
                 and value["schema"] == "records-systemd-operation-v1"
                 and value["purpose"] in {"run", "stop", "hold"}
                 and value["profile_sha256"] == journal.profile.sha256)
    _id(value["invocation_id"])
    return value


def _intent(journal, invocation_id, purpose):
    return host.encoded({"schema": "records-systemd-operation-v1", "purpose": purpose,
                         "invocation_id": invocation_id,
                         "profile_sha256": journal.profile.sha256})


def _begin_locked(journal, invocation_id, purpose):
    """Deny-first durable intent, before any fallible ownership validation."""
    host.require(_optional(journal.directory, "systemd-hold.json") is None)
    pending = _operation(journal)
    if pending is not None:
        host.require(purpose == "stop")
        if pending["invocation_id"] != invocation_id:
            # A proven older terminal invocation is a read-only no-op. It cannot
            # replace or resolve the current invocation's admission fence.
            _, value = _owned(journal, invocation_id)
            host.require(_prior_done(journal, value))
            return
        host.require(pending["purpose"] in {"run", "stop"})
    journal.directory.write(OPERATION, _intent(journal, invocation_id, purpose),
                            fresh=pending is None)


def _finish_operation(journal, invocation_id, purpose):
    with host.locked(journal.directory, "cancel-admit.lock", wait=65):
        with host.locked(journal.directory, "journal.lock", wait=1):
            journal.profile.check()
            _, value = _owned(journal, invocation_id)
            host.require(_prior_done(journal, value))
            pending = _operation(journal)
            if pending is None or pending["invocation_id"] != invocation_id:
                return
            if purpose == "run" and pending["purpose"] == "stop":
                return  # The independent matching stop still owns its fence.
            host.require(pending["purpose"] == purpose)
            os.unlink(OPERATION, dir_fd=journal.directory.fd)
            os.fsync(journal.directory.fd)


def admission(journal, requested_job_id=None, *, preassigned=False):
    """Every managed allocation/launch needs its sealed current operation."""
    host.require(_optional(journal.directory, "systemd-hold.json") is None)
    pending = _operation(journal)
    if not _mode(journal):
        host.require(pending is None)
        return
    raw, active = _binding(journal)
    host.require(requested_job_id is not None and pending is not None
                 and pending["purpose"] in {"run", "stop"})
    reservation = _optional(journal.directory, "reservation-" + requested_job_id + ".json")
    host.require(reservation is not None)
    value = _validate(reservation, journal.profile)
    host.require(value["job_id"] == requested_job_id and reservation == raw
                 and _owned(journal, value["invocation_id"])[0] == raw
                 and pending["invocation_id"] == value["invocation_id"]
                 and not _closed(journal, value))
    if pending["purpose"] == "stop":
        receipt = journal.read(requested_job_id)
        host.require(receipt["phase"] == "postrun" and receipt["cancel_requested"]
                     and receipt["quiescent"] and receipt["worker_removed"])
    # In managed mode there is NO _prior_done fallback for ordinary host.run().
    # A stop intent can authorize only the matching contained reporter, never
    # allocation or worker admission. Unmanaged host behavior is unchanged.


def _fence(journal, invocation_id):
    """Keep deny-first intent even if the secondary HOLD write fails."""
    try:
        with host.locked(journal.directory, "cancel-admit.lock", wait=65):
            with host.locked(journal.directory, "journal.lock", wait=1):
                if _optional(journal.directory, OPERATION) is None:
                    journal.directory.write(OPERATION, _intent(journal, invocation_id, "hold"), fresh=True)
                if _optional(journal.directory, "systemd-hold.json") is None:
                    journal.directory.write("systemd-hold.json", FENCE, fresh=True)
    except BaseException:
        pass  # Never clear an earlier intent/reservation on persistence failure.
              # Completely unavailable storage cannot furnish historical proof.
    return host._output("held")


def _prepare(journal, invocation_id):
    _id(invocation_id)
    with host.locked(journal.directory, "launch.lock"):
        with host.locked(journal.directory, "journal.lock", wait=1):
            _begin_locked(journal, invocation_id, "run")
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
                if result["status"] in {"completed", "postrun_failed"}:
                    _finish_operation(journal, invocation_id, "run")
                return result
            except BaseException:
                return _fence(journal, invocation_id)
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
                        _begin_locked(journal, invocation_id, "stop")
                        profile.check()
                        host.require(_optional(journal.directory, "systemd-hold.json") is None
                                     and _mode(journal))
                        _binding(journal)
                        raw, value = _owned(journal, invocation_id)
                        no_allocation = _closed(journal, value)
                        if no_allocation:
                            result = host._output("not_allocated")
                        if not no_allocation and not _exists(journal, value["job_id"]):
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
                            result = host._output("not_allocated")
                            no_allocation = True
                        if not no_allocation:
                            receipt = journal.read(value["job_id"])
                            host.require(receipt["profile_sha256"] == profile.sha256)
                            if not _terminal(receipt):
                                host.require(_binding(journal)[0] == raw)
                # The immutable ID guard is rechecked INSIDE serialized accepted stop,
                # including after admission.lock acquisition, not only here.
                if not no_allocation:
                    result = host.stop(profile_path, expected_job_id=value["job_id"],
                                       invoke=invoke, containment=containment)
                if result["status"] == "held":
                    with host.locked(journal.directory, "journal.lock", wait=1):
                        active = journal.active()
                        host.require(active["job_id"] == value["job_id"]
                                     and active["profile_sha256"] == profile.sha256)
                if result["status"] != "held":
                    _finish_operation(journal, invocation_id, "stop")
                return result
            except BaseException:
                return _fence(journal, invocation_id)
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
