import json
import os
import tempfile
import unittest
from unittest import mock

import ovn_stats


class StatsReportTest(unittest.TestCase):
    def setUp(self):
        ovn_stats.clear()

    @mock.patch('ovn_stats.ovn_context.active_context')
    def test_report_writes_raw_measurements(self, active_context):
        active_context.iteration = 2
        ovn_stats.add('Convergence', 1.25, failed=False)
        active_context.iteration = 3
        ovn_stats.add('Convergence', 2.5, failed=True)

        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            try:
                os.chdir(directory)
                ovn_stats.report('phase', brief=True)
                with open('phase-report.json') as report_file:
                    report = json.load(report_file)
                html_written = os.path.isfile('phase-report.html')
            finally:
                os.chdir(previous)

        self.assertTrue(html_written)
        self.assertEqual(report['version'], 1)
        self.assertEqual(report['test_name'], 'phase')
        self.assertEqual(
            report['measurements'],
            [
                {
                    'counter': 'Convergence',
                    'iteration': 2,
                    'seconds': 1.25,
                    'failed': False,
                },
                {
                    'counter': 'Convergence',
                    'iteration': 3,
                    'seconds': 2.5,
                    'failed': True,
                },
            ],
        )
        active_context.fail.assert_called_once_with()

    @mock.patch('ovn_stats.time.perf_counter', side_effect=[10.0, 12.5])
    @mock.patch('ovn_stats.ovn_context.active_context')
    def test_measure_records_named_duration(
        self, active_context, perf_counter
    ):
        active_context.iteration = 4

        with ovn_stats.measure('Convergence'):
            pass

        self.assertEqual(
            ovn_stats.timed_functions[('Convergence', 4)],
            [(2.5, False)],
        )
        active_context.fail.assert_not_called()

    @mock.patch('ovn_stats.time.perf_counter', side_effect=[10.0, 11.0])
    @mock.patch('ovn_stats.ovn_context.active_context')
    def test_measure_preserves_failures(self, active_context, perf_counter):
        active_context.iteration = 1

        with self.assertRaisesRegex(RuntimeError, 'failed'):
            with ovn_stats.measure('Mutation'):
                raise RuntimeError('failed')

        self.assertEqual(
            ovn_stats.timed_functions[('Mutation', 1)],
            [(1.0, True)],
        )
        active_context.fail.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
