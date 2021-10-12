from collections import namedtuple
from io import StringIO
import logging


log = logging.getLogger(__name__)


ExtCmdCfg = namedtuple('ExtCmdCfg',
                       ['iteration',
                        'duration',
                        'cmd',
                        'node',
                        'test',
                        'pid_name',
                        'pid_opt'])


class ExtCmd(object):
    def __init__(self, config, central_node, worker_nodes):
        cmd_list = [
                ExtCmdCfg(
                    iteration=ext_cmd.get('iteration', None),
                    duration=ext_cmd.get('duration', 0),
                    cmd=ext_cmd.get('cmd', ''),
                    node=ext_cmd.get('node', ''),
                    test=ext_cmd.get('test', ''),
                    pid_name=ext_cmd.get('pid_name', ''),
                    pid_opt=ext_cmd.get('pid_opt', ''),
                ) for ext_cmd in config.get('ext_cmd', list())
        ]
        cmd_list.sort(key=lambda x: x.iteration)
        self.cmd_map = self.build_cmd_map(cmd_list, central_node, worker_nodes)
        self.processes = []

    def __del__(self):
        # Just in case someone set some daft duration for a command, terminate
        # it here.
        while len(self.processes) > 0:
            process, _ = self.processes.pop(0)
            log.warning(f'Terminating command "{process.command}" '
                        f'(bad duration)')
            process.terminate()

    def build_cmd_map(self, cmd_list, central_node, worker_nodes):
        cmd_map = dict()
        for c in cmd_list:
            if c.iteration is None:
                continue
            if not len(c.cmd):
                continue
            if not len(c.test):
                continue
            if c.node == central_node.container:
                cmd_map[(c.iteration, c.test)] = {
                        'node': central_node,
                        'cmd': c.cmd,
                        'pid_name': c.pid_name,
                        'pid_opt': c.pid_opt,
                        'duration': c.duration,
                }
                continue
            for w in worker_nodes:
                if c.node != w.container:
                    continue
                cmd_map[(c.iteration, c.test)] = {
                        'node': w,
                        'cmd': c.cmd,
                        'pid_name': c.pid_name,
                        'pid_opt': c.pid_opt,
                        'duration': c.duration,
                }
                break
        return cmd_map

    async def exec_cmd(self, iteration, test):
        ext_cmd = self.cmd_map.get((iteration, test), None)
        if not ext_cmd:
            return

        cmd = ext_cmd['cmd']
        w = ext_cmd['node']

        if len(ext_cmd['pid_name']):
            stdout = StringIO()
            await w.run("pidof -s " + ext_cmd['pid_name'], stdout=stdout)
            pid = stdout.getvalue().strip()
            cmd = cmd + " " + ext_cmd['pid_opt'] + " " + pid

        if ext_cmd['duration'] <= 0:
            await w.run(cmd, stdout=stdout)
        else:
            process = await w.run_deferred(cmd)
            self.processes.append((process, ext_cmd['duration']))

    def terminate_process(self, test):
        new_processes = []
        for process, duration in self.processes:
            duration -= 1
            if duration <= 0:
                log.info(f'Terminating command "{process.command}"')
                # XXX Should we print stdout/stderr from the command?
                process.terminate()
            else:
                new_processes.append((process, duration))

        self.processes = new_processes
