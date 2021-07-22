from collections import namedtuple
from io import StringIO

ExtCmdCfg = namedtuple('ExtCmdCfg',
                       ['iteration',
                        'cmd',
                        'node',
                        'test',
                        'pid_name',
                        'pid_opt',
                        'background_opt'])


class ExtCmd(object):
    def __init__(self, config, central_node, worker_nodes):
        cmd_list = [
                ExtCmdCfg(
                    iteration=ext_cmd.get('iteration', None),
                    cmd=ext_cmd.get('cmd', ''),
                    node=ext_cmd.get('node', ''),
                    test=ext_cmd.get('test', ''),
                    pid_name=ext_cmd.get('pid_name', ''),
                    pid_opt=ext_cmd.get('pid_opt', ''),
                    background_opt=ext_cmd.get('background_opt', False),
                ) for ext_cmd in config.get('ext_cmd', list())
        ]
        cmd_list.sort(key=lambda x: x.iteration)
        self.cmd_map = self.build_cmd_map(cmd_list, central_node, worker_nodes)

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
                        'background_opt': c.background_opt,
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
                        'background_opt': c.background_opt,
                }
                break
        return cmd_map

    def exec_cmd(self, iteration, test):
        ext_cmd = self.cmd_map.get((iteration, test), None)
        if not ext_cmd:
            return

        cmd = ext_cmd['cmd']
        w = ext_cmd['node']

        if len(ext_cmd['pid_name']):
            stdout = StringIO()
            w.run("pidof -s " + ext_cmd['pid_name'], stdout=stdout)
            pid = stdout.getvalue().strip()
            cmd = cmd + " " + ext_cmd['pid_opt'] + " " + pid

        if ext_cmd['background_opt']:
            cmd = cmd + " &"

        stdout = StringIO()
        w.run(cmd, stdout=stdout)
        return stdout.getvalue().strip()
