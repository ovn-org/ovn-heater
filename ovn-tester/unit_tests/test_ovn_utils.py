import unittest
from unittest import mock

import ovn_exceptions
import ovn_utils


class WaitForValueTest(unittest.TestCase):
    @mock.patch('ovn_utils.time.sleep')
    @mock.patch('ovn_utils.time.perf_counter', side_effect=[0, 0, 0.25])
    def test_returns_matching_value_and_duration(self, _clock, sleep):
        value, duration = ovn_utils.wait_for_value(
            lambda: 2,
            lambda observed: observed == 2,
            1,
            'two',
        )

        self.assertEqual(value, 2)
        self.assertEqual(duration, 0.25)
        sleep.assert_not_called()

    @mock.patch('ovn_utils.time.sleep')
    @mock.patch('ovn_utils.time.perf_counter', side_effect=[0, 0, 1])
    def test_reports_last_value_on_timeout(self, _clock, _sleep):
        with self.assertRaisesRegex(
            ovn_exceptions.OvnConvergenceTimeoutException,
            'Timed out waiting for two: observed=1',
        ):
            ovn_utils.wait_for_value(
                lambda: 1,
                lambda observed: observed == 2,
                1,
                'two',
            )


if __name__ == '__main__':
    unittest.main()
