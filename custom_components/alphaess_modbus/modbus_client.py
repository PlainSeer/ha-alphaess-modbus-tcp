from __future__ import annotations

import asyncio
import inspect
import logging
import struct
import time
from typing import Any, Awaitable, Callable

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from .dispatch import WriteNotSent, WriteRejected, WriteBusy, WriteIndeterminate, DispatchConflict, command_matches

_LOGGER = logging.getLogger(__name__)

# pymodbus renamed the slave keyword: 'slave' in <3.8, 'device_id' in >=3.8
_sig = inspect.signature(AsyncModbusTcpClient.read_holding_registers).parameters
_SLAVE_KWARG = "device_id" if "device_id" in _sig else "slave"

_CONNECT_RETRIES = 2
_CONNECT_RETRY_DELAY = 1.0  # seconds between retries

# Comms-level failures that mean the socket is dead/half-open. pymodbus raises
# ModbusException subclasses (ConnectionException, ModbusIOException) for transport
# errors; protocol errors (e.g. illegal address) are returned via result.isError()
# instead, and do NOT indicate a broken link.
_COMM_ERRORS = (ModbusException, asyncio.TimeoutError, OSError)


def _retry_owner(client):
    # 3.16 async: ctx; earlier 3.x: transaction; 3.7 async: client.
    for owner in (getattr(client, "ctx", None), getattr(client, "transaction", None), client):
        if owner is not None and hasattr(owner, "retries"):
            return owner
    return None


async def _finish_request(awaitable):
    """Keep the shared transaction alive even if its service caller is cancelled."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        if not task.cancelled():
            task.exception()  # consume an error before propagating caller cancellation
        raise asyncio.CancelledError
    return task.result()


class AlphaESSModbusClient:
    def __init__(self, host: str, port: int, slave_id: int) -> None:
        self._host = host
        self._port = port
        self._slave_id = slave_id
        self._client: AsyncModbusTcpClient | None = None
        self._lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._consecutive_failures: int = 0
        self._skip_until: float = 0.0
        self._closing = False

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.connected

    async def connect(self) -> None:
        """Connect with retries — inverter may refuse immediately after a prior close.

        This wrapper is the single reconnect authority: pymodbus's own background
        reconnect loop is disabled (reconnect_delay=0) so two mechanisms don't fight
        over the inverter's one-connection limit. The previous client is always closed
        before a new one is created, so a reconnect never leaks a socket. Guarded by
        _connect_lock so concurrent callers coalesce into one attempt.
        """
        async with self._connect_lock:
            if self._closing:
                raise ModbusException("Client is unloading")
            # Never leak a prior client (and its socket); tear it down first.
            self._teardown_client()
            client = AsyncModbusTcpClient(
                self._host, port=self._port, timeout=5, reconnect_delay=0
            )
            try:
                for attempt in range(_CONNECT_RETRIES):
                    await client.connect()
                    if client.connected:
                        self._client = client
                        return
                    if attempt < _CONNECT_RETRIES - 1:
                        _LOGGER.debug("Connect attempt %d/%d failed, retrying", attempt + 1, _CONNECT_RETRIES)
                        await asyncio.sleep(_CONNECT_RETRY_DELAY)
            finally:
                if self._client is not client:
                    # Also cover exceptions/cancellation during initial setup.
                    client.close()

    def _teardown_client(self) -> None:
        """Close and forget the current client (synchronous; pymodbus close() is sync)."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    async def close(self) -> None:
        # Gate queued/new operations, then drain the operation holding the lock.
        # This includes writes made directly by service handlers, not just servos.
        self._closing = True
        async with self._lock:
            async with self._connect_lock:
                self._teardown_client()

    async def _ensure_connected(self) -> None:
        if self._closing:
            raise ModbusException("Client is unloading")
        if not self.connected:
            # Backoff: after 3 consecutive failures hold off reconnects for 10 s so
            # one disconnected cycle doesn't fire dozens of blocking retry loops.
            if time.monotonic() < self._skip_until:
                raise ModbusException("Reconnect backoff active")
            await self.connect()
            if self.connected:
                self._consecutive_failures = 0
            else:
                self._consecutive_failures += 1
                if self._consecutive_failures >= 3:
                    self._skip_until = time.monotonic() + 10.0
                    _LOGGER.debug(
                        "3 consecutive connect failures — backing off for 10 s"
                    )
        if not self.connected:
            raise ModbusException("Not connected")

    async def _request(
        self, call: Callable[[], Awaitable[Any]], what: str
    ) -> Any:
        """Run one pymodbus request, dropping the connection on a comms failure.

        A raised comms error means the socket is dead or half-open (e.g. after a
        router restart): close it so the next cycle reconnects cleanly instead of
        reusing a zombie connection forever. A returned isError() result is a
        protocol error on a healthy link and leaves the connection intact.
        """
        try:
            result = await _finish_request(call())
        except asyncio.CancelledError:
            self._teardown_client()
            raise
        except _COMM_ERRORS as err:
            self._teardown_client()
            raise ModbusException(f"Comm error {what}: {err}") from err
        if result is None:
            self._teardown_client()
            raise ModbusException(f"No response {what}")
        if result.isError():
            if not getattr(result, "exception_code", None):
                self._teardown_client()  # includes 3.7's synthesized no-reply response
            raise ModbusException(f"Error {what}: {result}")
        return result

    async def read_block(self, start: int, count: int) -> list[int]:
        """Read `count` consecutive holding registers starting at `start`.

        Returns a list of raw uint16 values; decoding is done by the caller.
        """
        async with self._lock:
            await self._ensure_connected()
            result = await self._request(
                lambda: self._client.read_holding_registers(
                    start, count=count, **{_SLAVE_KWARG: self._slave_id}
                ),
                f"reading block {start:#06x}+{count}",
            )
            return list(result.registers)

    async def read_register(self, address: int, data_type: str, count: int = 1) -> Any:
        async with self._lock:
            return await self._read(address, data_type, count)

    async def _read(self, address: int, data_type: str, count: int) -> Any:
        await self._ensure_connected()

        if data_type == "string":
            result = await self._request(
                lambda: self._client.read_holding_registers(
                    address, count=count, **{_SLAVE_KWARG: self._slave_id}
                ),
                f"reading {address:#06x}",
            )
            raw = b"".join(struct.pack(">H", r) for r in result.registers)
            return raw.decode("ascii", errors="replace").rstrip("\x00")

        reg_count = 2 if data_type in ("int32", "uint32") else 1
        result = await self._request(
            lambda: self._client.read_holding_registers(
                address, count=reg_count, **{_SLAVE_KWARG: self._slave_id}
            ),
            f"reading {address:#06x}",
        )

        regs = result.registers
        if data_type == "int16":
            value = regs[0]
            if value > 32767:
                value -= 65536
        elif data_type == "uint16":
            value = regs[0]
        elif data_type == "int32":
            combined = (regs[0] << 16) | regs[1]
            if combined > 2147483647:
                combined -= 4294967296
            value = combined
        elif data_type == "uint32":
            value = (regs[0] << 16) | regs[1]
        else:
            raise ValueError(f"Unknown data_type: {data_type}")

        return value

    async def write_registers(
        self, address: int, values: list[int], *, guard: Callable[[], bool] | None = None,
    ) -> bool:
        async with self._lock:
            if guard is not None and not guard():
                return False
            await self._ensure_connected()
            # Recheck after reconnect: a controller may have stopped while waiting.
            if guard is not None and not guard():
                return False
            await self._request(
                lambda: self._client.write_registers(
                    address, values, **{_SLAVE_KWARG: self._slave_id}
                ),
                f"writing {address:#06x}",
            )
            return True

    async def write_dispatch(self, address, *, guard, prepare, expected, attempted, acknowledged, confirmed=None):
        """One dispatch transmission; all checks run under the shared lock.

        Per-write retries are not a pymodbus API. Temporarily disable transport
        retries under our lock, restoring the sensor/read policy in finally.
        The retry owner is version-dependent (client, transaction or ctx).
        """
        async with self._lock:
            if not guard():
                return False
            try:
                await self._ensure_connected()
            except Exception as err:
                raise WriteNotSent(str(err)) from err
            if not guard():
                return False
            retry_owner = _retry_owner(self._client)
            if retry_owner is None:
                raise WriteNotSent("Unsupported pymodbus dispatch retry policy; no command sent")
            try:
                previous_retries = retry_owner.retries
                retry_owner.retries = 0
            except Exception as err:
                raise WriteNotSent(f"Cannot select dispatch retry policy: {err}") from err
            try:
                candidates = expected()
                if candidates:
                    try:
                        result = await self._request(
                            lambda: self._client.read_holding_registers(
                                address, count=11, **{_SLAVE_KWARG: self._slave_id}),
                            "checking dispatch before write",
                        )
                        observed = list(result.registers)
                        if len(observed) != 11:
                            raise ValueError("Incomplete dispatch read-back")
                    except Exception as err:
                        raise WriteNotSent(f"Dispatch read-back failed: {err}") from err
                    if observed[0] == 0 and (planned := prepare()) is not None and planned[0] == 0:
                        acknowledged(observed)
                        return True
                    match = next((cmd for cmd in candidates if command_matches(observed, cmd)), None)
                    if match is None:
                        raise DispatchConflict("Dispatch read-back differs from acknowledged and uncertain commands")
                    if confirmed is not None:
                        confirmed(match)
                # Read-back and reconnect may have taken time. Prepare last.
                if not guard() or (values := prepare()) is None:
                    return False
                attempted(values)
                try:
                    result = await _finish_request(self._client.write_registers(
                        address, values, **{_SLAVE_KWARG: self._slave_id}))
                except asyncio.CancelledError:
                    self._teardown_client()
                    raise
                except Exception as err:
                    self._teardown_client()
                    raise WriteIndeterminate(f"Dispatch acknowledgement failed: {err}") from err
                if result is None:
                    self._teardown_client()
                    raise WriteIndeterminate("Missing dispatch acknowledgement")
                if result.isError():
                    # A transport timeout can be a synthesized ExceptionResponse.
                    # Processing/device/gateway errors do not prove non-application.
                    code = getattr(result, "exception_code", None)
                    if code == 6:
                        raise WriteBusy("Inverter busy (exception 6)")
                    if code not in (1, 2, 3):
                        self._teardown_client()
                        raise WriteIndeterminate(f"No authoritative dispatch acknowledgement: {result}")
                    raise WriteRejected(f"Inverter rejected dispatch (exception {code})")
                acknowledged(values)
                return True
            finally:
                retry_owner.retries = previous_retries

    async def write_register(self, address: int, value: int) -> None:
        async with self._lock:
            await self._ensure_connected()
            await self._request(
                lambda: self._client.write_register(
                    address, value, **{_SLAVE_KWARG: self._slave_id}
                ),
                f"writing {address:#06x}",
            )
