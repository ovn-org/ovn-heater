import logging
import paramiko
import shlex
import socket
import uuid

from collections import defaultdict
from io import StringIO
from ovn_exceptions import SSHError
from typing import List

log = logging.getLogger(__name__)

DEFAULT_SANDBOX_TIMEOUT = 60


class SSH:
    def __init__(self, hostname: str, cmd_log: bool):
        self.hostname = hostname
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect(hostname)
        self.cmd_log = cmd_log

    @staticmethod
    def printable_result(out: str) -> str:
        if '\n' in out or '\r' in out:
            out = "---\n" + out
        return out

    def run(self, cmd="", stdout=None, raise_on_error: bool = False) -> None:
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


class PhysicalNode:
    def __init__(self, hostname: str, log_cmds: bool):
        self.ssh = SSH(hostname, log_cmds)

    def run(self, cmd="", stdout=None, raise_on_error: bool = False) -> None:
        self.ssh.run(cmd=cmd, stdout=stdout, raise_on_error=raise_on_error)


class Sandbox:
    def __init__(self, phys_node, container):
        self.phys_node = phys_node
        self.container = container
        self.channel = None
        self.background_processes = defaultdict(list)

    def ensure_channel(self) -> None:
        if self.channel:
            return

        self.channel = self.phys_node.ssh.ssh.invoke_shell(
            width=10000, height=10000
        )
        if self.container:
            dcmd = 'podman exec -it ' + self.container + ' bash'
            self.channel.sendall(f"{dcmd}\n".encode())

        stdout = StringIO()
        # Checking + consuming all the unwanted output from the shell.
        self.run(cmd="echo Hello", stdout=stdout, raise_on_error=True)

    # Splits 'out' by universal newline characters with the addition that it
    # considers the terminal String Terminator character '\x1b\' as a newline.
    @staticmethod
    def split_channel_output(out: str) -> List[str]:
        lines = []
        for line in out.splitlines():
            lines += line.split('\x1b\\')
        return lines

    def run(
        self,
        cmd: str = "",
        stdout=None,
        raise_on_error: bool = False,
        timeout: int = DEFAULT_SANDBOX_TIMEOUT,
    ) -> None:
        if self.phys_node.ssh.cmd_log:
            log.info(f'Logging command: ssh {self.container} "{cmd}"')

        self.ensure_channel()
        # Fail if command didn't finish after 'timeout' seconds.
        self.channel.settimeout(timeout)

        # Can't have ';' right after '&'.
        if not cmd.endswith('&'):
            cmd = cmd + ' ;'

        self.channel.sendall(
            f"echo '++++start'; "
            f"{cmd} echo $? ; "
            f"echo '++++end' \n".encode()
        )
        timed_out = False
        out = ''
        try:
            out = self.channel.recv(10240).decode()
            while '++++end' not in out.splitlines():
                out = out + self.channel.recv(10240).decode()
        except (paramiko.buffered_pipe.PipeTimeout, socket.timeout):
            if '++++start' not in self.split_channel_output(out):
                out = '++++start\n' + out
            out = out + '\n42\n++++end'
            timed_out = True
            log.warning(f'Command "{cmd}" timed out!')
            # Can't trust this shell anymore.
            self.channel.close()
            self.channel = None
            pass

        # Splitting and removing all lines with terminal control chars.
        out = self.split_channel_output(out)
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

    def run_output(
        self,
        cmd: str = "",
        raise_on_error: bool = False,
        timeout: int = DEFAULT_SANDBOX_TIMEOUT,
    ) -> str:
        stdout = StringIO()
        self.run(
            cmd=cmd,
            stdout=stdout,
            raise_on_error=raise_on_error,
            timeout=timeout,
        )
        return stdout.getvalue()

    def start_background_process(self, owner: str, cmd: str) -> None:
        process_id = uuid.uuid4().hex
        pid_file = f'/tmp/ovn-heater-{process_id}.pid'
        log_file = f'{pid_file}.log'
        start_cmd = (
            f'nohup {cmd} >{shlex.quote(log_file)} 2>&1 </dev/null & '
            f'echo $! >{shlex.quote(pid_file)}'
        )
        self.run_output(
            f'sh -c {shlex.quote(start_cmd)}',
            raise_on_error=True,
        )
        self.run_output(
            f'sleep 0.1; test -s {shlex.quote(pid_file)} && '
            f'kill -0 "$(cat {shlex.quote(pid_file)})" || '
            f'{{ cat {shlex.quote(log_file)}; false; }}',
            raise_on_error=True,
        )
        self.background_processes[owner].append((pid_file, log_file))

    def stop_background_processes(self, owner: str) -> None:
        for pid_file, log_file in self.background_processes.pop(owner, []):
            self.run_output(
                f'if test -s {shlex.quote(pid_file)}; then '
                f'kill "$(cat {shlex.quote(pid_file)})" 2>/dev/null || true; '
                f'fi; rm -f {shlex.quote(pid_file)} '
                f'{shlex.quote(log_file)}',
                raise_on_error=True,
            )
