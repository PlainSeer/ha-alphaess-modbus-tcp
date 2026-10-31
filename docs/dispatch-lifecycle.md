# Dispatch lifecycle and write reliability (development)

These are development changes, not a released version. They do not require
PredBat or any particular automation. No inverter control changes are deployed
by updating this documentation or the development branch.

## Sessions and duration

Force Charging, Force Discharging, Force Export, Force Import and generic Dispatch
remain fixed-duration modes. Explicitly enabling one or changing its duration
starts a new session and generation. A suspended fault requires an explicit new
start; changing a parameter alone does not silently recover it. A successful
restart keeps an already-on switch on, avoiding synthetic off/on events that
could trigger other automations. A failed restart still turns local control off
and reports the error. The deadline begins at turn-on, including
when Force Export/Import must wait for data before its first command.

Power and cutoff-SOC changes retain that deadline. Immediately before a command
is sent, after waiting for the shared connection, its duration is reduced to the
remaining whole seconds. A command cannot start after its deadline; servos avoid
rewrites during the final two seconds. The HA stop timer and displayed countdown
use the session deadline rather than restarting on each successful write.

Excess Export and Peak Shaving retain their existing renewable 300-second leases.
Force Export/Import have **not** been converted to short leases. PV Enabled and
Hold preferences are not written off during unload.

## Reload and options

Poll mode and its multipliers are updated in place and request a refresh. A model
variant change still reloads, avoiding a mixture of old and new register scales.
Cached register values expire after five configured poll intervals in slow
mode, rather than five unscaled intervals. Thus a healthy slow-polled value is
not removed before its next scheduled read. Failed values still expire; normal
and fast modes retain their previous expiry tolerance. This does not relax the
controllers' separate fresh-input checks.

Unload invalidates generations and stops timers/listeners before waiting. Pending
controller tasks are drained without cancellation. Closing the shared client
also waits for a service-driven write or poll already using that connection;
queued/new operations cannot reconnect after close begins. If an HA caller is
cancelled during a transaction (for example by an automation in restart mode),
the wire operation is drained under the same lock before cancellation propagates;
the connection is not closed underneath that operation. There is no separate
15-second cancellation timeout: the active operation uses pymodbus's timeouts.

Unload does not issue an automatic reset, including for Peak Shaving, and does
not restore active switch/controller sessions after reload. A persistent
notification identifies dispatches that may remain active and their last expected
expiry when known. This is an estimate, especially after a lost acknowledgement.
Dispatch Reset remains the explicit way to stop the inverter after reload.

## Outcomes and feedback

Dispatch switch attributes expose `dispatch_status`, `dispatch_error`, `dispatch_suspended`,
`requested_command`, `acknowledged_command` and `uncertain_command`. The command
arrays follow the 11-register block in the register map. An acknowledgement is
not a claim that a mode is still running indefinitely.

- **Not sent:** connection/read-back failed before transmitting this command, or
  the retry policy could not be safely selected. A guard/deadline can also skip
  stale work without transmitting it.
- **Rejected:** a definitive inverter exception such as illegal address/value.
- **Busy:** exception 6 is an explicit transient rejection. The rejected command
  is not recorded as uncertain or acknowledged. It can be retried within the
  same bounded budget; exhausted busy attempts report `dispatch_status: busy`.
- **Indeterminate:** a reply was lost or a device/processing/gateway error cannot
  establish whether the command applied. In particular, a locally synthesized
  pymodbus timeout response is not treated as an inverter rejection.
- **Conflict:** read-back differs from both the acknowledged command and the
  latest uncertain attempted command. Automatic writes are suspended.

**Behavior change for callers:** failed starts and parameter changes now raise a
Home Assistant service error rather than only logging success-looking failures.
HA scripts using normal error handling stop at that action. External callers,
including PredBat, retain their own error-handling/sequencing behavior; this
integration cannot promise that an external multi-action sequence aborts.

## Servo writes and retries

Force Export, Force Import and Excess Export have one background writer each.
Updates arriving during a slow write are coalesced into the newest requested
state. A retry recalculates from current inputs; setpoints and remaining time are
prepared again after any connection/read-back wait. Successful-write bookkeeping
is not advanced merely because a task was queued.

Read-back checks active fields, tolerates a configured or decrementing duration,
and respects PV `0` (leave unchanged). An inactive acknowledged stop can be
followed by the controller's normal resume even if inactive fields were restored
by the inverter. A read-back match proves consistency, **not who wrote it**.
Locally valid session identity is also required.

A timed-out attempted command can be confirmed by read-back. If it already applied
and no newer servo update is pending, it is not replayed. Other transient failures
have at most three controller attempts, with 0.5- and 1-second backoff. Service
starts/parameter writes and Peak Shaving also retry busy responses, without
adding another retry budget inside the servo. Permanent rejection, conflict or exhausted attempts suspend automatic writes until an
explicit new start (Peak Shaving also retains its existing inactive-dispatch
prerequisite). Failures do not cause automatic reset or automatic restart.
A suspended session does not reset an unexplained external command at its deadline.

Dispatch transmissions and their consistency reads use zero pymodbus retries
under the shared lock. The client/transaction/context retry setting is restored in
`finally`; ordinary polling keeps its existing policy. This path is tested with
pymodbus 3.7.4 and 3.16.1. An unsupported retry layout fails before sending.

Automatic deadline/SOC stops and Excess Export/Peak Shaving pauses remain, subject
to session and consistency checks. Transient pause and automatic-stop failures
have at most three attempts. Each retry rechecks local authorization and reads
the dispatch block before writing: a conflicting external command vetoes the
retry, and an already inactive dispatch confirms success without a duplicate
stop. A reset, unload or newer session invalidates old retries. Exhausted pause
failures remain enabled but explicitly suspended; automatic-stop failures leave
the local switch off with fault/uncertain-command attributes. Neither silently
resumes. Explicit stop services retry busy only, and still raise on uncertain
failure. Dispatch Reset retains one stop transmission followed by direct
`0x0880` verification; it never retries an uncertain reset write.

Dispatch Reset invalidates controllers before waiting for the control lock or
draining transactions. Starts already queued behind a slow start are rejected
while Reset is pending. After Reset completes or raises, explicit starts are
allowed again. The stop register block is unchanged, and verification errors
continue to abort HA recovery scripts using normal error handling.

The raw `write_register` service remains an explicit register write. A raw write
within `0x0880..0x088A` first relinquishes local dispatch sessions without adding
an inverter reset, so their old callbacks cannot reclaim that command. Unrelated
raw register writes do not stop dispatch sessions. Known PV preference writes
update the local consistency snapshot.

## Compatibility validation

Fault-injection tests cover applied-but-unacknowledged writes, rejection,
read-back mismatch, reconnect/deadline races, generation changes, slow/coalesced
servos, raw takeover, failed initial refresh and unload during a service write. Compatibility
cases cover eight-hour starts, power/SOC updates, Charging Hold, Mode 1 at -8/0 kW,
Mode 19, explicit return from holds and both recovery-script failure sequences.
They simulate Modbus and are not a substitute for a separately approved hardware
validation. No HA configuration or PredBat settings are changed by this patch.
Follow-up cases cover Reset behind an in-flight start, queued restarts, actual HA
state events during duration changes, bounded busy responses, pause/stop replies
lost before or after application, and external/new-session takeover during backoff.
