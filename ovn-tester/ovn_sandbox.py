import logging
import paramiko
import socket

from io import StringIO
from ovn_exceptions import SSHError

log = logging.getLogger(__name__)


class SSH:
    def __init__(self, hostname, cmd_log):
        self.hostname = hostname
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect(hostname)
        self.cmd_log = cmd_log

    @staticmethod
    def printable_result(out):
        if '\n' in out or '\r' in out:
            out = "---\n" + out
        return out

    def run(self, cmd="", stdout=None, raise_on_error=False):
        if self.cmd_log:
            log.info(f'Logging command: ssh {self.hostname} "{cmd}"')

        ssh_stdin, ssh_stdout, ssh_stderr = self.ssh.exec_command(cmd)
        exit_status = ssh_stdout.channel.recv_exit_status()

        if exit_status != 0 and raise_on_error:
            out = self.printable_result(ssh_stderr.read().decode().strip())
            if len(out):
                log.warning(out)
            raise SSHError(
                f'Command "{cmd}" failed with exit_status {exit_status}.'
            )

        if not ssh_stdout.channel.recv_ready():
            return

        if stdout:
            stdout.write(ssh_stdout.read().decode('ascii'))
        else:
            out = self.printable_result(ssh_stdout.read().decode().strip())
            if len(out):
                log.info(out)


class PhysicalNode(object):
    def __init__(self, hostname, log_cmds):
        self.ssh = SSH(hostname, log_cmds)

    def run(self, cmd="", stdout=None, raise_on_error=False):
        self.ssh.run(cmd=cmd, stdout=stdout, raise_on_error=raise_on_error)


class Sandbox(object):
    def __init__(self, phys_node, container):
        self.phys_node = phys_node
        self.container = container
        self.channel = None

    def ensure_channel(self):
        if self.channel:
            return

        self.channel = self.phys_node.ssh.ssh.invoke_shell(width=10000,
                                                           height=10000)
        # Fail if command didn't finish after 1 minute.
        self.channel.settimeout(60.0)
        if self.container:
            dcmd = 'docker exec -it ' + self.container + ' bash'
            self.channel.sendall(f"{dcmd}\n".encode())

        stdout = StringIO()
        # Checking + consuming all the unwanted output from the shell.
        self.run(cmd="echo Hello", stdout=stdout, raise_on_error=True)

    def run(self, cmd="", stdout=None, raise_on_error=False):
        if self.phys_node.ssh.cmd_log:
            log.info(f'Logging command: ssh {self.container} "{cmd}"')

        self.ensure_channel()

        # Can't have ';' right after '&'.
        if not cmd.endswith('&'):
            cmd = cmd + ' ;'

        self.channel.sendall(f"echo '++++start'; "
                             f"{cmd} echo $? ; "
                             f"echo '++++end' \n".encode())
        timed_out = False
        out = ''
        try:
            out = self.channel.recv(10240).decode()
            while '++++end' not in out.splitlines():
                out = out + self.channel.recv(10240).decode()
        except (paramiko.buffered_pipe.PipeTimeout, socket.timeout):
            if '++++start' not in out.splitlines():
                out = '++++start\n' + out
            out = out + '\n42\n++++end'
            timed_out = True
            log.warning(f'Command "{cmd}" timed out!')
            # Can't trust this shell anymore.
            self.channel.close()
            self.channel = None
            pass

        # Splitting and removing all lines with terminal control chars.
        out = out.splitlines()
        start = out.index('++++start') + 1
        end = out.index('++++end') - 1
        exit_status = int(out[end])
        out = [s for s in out[start:end] if '\x1b' not in s]

        if self.phys_node.ssh.cmd_log or timed_out:
            log.info(f'Result: {out}, Exit status: {exit_status}')

        out = '\n'.join(out).strip()

        if exit_status != 0 and raise_on_error:
            if len(out):
                log.warning(SSH.printable_result(out))
            raise SSHError(
                f'Command "{cmd}" failed with exit_status {exit_status}.'
            )

        if stdout:
            stdout.write(out)
        else:
            out = SSH.printable_result(out)
            if len(out):
                log.info(out)
