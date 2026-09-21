from __future__ import annotations

import struct
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.irrigation import services
from apps.irrigation.models import (
    RELAY_FLASH_MAX_DURATION_SECONDS,
    RelayDevice,
    Schedule,
    ScheduleRule,
    Site,
    Valve,
)


class FakeModbusSocket:
    def __init__(self) -> None:
        self.sent = b""
        self.response = b""
        self.timeout = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def sendall(self, data: bytes) -> None:
        self.sent = data
        self.response = data

    def recv(self, size: int) -> bytes:
        chunk = self.response[:size]
        self.response = self.response[size:]
        return chunk


class RelayFlashServiceTests(TestCase):
    def setUp(self) -> None:
        self.site = Site.objects.create(name="Home", timezone="UTC")
        self.device = RelayDevice.objects.create(
            site=self.site,
            name="Relay",
            host="192.0.2.10",
            port=502,
            unit_id=1,
        )

    def _create_valve(self, *, is_active_high: bool) -> Valve:
        return Valve.objects.create(
            relay_device=self.device,
            channel=1,
            name="Front",
            is_active_high=is_active_high,
            default_max_duration_seconds=600,
        )

    def _capture_flash_frame(
        self, valve: Valve, duration_seconds: int
    ) -> bytes:
        fake_socket = FakeModbusSocket()
        with mock.patch(
            "apps.irrigation.services.socket.create_connection",
            return_value=fake_socket,
        ):
            self.assertIs(services.open_valve_for(valve, duration_seconds), False)
        return fake_socket.sent

    def test_active_high_open_uses_flash_on_address(self) -> None:
        frame = self._capture_flash_frame(
            self._create_valve(is_active_high=True),
            600,
        )

        *_, unit_id, function_code, address, value = struct.unpack(
            ">HHHBBHH", frame
        )
        self.assertEqual(unit_id, 1)
        self.assertEqual(function_code, 0x05)
        self.assertEqual(address, 0x0200)
        self.assertEqual(value, 6000)

    def test_active_low_open_uses_flash_off_address(self) -> None:
        frame = self._capture_flash_frame(
            self._create_valve(is_active_high=False),
            60,
        )

        *_, unit_id, function_code, address, value = struct.unpack(
            ">HHHBBHH", frame
        )
        self.assertEqual(unit_id, 1)
        self.assertEqual(function_code, 0x05)
        self.assertEqual(address, 0x0400)
        self.assertEqual(value, 600)

    def test_over_limit_duration_is_rejected_before_network_io(self) -> None:
        valve = self._create_valve(is_active_high=True)
        with mock.patch(
            "apps.irrigation.services.socket.create_connection"
        ) as create_connection:
            with self.assertRaises(ValueError):
                services.open_valve_for(
                    valve, RELAY_FLASH_MAX_DURATION_SECONDS + 1
                )

        create_connection.assert_not_called()

    def test_successful_retry_reports_uncertainty_without_changing_pulse(self):
        valve = self._create_valve(is_active_high=True)
        first, second = FakeModbusSocket(), FakeModbusSocket()
        with (
            mock.patch.object(services, "MODBUS_RETRIES", 1),
            mock.patch.object(
                first, "recv", side_effect=TimeoutError("Lost acknowledgement"),
            ),
            mock.patch(
                "apps.irrigation.services.socket.create_connection",
                side_effect=[first, second],
            ) as connection,
        ):
            self.assertIs(services.open_valve_for(valve, 1), True)

        self.assertEqual(connection.call_count, 2)
        self.assertEqual(first.sent, second.sent)
        self.assertEqual(struct.unpack(">HHHBBHH", first.sent)[-1], 10)
        self.assertEqual(first.timeout, services.MODBUS_TIMEOUT_SECONDS)
        self.assertEqual(second.timeout, services.MODBUS_TIMEOUT_SECONDS)

    def test_failed_retries_preserve_existing_attempt_limit(self):
        valve = self._create_valve(is_active_high=True)
        with (
            mock.patch.object(services, "MODBUS_RETRIES", 1),
            mock.patch(
                "apps.irrigation.services._send_raw_write_single_coil",
                side_effect=TimeoutError("Lost acknowledgement"),
            ) as send,
        ):
            with self.assertRaises(services.ModbusError):
                services.open_valve_for(valve, 1)
        self.assertEqual(send.call_count, 2)

    def test_simulator_reports_no_transport_retry(self):
        valve = self._create_valve(is_active_high=True)
        with (
            mock.patch.object(services, "SIMULATOR", True),
            mock.patch(
                "apps.irrigation.services._set_simulated_state",
            ) as set_state,
            mock.patch(
                "apps.irrigation.services.socket.create_connection",
            ) as connection,
        ):
            self.assertIs(services.open_valve_for(valve, 60), False)
        set_state.assert_called_once_with(valve, True)
        connection.assert_not_called()

    def test_unbounded_open_is_disabled(self) -> None:
        valve = self._create_valve(is_active_high=True)

        with self.assertRaises(RuntimeError):
            services.open_valve(valve)


class RelayFlashValidationTests(TestCase):
    def setUp(self) -> None:
        self.site = Site.objects.create(name="Home", timezone="UTC")
        self.device = RelayDevice.objects.create(
            site=self.site,
            name="Relay",
            host="192.0.2.10",
        )
        self.valve = Valve.objects.create(
            relay_device=self.device,
            channel=1,
            name="Front",
            default_max_duration_seconds=600,
        )
        self.schedule = Schedule.objects.create(site=self.site, name="Default")

    def test_valve_default_duration_rejects_over_limit(self) -> None:
        valve = Valve(
            relay_device=self.device,
            channel=2,
            name="Back",
            default_max_duration_seconds=RELAY_FLASH_MAX_DURATION_SECONDS + 1,
        )

        with self.assertRaises(ValidationError):
            valve.full_clean()

    def test_schedule_duration_cannot_exceed_relay_flash_limit(self) -> None:
        rule = ScheduleRule(
            schedule=self.schedule,
            valve=self.valve,
            enabled=True,
            days_of_week_mask=1,
            start_time="06:00",
            mode=ScheduleRule.MODE_FIXED,
            max_duration_seconds=RELAY_FLASH_MAX_DURATION_SECONDS + 1,
        )

        with self.assertRaises(ValidationError):
            rule.full_clean()
