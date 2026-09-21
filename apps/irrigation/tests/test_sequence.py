import itertools
import random

from django.test import SimpleTestCase

from apps.irrigation.sequence import plan_sequence, target_seconds


def members(totals, caps):
    return [
        {"valve_id": index, "order": index, "total_seconds": total,
         "run_cap_seconds": cap}
        for index, (total, cap) in enumerate(zip(totals, caps))
    ]


class SequenceTests(SimpleTestCase):
    def test_one_hour_target_and_two_hour_peak_have_equal_duration_breaks(self):
        for interval in (30, 60):
            actual = plan_sequence(
                members([3600], [1800]), controller_interval_seconds=interval,
                command_allowance_seconds=12,
            )
            self.assertEqual(actual["watering_seconds"], 3600)
            self.assertEqual(actual["break_seconds"], 1800)
            self.assertEqual(actual["elapsed_seconds"], 5400)
            self.assertEqual(actual["scheduling_allowance_seconds"], 4 * interval + 24)
            peak = plan_sequence(
                members([7200], [1800]), controller_interval_seconds=interval,
                command_allowance_seconds=12,
            )
            self.assertEqual(peak["pulse_count"], 4)
            self.assertEqual(peak["repeat_count"], 3)
            self.assertEqual(peak["break_seconds"], 5400)
            self.assertEqual(peak["elapsed_seconds"], 12600)
            self.assertEqual(peak["scheduling_allowance_seconds"], 8 * interval + 48)

    def test_short_intervening_valve_only_offsets_part_of_required_break(self):
        sequence = plan_sequence(members([3600, 600], [1800, 300]))
        self.assertEqual(
            [(p["valve_id"], p["pass_number"], p["duration_seconds"])
             for p in sequence["pulses"]],
            [(0, 1, 1800), (1, 1, 300), (0, 2, 1800), (1, 2, 300)],
        )
        self.assertEqual(sequence["pulses"][2]["break_before_seconds"], 1500)
        self.assertEqual(sequence["pulses"][2]["start_seconds"], 3600)
        self.assertEqual(sequence["break_seconds"], 1500)

    def test_partial_final_runs_and_zero_members_preserve_order(self):
        sequence = plan_sequence(members([130, 0, 250], [60, 100, 100]))
        self.assertEqual(
            [(p["valve_id"], p["pass_number"], p["duration_seconds"])
             for p in sequence["pulses"]],
            [(0, 1, 60), (2, 1, 100), (0, 2, 60), (2, 2, 100),
             (0, 3, 10), (2, 3, 50)],
        )
        self.assertEqual(sequence["watering_seconds"], 380)
        self.assertEqual(sequence["repeat_count"], 4)
        self.assertEqual(sequence["elapsed_seconds"], 510)

    def test_fixed_has_one_pass_and_no_post_sequence_rest(self):
        sequence = plan_sequence(members([300, 60], [300, 60]), smart=False)
        self.assertEqual(sequence["watering_seconds"], 360)
        self.assertEqual(sequence["break_seconds"], 0)
        self.assertEqual(sequence["elapsed_seconds"], 360)
        self.assertEqual(sequence["repeat_count"], 0)
        self.assertEqual(sequence["scheduling_allowance_seconds"], 180)

    def test_zero_target_has_no_allowance(self):
        sequence = plan_sequence(members([0, 0], [100, 300]))
        self.assertEqual(sequence["pulses"], [])
        self.assertEqual(sequence["reserved_seconds"], 0)

    def test_finite_arithmetic_and_early_day_bounds(self):
        self.assertEqual(target_seconds(7, 7), 3600)
        self.assertEqual(target_seconds(14, 7), 7200)
        self.assertEqual(target_seconds(0, 1e-300), 0)
        self.assertEqual(target_seconds(0.1, 0.3), 1200)
        for target, rate in ((1e308, 1e-300), (float("inf"), 7), (7, 0)):
            with self.assertRaises(ValueError):
                target_seconds(target, rate)
        with self.assertRaisesMessage(ValueError, "allowance"):
            plan_sequence(members([40000], [1]))
        with self.assertRaisesMessage(ValueError, "Peak watering"):
            plan_sequence(members([10 ** 1000], [1]))
        with self.assertRaisesMessage(ValueError, "breaks"):
            plan_sequence(
                members([3600], [1800]), available_seconds=5000,
                controller_interval_seconds=1,
            )

    def test_exhaustive_reduced_targets_never_outgrow_peak_envelope(self):
        for caps in itertools.product((1, 2, 3), repeat=2):
            peak = plan_sequence(
                members([6, 6], caps), controller_interval_seconds=1
            )
            for totals in itertools.product(range(7), repeat=2):
                actual = plan_sequence(
                    members(totals, caps), controller_interval_seconds=1
                )
                self.assertLessEqual(
                    actual["reserved_seconds"], peak["reserved_seconds"],
                    (totals, caps),
                )

    def test_random_reduced_and_missing_members_stay_within_peak(self):
        generator = random.Random(2409)
        for _ in range(100):
            caps = [generator.randint(1, 100) for _ in range(4)]
            totals = [generator.randint(0, 400) for _ in range(4)]
            reduced = [generator.randint(0, total) for total in totals]
            for interval in (30, 60):
                kwargs = {"controller_interval_seconds": interval,
                          "command_allowance_seconds": 12}
                peak = plan_sequence(members(totals, caps), **kwargs)
                actual = plan_sequence(members(reduced, caps), **kwargs)
                self.assertLessEqual(actual["reserved_seconds"], peak["reserved_seconds"])
