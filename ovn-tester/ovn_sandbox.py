import logging
import asyncssh
from itertools import cycle
import asyncio
import re

from ovn_exceptions import SSHError

log = logging.getLogger(__name__)

# asyncssh is VERY chatty when its log level is anything looser than
# "warning".
asyncssh.set_log_level(logging.WARNING)

# Messages sent over SSH are then sent over a unix socket to the ovn-nbctl
# daemon. During tests that send large amounts of data, it is possible to
# overload the unix socket and have messages rejected. When this happens,
# ovs reports the problem as a "Protocol error". In actuality, the error
# was EAGAIN when trying to connect, but OVS translates EAGAIN to EPROTO.
PROTO_ERROR = re.compile('could not connect to .* daemon \(Protocol error\)')


async def create_ssh(hostname, log):
    ssh = SSH(hostname, log)
    # XXX Need to mirror the paramiko auto add policy?
    ssh.conn = await asyncssh.connect(hostname)
    return ssh


class SSH:
    def __init__(self, hostname, cmd_log):
        self.hostname = hostname
        self.cmd_log = cmd_log
        self.conn = None

    @staticmethod
    def printable_result(out):
        if '\n' in out or '\r' in out:
            out = "---\n" + out
        return out

    async def run(self, cmd="", stdout=None, raise_on_error=False):
        if self.cmd_log:
            log.info(f'Logging command: ssh {self.hostname} "{cmd}"')

        while True:
            try:
                result = await self.conn.run(cmd)
            except asyncssh.misc.ChannelOpenError:
                # We sometimes see these exceptions seemingly at random.
                # Just retry if it happens.
                continue
            else:
                if result.exit_status != 0 and PROTO_ERROR.search(result.stderr):
                    # The command failed because the unix socket is full. If we give
                    # the daemon some time to run, we can retry the operation and it
                    # should succeed.
                    await asyncio.sleep(0.3)
                    continue
                else:
                    break

        if result.exit_status != 0 and raise_on_error:
            out = self.printable_result(result.stderr)
            if len(out) > 0:
                log.warning(out)
            raise SSHError(
                f'Command {cmd} failed with exit_status {result.exit_status}.'
            )

        if stdout:
            stdout.write(result.stdout.strip())
        else:
            out = self.printable_result(result.stdout.strip())
            if len(out) > 0:
                log.info(out)

    async def run_deferred(self, cmd=""):
        if self.cmd_log:
            log.info(f'Logging deferred command: ssh {self.hostname} "{cmd}"')

        return await self.conn.create_process(cmd)


async def create_physical_node(hostname, log_cmds, n_connections):
    node = PhysicalNode()
    node.ssh = cycle([await create_ssh(hostname, log_cmds) for _ in
                     range(n_connections)])
    node.deferred_ssh = await create_ssh(hostname, log_cmds)
    return node


class PhysicalNode(object):
    def __init__(self):
        self.ssh = None
        self.deferred_ssh = None

    async def run(self, cmd="", stdout=None, raise_on_error=False):
        conn = next(self.ssh)
        await conn.run(cmd=cmd, stdout=stdout,
                       raise_on_error=raise_on_error)

    async def run_deferred(self, cmd=""):
        return await self.deferred_ssh.run_deferred(cmd=cmd)


class Sandbox(object):
    def __init__(self, phys_node, container):
        self.phys_node = phys_node
        self.container = container

    async def run(self, cmd="", stdout=None, raise_on_error=False):
        if self.container:
            cmd = 'docker exec ' + self.container + ' ' + cmd
        await self.phys_node.run(cmd=cmd, stdout=stdout,
                                 raise_on_error=raise_on_error)

    async def run_deferred(self, cmd=""):
        if self.container:
            cmd = 'docker exec ' + self.container + ' ' + cmd
        return await self.phys_node.run_deferred(cmd=cmd)
