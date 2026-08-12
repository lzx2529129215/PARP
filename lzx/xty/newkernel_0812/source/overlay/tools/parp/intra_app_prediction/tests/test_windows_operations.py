import unittest

from intra_app_prediction.operation_alignment import OperationEvent, align_operations
from intra_app_prediction.window_builder import EventWindowBuilder, window_bounds


S = 1_000_000_000


class WindowTests(unittest.TestCase):
    def test_exact_boundary(self):
        self.assertEqual(window_bounds(20 * S), (20 * S, 30 * S))

    def test_end_point_is_next_window(self):
        self.assertNotEqual(window_bounds(29999999999), window_bounds(30 * S))

    def test_cross_minute(self):
        self.assertEqual(window_bounds(65 * S), (60 * S, 70 * S))

    def test_cross_hour(self):
        self.assertEqual(window_bounds(3605 * S), (3600 * S, 3610 * S))

    def test_out_of_order_events_are_sorted(self):
        b = EventWindowBuilder()
        b.add("boot", "session", 25 * S, "b")
        b.add("boot", "session", 15 * S, "a")
        self.assertEqual([w.start_ns for w in b.windows()], [10 * S, 20 * S])

    def test_late_event_is_retained_with_flag(self):
        b = EventWindowBuilder(watermark_ns=5 * S)
        b.add("boot", "s", 20 * S, 1)
        b.add("boot", "s", 10 * S, 2)
        self.assertEqual(b.late_events, 1)

    def test_boot_ids_cannot_merge(self):
        b = EventWindowBuilder()
        b.add("a", "s", 1, 1)
        b.add("b", "s", 2, 2)
        self.assertEqual(len(b.windows()), 2)

    def test_monotonic_to_wall_anchor(self):
        b = EventWindowBuilder(monotonic_anchor_ns=100, wall_anchor_ns=1000)
        self.assertEqual(b.monotonic_to_wall(150), 1050)


class OperationTests(unittest.TestCase):
    def event(self, name, start, end):
        return OperationEvent(name, "WPS", start * S, end * S, name, "automation", 1.0, "s")

    def test_single_operation_pure(self):
        out = align_operations(0, 10 * S, [self.event("OPEN", 0, 10)])
        self.assertEqual((out.dominant_operation, out.label_quality), ("OPEN", "PURE"))

    def test_multiple_operations_choose_longest(self):
        out = align_operations(0, 10 * S, [self.event("A", 0, 3), self.event("B", 3, 10)])
        self.assertEqual(out.dominant_operation, "B")

    def test_mixed_window(self):
        out = align_operations(0, 10 * S, [self.event("A", 0, 6)])
        self.assertEqual(out.label_quality, "MIXED")

    def test_low_confidence_window(self):
        out = align_operations(0, 10 * S, [self.event("A", 0, 4)])
        self.assertEqual(out.label_quality, "LOW_CONFIDENCE")

    def test_operation_transition(self):
        out = align_operations(0, 10 * S, [self.event("A", 0, 5), self.event("B", 5, 10)])
        self.assertEqual(out.operation_transition, "A->B")

    def test_future_operation_not_used_as_current(self):
        out = align_operations(0, 10 * S, [self.event("FUTURE", 10, 20)])
        self.assertEqual((out.operation_count, out.dominant_operation), (0, "UNKNOWN"))

    def test_operation_at_start_and_end(self):
        out = align_operations(0, 10 * S, [self.event("A", -1, 4), self.event("B", 4, 11)])
        self.assertEqual((out.operation_at_start, out.operation_at_end), ("A", "B"))


if __name__ == "__main__":
    unittest.main()
