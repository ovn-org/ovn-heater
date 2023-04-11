from collections import defaultdict
from io import StringIO


class ExtCmdUnit(object):
    def __init__(self, conf, central_node, worker_nodes):
        self.iteration = conf.get('iteration')
        self.cmd = conf.get('cmd')
        self.test = conf.get('test')
        self.pid_name = conf.get('pid_name')
        self.background_opt = conf.get('background_opt')
        self.pid_opt = conf.get('pid_opt', '')
        self.node = None

        node = conf.get('node')
        if node == central_node.container:
            self.node = central_node
            return

        for wn in worker_nodes:
            if node == wn.container:
                self.node = wn
                return

    def is_valid(self):
        return (
            self.iteration is not None
            and self.cmd
            and self.test
            and self.node is not None
        )

    def exec(self):
        cmd = self.cmd

        if self.pid_name:
            stdout = StringIO()
            self.node.run(f'pidof -s {self.pid_name}', stdout=stdout)
            cmd += f' {self.pid_opt} {stdout.getvalue().strip()}'

        if self.background_opt:
            cmd += ' &'

        stdout = StringIO()
        self.node.run(cmd, stdout=stdout)
        return stdout.getvalue().strip()


class ExtCmd(object):
    def __init__(self, config, central_node, worker_nodes):
        self.cmd_map = defaultdict(list)
        for ext_cmd in config.get('ext_cmd', list()):
            cmd_unit = ExtCmdUnit(ext_cmd, central_node, worker_nodes)
            if cmd_unit.is_valid():
                self.cmd_map[(cmd_unit.iteration, cmd_unit.test)].append(
                    cmd_unit
                )

    def exec_cmd(self, iteration, test):
        ext_cmds = self.cmd_map.get((iteration, test))
        if not ext_cmds:
            return

        return {ext_cmd: ext_cmd.exec() for ext_cmd in ext_cmds}
