from __future__ import annotations

import os

from django.utils import timezone

from apps.irrigation.models import RelayDevice, Valve


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


def open_valve(valve: Valve) -> None:
    if SIMULATOR:
        _set_simulated_state(valve, True)
        return

    client = _client_for(valve.relay_device)
    coil_value = valve.is_active_high
    _write_coil(client, valve.channel - 1, coil_value)


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
