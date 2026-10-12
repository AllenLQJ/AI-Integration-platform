import unittest
from datetime import datetime
from scripts.macos_sleep_evidence import evaluate_cycle, evaluate_power_log, full_wake_rows, timeout_processed_after_resume


class SleepEvidenceTests(unittest.TestCase):
    def test_full_wake_transition_is_not_mistaken_for_dark_wake(self):
        dark = '2026-10-11 15:13:24 +0800 DarkWake    DarkWake from Deep Idle'
        full = '2026-10-11 15:13:30 +0800 Wake        DarkWake to FullWake from Deep Idle'
        self.assertEqual(full_wake_rows([dark, full]), [full])
    def test_actual_early_wake_does_not_prove_deadline_crossing(self):
        cycle = evaluate_cycle(['2026-10-11 15:05:26 +0800 Sleep'],
                               ['2026-10-11 15:05:30 +0800 Wake'],
                               datetime.fromisoformat('2026-10-11T15:05:34.903+08:00').timestamp())
        self.assertFalse(cycle['deadline_crossed'])
        self.assertEqual(cycle['actual_sleep_seconds'], 4)

    def test_expiry_between_sleep_and_wake_is_verified(self):
        cycle = evaluate_cycle(['2026-10-11 15:05:26 +0800 Sleep'],
                               ['2026-10-11 15:05:30 +0800 Wake'],
                               datetime.fromisoformat('2026-10-11T15:05:28+08:00').timestamp())
        self.assertTrue(cycle['deadline_crossed'])

    def test_seconds_precision_cannot_prove_sleep_started_before_expiry(self):
        cycle = evaluate_cycle(['2026-10-11 15:05:26 +0800 Sleep'],
                               ['2026-10-11 15:05:30 +0800 Wake'],
                               datetime.fromisoformat('2026-10-11T15:05:26.500+08:00').timestamp())
        self.assertFalse(cycle['deadline_crossed'])

    def test_missing_wake_is_unverified(self):
        self.assertFalse(evaluate_cycle(['2026-10-11 15:05:26 +0800 Sleep'], [], 0)['deadline_crossed'])

    def test_deadline_during_dark_wake_is_not_counted_as_sleep(self):
        rows = ['2026-10-11 15:13:21 +0800 Sleep   Entering Sleep state',
                '2026-10-11 15:13:24 +0800 DarkWake  DarkWake from Deep Idle',
                '2026-10-11 15:14:10 +0800 Sleep   Entering Sleep state',
                '2026-10-11 15:14:24 +0800 Wake    Wake from Deep Idle']
        deadline = datetime.fromisoformat('2026-10-11T15:13:30+08:00').timestamp()
        self.assertFalse(evaluate_power_log(rows, deadline)['deadline_crossed'])

    def test_dark_wake_ends_a_proven_sleep_interval(self):
        rows = ['2026-10-11 15:13:21 +0800 Sleep   Entering Sleep state',
                '2026-10-11 15:13:24 +0800 DarkWake  DarkWake from Deep Idle']
        deadline = datetime.fromisoformat('2026-10-11T15:13:23+08:00').timestamp()
        result = evaluate_power_log(rows, deadline)
        self.assertTrue(result['deadline_crossed'])
        self.assertEqual(result['resume_kind'], 'DarkWake')

    def test_wake_requests_does_not_end_physical_sleep(self):
        rows = ['2026-10-11 15:20:15 +0800 Sleep               \tEntering Sleep state',
                '2026-10-11 15:20:16 +0800 Wake Requests       \t[process=mDNSResponder request=Maintenance]',
                '2026-10-11 15:20:46 +0800 DarkWake            \tDarkWake from Deep Idle',
                '2026-10-11 15:20:54 +0800 Wake                \tDarkWake to FullWake']
        deadline = datetime.fromisoformat('2026-10-11T15:20:16.217699+08:00').timestamp()
        result = evaluate_power_log(rows, deadline)
        self.assertTrue(result['deadline_crossed'])
        self.assertEqual(result['actual_sleep_seconds'], 31)
        self.assertEqual(result['resume_kind'], 'DarkWake')
        self.assertEqual(full_wake_rows(rows), [rows[-1]])

    def test_non_tab_wake_requests_is_not_a_wake(self):
        row = '2026-10-11 15:20:16 +0800 Wake Requests       request=Maintenance'
        self.assertEqual(full_wake_rows([row]), [])

    def test_timeout_processed_before_suspend_does_not_prove_wake_recovery(self):
        cycle = {'wake_event_time': 46, 'timestamp_resolution_seconds': 1}
        self.assertFalse(timeout_processed_after_resume(cycle, 16.417))
        self.assertFalse(timeout_processed_after_resume(cycle, 46.5))

    def test_timeout_processed_after_resume_is_verified(self):
        cycle = {'wake_event_time': 46, 'timestamp_resolution_seconds': 1}
        self.assertTrue(timeout_processed_after_resume(cycle, 47.1))
