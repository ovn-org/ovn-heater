import unittest
from collections import defaultdict
from unittest import mock

from ovn_sandbox import Sandbox
from ovn_workload import ChassisNode


class SandboxTest(unittest.TestCase):
    def make_sandbox(self):
        sandbox = Sandbox.__new__(Sandbox)
        sandbox.background_processes = defaultdict(list)
        sandbox.run_output = mock.Mock(return_value='')
        return sandbox

    def test_run_output_returns_captured_stdout(self):
        sandbox = Sandbox.__new__(Sandbox)

        def write_output(**kwargs):
            kwargs['stdout'].write('output')

        sandbox.run = mock.Mock(side_effect=write_output)

        result = sandbox.run_output(
            'command',
            raise_on_error=True,
            timeout=12,
        )

        self.assertEqual(result, 'output')
        sandbox.run.assert_called_once_with(
            cmd='command',
            stdout=mock.ANY,
            raise_on_error=True,
            timeout=12,
        )

    @mock.patch('ovn_sandbox.uuid.uuid4')
    def test_background_process_is_tracked_by_owner(self, uuid4):
        uuid4.return_value.hex = 'process-id'
        sandbox = self.make_sandbox()

        sandbox.start_background_process('port-0', 'sleep 10')

        self.assertEqual(
            sandbox.background_processes,
            {
                'port-0': [
                    (
                        '/tmp/ovn-heater-process-id.pid',
                        '/tmp/ovn-heater-process-id.pid.log',
                    )
                ]
            },
        )
        self.assertEqual(sandbox.run_output.call_count, 2)
        start_cmd = sandbox.run_output.call_args_list[0].args[0]
        self.assertIn('nohup sleep 10', start_cmd)
        self.assertIn('kill -0', sandbox.run_output.call_args_list[1].args[0])

    def test_stop_background_processes_for_owner(self):
        sandbox = self.make_sandbox()
        sandbox.background_processes = {
            'port-0': [('/tmp/process.pid', '/tmp/process.log')],
            'port-1': [('/tmp/other.pid', '/tmp/other.log')],
        }

        sandbox.stop_background_processes('port-0')

        self.assertNotIn('port-0', sandbox.background_processes)
        self.assertIn('port-1', sandbox.background_processes)
        stop_cmd = sandbox.run_output.call_args.args[0]
        self.assertIn('kill "$(cat /tmp/process.pid)"', stop_cmd)
        self.assertIn('rm -f /tmp/process.pid /tmp/process.log', stop_cmd)


class ChassisNodeTest(unittest.TestCase):
    def test_unbind_port_stops_owned_background_processes(self):
        node = ChassisNode.__new__(ChassisNode)
        node.stop_background_processes = mock.Mock()
        node.vsctl = mock.Mock()
        port = mock.Mock(name='port-0', passive=False)
        port.name = 'port-0'

        ChassisNode.unbind_port.__wrapped__(node, port)

        node.stop_background_processes.assert_called_once_with('port-0')
        node.vsctl.unbind_vm_port.assert_called_once_with(port)
        node.vsctl.del_port.assert_called_once_with(port)


if __name__ == '__main__':
    unittest.main()
