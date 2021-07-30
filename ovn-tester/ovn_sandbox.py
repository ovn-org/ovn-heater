import paramiko

from ovn_exceptions import SSHError


class SSH:
    def __init__(self, hostname, log):
        self.hostname = hostname
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect(hostname)
        self.log = log

    def run(self, cmd="", stdout=None, raise_on_error=False):
        if self.log:
            print(f'Logging command: ssh {self.hostname} "{cmd}"')

        ssh_stdin, ssh_stdout, ssh_stderr = self.ssh.exec_command(cmd)
        exit_status = ssh_stdout.channel.recv_exit_status()

        if exit_status != 0 and raise_on_error:
            print(ssh_stderr.read().decode())
            raise SSHError(
                f'Command "{cmd}" failed with exit_status {exit_status}.'
            )

        if not ssh_stdout.channel.recv_ready():
            return

        if stdout:
            stdout.write(ssh_stdout.read().decode('ascii'))
        else:
            out = ssh_stdout.read().decode().strip()
            if len(out):
                print(out)


class PhysicalNode(object):
    def __init__(self, hostname, log_cmds):
        self.ssh = SSH(hostname, log_cmds)

    def run(self, cmd="", stdout=None, raise_on_error=False):
        self.ssh.run(cmd=cmd, stdout=stdout, raise_on_error=raise_on_error)


class Sandbox(object):
    def __init__(self, phys_node, container):
        self.phys_node = phys_node
        self.container = container

    def run(self, cmd="", stdout=None, raise_on_error=False):
        if self.container:
            cmd = 'docker exec ' + self.container + ' ' + cmd
        self.phys_node.run(cmd=cmd, stdout=stdout,
                           raise_on_error=raise_on_error)
