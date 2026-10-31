"""Dispatch outcomes and conservative read-back consistency checks.

Matching registers establish consistency, never the identity of their writer.
"""
from homeassistant.exceptions import HomeAssistantError


class DispatchWriteError(HomeAssistantError):
    outcome = "indeterminate"


class WriteNotSent(DispatchWriteError):
    outcome = "not_sent"


class WriteRejected(DispatchWriteError):
    outcome = "rejected"


class WriteIndeterminate(DispatchWriteError):
    outcome = "indeterminate"


class DispatchConflict(DispatchWriteError):
    outcome = "conflict"


def command_matches(observed, command):
    if len(observed) != 11 or len(command) != 11:
        return False
    if observed[0] == 0 and command[0] == 0:
        return True  # inactive fields need not retain the stopped command
    # Some firmware exposes a remaining time and others the configured time.
    # PV=0 means leave unchanged, not an instruction to turn PV off.
    for index in (*range(7), 9, 10):
        if index == 10 and command[index] == 0:
            continue
        if observed[index] != command[index]:
            return False
    observed_time = (observed[7] << 16) | observed[8]
    sent_time = (command[7] << 16) | command[8]
    return 0 <= observed_time <= sent_time
