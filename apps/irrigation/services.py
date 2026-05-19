from __future__ import annotations

import os
import socket
import struct

from django.utils import timezone

from apps.irrigation.models import (
    RELAY_FLASH_MAX_DURATION_SECONDS,
    RELAY_FLASH_MIN_DURATION_SECONDS,
    RELAY_FLASH_TICKS_PER_SECOND,
    RelayDevice,
    Valve,
)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


SIMULATOR = _env_bool("RELAY_SIMULATOR", False)
MODBUS_TIMEOUT_SECONDS = _env_float("MODBUS_TIMEOUT_SECONDS", 2.0)
MODBUS_RETRIES = _env_int("MODBUS_RETRIES", 1)
WRITE_SINGLE_COIL = 0x05
FLASH_ON_BASE_ADDRESS = 0x0200
FLASH_OFF_BASE_ADDRESS = 0x0400
MODBUS_PROTOCOL_ID = 0


class ModbusError(RuntimeError):
    pass


def _client_for(device: RelayDevice):
    from pyModbusTCP.client import ModbusClient

    return ModbusClient(
        host=device.host,
        port=device.port,
        unit_id=device.unit_id,
        timeout=MODBUS_TIMEOUT_SECONDS,
        auto_open=True,
        auto_close=True,
    )


def _set_simulated_state(valve: Valve, is_open: bool) -> None:
    now = timezone.now()
    updates = {}
    if valve.last_known_is_open != is_open:
        updates["last_known_is_open"] = is_open
        updates["last_polled_at"] = now
    if valve.last_polled_at is None and "last_polled_at" not in updates:
        updates["last_polled_at"] = now
    if updates:
        Valve.objects.filter(pk=valve.pk).update(**updates)


def _write_coil(client, channel: int, value: bool) -> None:
    attempts = MODBUS_RETRIES + 1
    for attempt in range(attempts):
        ok = client.write_single_coil(channel, value)
        if ok:
            return
        if attempt == attempts - 1:
            raise ModbusError("Failed to write coil")


def _read_coils(client, start: int, count: int) -> list[bool]:
    attempts = MODBUS_RETRIES + 1
    for attempt in range(attempts):
        result = client.read_coils(start, count)
        if result is not None:
            if len(result) < count:
                raise ModbusError("Incomplete coil read")
            return list(result)
        if attempt == attempts - 1:
            raise ModbusError("Failed to read coils")
    return []


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ModbusError("Connection closed while reading Modbus response")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _send_raw_write_single_coil(
    device: RelayDevice, address: int, value: int
) -> None:
    transaction_id = 1
    request = struct.pack(
        ">HHHBBHH",
        transaction_id,
        MODBUS_PROTOCOL_ID,
        6,
        device.unit_id,
        WRITE_SINGLE_COIL,
        address,
        value,
    )
    with socket.create_connection(
        (device.host, device.port), timeout=MODBUS_TIMEOUT_SECONDS
    ) as sock:
        sock.settimeout(MODBUS_TIMEOUT_SECONDS)
        sock.sendall(request)
        header = _recv_exact(sock, 7)
        rx_transaction_id, protocol_id, length, unit_id = struct.unpack(
            ">HHHB", header
        )
        if rx_transaction_id != transaction_id:
            raise ModbusError("Modbus transaction id mismatch")
        if protocol_id != MODBUS_PROTOCOL_ID:
            raise ModbusError("Modbus protocol id mismatch")
        if unit_id != device.unit_id:
            raise ModbusError("Modbus unit id mismatch")
        if length < 2:
            raise ModbusError("Invalid Modbus response length")

        pdu = _recv_exact(sock, length - 1)
        function_code = pdu[0]
        if function_code == WRITE_SINGLE_COIL | 0x80:
            exception_code = pdu[1] if len(pdu) > 1 else "unknown"
            raise ModbusError(f"Relay returned Modbus exception {exception_code}")
        if len(pdu) != 5 or function_code != WRITE_SINGLE_COIL:
            raise ModbusError("Unexpected Modbus response")

        response_address, response_value = struct.unpack(">HH", pdu[1:5])
        if response_address != address or response_value != value:
            raise ModbusError("Relay response does not match flash command")


def _write_flash_command(device: RelayDevice, address: int, ticks: int) -> None:
    attempts = MODBUS_RETRIES + 1
    last_exception: Exception | None = None
    for _attempt in range(attempts):
        try:
            _send_raw_write_single_coil(device, address, ticks)
            return
        except (OSError, ModbusError) as exc:
            last_exception = exc
    raise ModbusError(
        "Failed to write Waveshare relay flash command"
    ) from last_exception


def _duration_to_flash_ticks(duration_seconds: int) -> int:
    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int):
        raise ValueError("Duration must be an integer number of seconds.")
    if not (
        RELAY_FLASH_MIN_DURATION_SECONDS
        <= duration_seconds
        <= RELAY_FLASH_MAX_DURATION_SECONDS
    ):
        raise ValueError(
            "Duration must be between "
            f"{RELAY_FLASH_MIN_DURATION_SECONDS} and "
            f"{RELAY_FLASH_MAX_DURATION_SECONDS} seconds."
        )
    return duration_seconds * RELAY_FLASH_TICKS_PER_SECOND


def _flash_address_for(valve: Valve) -> int:
    channel_index = valve.channel - 1
    if not 0 <= channel_index <= 7:
        raise ValueError("Valve channel must be between 1 and 8.")
    base_address = (
        FLASH_ON_BASE_ADDRESS if valve.is_active_high else FLASH_OFF_BASE_ADDRESS
    )
    return base_address + channel_index


def open_valve(valve: Valve) -> None:
    raise RuntimeError("Unbounded valve opening is disabled. Use open_valve_for().")


def open_valve_for(valve: Valve, duration_seconds: int) -> None:
    ticks = _duration_to_flash_ticks(duration_seconds)
    if SIMULATOR:
        _set_simulated_state(valve, True)
        return

    _write_flash_command(valve.relay_device, _flash_address_for(valve), ticks)


def close_valve(valve: Valve) -> None:
    if SIMULATOR:
        _set_simulated_state(valve, False)
        return

    client = _client_for(valve.relay_device)
    coil_value = not valve.is_active_high
    _write_coil(client, valve.channel - 1, coil_value)


def read_valve_state(valve: Valve) -> bool:
    if SIMULATOR:
        return valve.last_known_is_open

    client = _client_for(valve.relay_device)
    result = _read_coils(client, valve.channel - 1, 1)
    is_open = result[0] == valve.is_active_high
    return is_open


def read_device_states(device: RelayDevice) -> list[bool]:
    if SIMULATOR:
        states = [False] * 8
        for valve in Valve.objects.filter(relay_device=device):
            if 1 <= valve.channel <= 8:
                if valve.last_known_is_open:
                    states[valve.channel - 1] = valve.is_active_high
                else:
                    states[valve.channel - 1] = not valve.is_active_high
        return states

    client = _client_for(device)
    raw_states = _read_coils(client, 0, 8)
    return list(raw_states)
