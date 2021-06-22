import paramiko
from io import StringIO


class PhysicalNode(object):
    def __init__(self, hostname, log_cmds):
        self.hostname = hostname
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


class OvnTestException(Exception):
    pass


class OvnInvalidConfigException(OvnTestException):
    pass


class OvnPingTimeoutException(OvnTestException):
    pass


class OvnChassisTimeoutException(OvnTestException):
    pass


class SSHError(OvnTestException):
    pass


class SSH:
    def __init__(self, hostname, log):

        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect(hostname)
        self.log = log

    def run(self, cmd="", stdout=None, raise_on_error=False):
        if self.log:
            print('Logging command: {}'.format(cmd))

        ssh_stdin, ssh_stdout, ssh_stderr = self.ssh.exec_command(cmd)
        exit_status = ssh_stdout.channel.recv_exit_status()

        if stdout:
            stdout.write(ssh_stdout.read().decode('ascii'))
        else:
            out = ssh_stdout.read().decode().strip()
            if len(out):
                print(out)
        if exit_status != 0 and raise_on_error:
            print(ssh_stderr.read().decode())
            details = "Command '{}' failed with exit_status {}.".format(
                cmd, exit_status)
            raise SSHError(details)


class OvsVsctl:
    def __init__(self, sb):
        self.sb = sb

    def run(self, cmd="", prefix="ovs-vsctl ", stdout=None):
        self.sb.run(cmd=prefix + cmd, stdout=stdout)

    def add_port(self, port, brige, internal=True, ifaceid=None):
        name = port["name"]
        cmd = "add-port {} {}".format(brige, name)
        if internal:
            cmd += " -- set interface {} type=internal".format(name)
        if ifaceid:
            cmd += " -- set Interface {} external_ids:iface-id={}".format(
                name, ifaceid
            )
            cmd += \
                " -- set Interface {} external_ids:iface-status=active".format(
                    name
                )
            cmd += " -- set Interface {} admin_state=up".format(name)
        self.run(cmd=cmd)

    def bind_vm_port(self, lport=None):
        self.run('ethtool -K {p} tx off &> /dev/null'.format(p=lport["name"]),
                 prefix="")
        self.run('ip netns add {p}'.format(p=lport["name"]), prefix="")
        self.run('ip link set {p} netns {p}'.format(p=lport["name"]),
                 prefix="")
        self.run('ip netns exec {p} ip link set {p} address {m}'.format(
            p=lport["name"], m=lport["mac"]), prefix="")
        self.run('ip netns exec {p} ip addr add {ip} dev {p}'.format(
            p=lport["name"], ip=lport["ip"]), prefix="")
        self.run('ip netns exec {p} ip link set {p} up'.format(
            p=lport["name"]), prefix="")

        self.run('ip netns exec {p} ip route add default via {gw}'.format(
            p=lport["name"], gw=lport["gw"]), prefix="")


# FIXME: Instead of returning raw dicts we should probably return custom
# objects with named fields: Lswitch, Lrouter, Lport, Rport, Port_Group, etc.
class OvnNbctl:
    def __init__(self, sb):
        self.sb = sb
        self.socket = ""

    def __del__(self):
        # FIXME: the SSH connection might have already been closed here..
        # self.stop_daemon()
        pass

    def run(self, cmd="", stdout=None):
        prefix = "ovn-nbctl "
        if len(self.socket):
            prefix = prefix + "-u " + self.socket + " "
        self.sb.run(cmd=prefix + cmd, stdout=stdout)

    def set_global(self, option, value):
        self.run("set NB_Global . options:{}={}".format(
            option, value
        ))

    def lr_add(self, name=""):
        self.run(cmd="lr-add {}".format(name))
        return {"name": name}

    def lr_port_add(self, router, name, mac, ip, prefixlen):
        self.run(cmd="lrp-add {} {} {} {}/{}".format(
            router["name"], name, mac, ip, prefixlen)
        )
        return {"name": name}

    def ls_add(self, name, cidr):
        print("***** creating lswitch {} *****".format(name))
        self.run(cmd="ls-add {}".format(name))
        return {"name": name, "cidr": cidr}

    def ls_port_add(self, lswitch, name="", router_port=None,
                    mac="", ip="", gw="", ext_gw=""):
        self.run(cmd="lsp-add {} {}".format(lswitch["name"], name))
        if router_port:
            cmd = "lsp-set-type {} router".format(name)
            cmd = cmd + " -- lsp-set-addresses {} router".format(name)
            cmd = cmd + " -- lsp-set-options {} router-port={}".format(
                name, router_port["name"]
            )
            self.run(cmd=cmd)
        elif len(mac) or len(ip):
            cmd = "lsp-set-addresses {} \"".format(name)
            if len(mac):
                cmd = cmd + mac + " "
            if len(ip):
                cmd = cmd + ip
            cmd = cmd + "\""
            self.run(cmd=cmd)
        stdout = StringIO()
        cmd = "get logical_switch_port {} _uuid".format(name)
        self.run(cmd=cmd, stdout=stdout)
        uuid = stdout.getvalue()
        return {
            "name": name, "mac": mac, "ip": ip, "gw": gw, "ext-gw": ext_gw,
            "uuid": uuid
        }

    def ls_port_set_set_options(self, port, options):
        self.run("lsp-set-options {} {}".format(port["name"], options))

    def ls_port_set_set_type(self, port, lsp_type):
        self.run("lsp-set-type {} {}".format(port["name"], lsp_type))

    def port_group_add(self, name="", lport=None, create=True):
        if (create):
            self.run(cmd='create port_group name={}'.format(name))
        else:
            cmd = "add port_group {} ports {}".format(name, lport["uuid"])
            self.run(cmd=cmd)

    def address_set_add(self, name="", addrs="", create=True):
        if create:
            cmd = "create Address_Set name=\"" + name + "\" addresses=\"" + \
                addrs + "\""
        else:
            cmd = "add Address_Set \"" + name + "\" addresses \"" + \
                addrs + "\""
        self.run(cmd=cmd)

    def acl_add(self, name="", direction="from-lport", priority=100,
                entity="switch", match="", verdict="allow"):
        cmd = "--type={} acl-add {} {} {} \"{}\" {}".format(
            entity, name, direction, str(priority), match, verdict
        )
        self.run(cmd=cmd)

    def route_add(self, router, network="0.0.0.0/0", gw="", policy=None):
        if policy:
            cmd = "--policy={} lr-route-add {} {} {}".format(policy,
                                                             router["name"],
                                                             network, gw)
        else:
            cmd = "lr-route-add {} {} {}".format(router["name"], network, gw)
        self.run(cmd=cmd)

    def nat_add(self, router, nat_type="snat", external_ip="", logical_ip=""):

        cmd = "lr-nat-add {} {} {} {}".format(router["name"], nat_type,
                                              external_ip, logical_ip)
        self.run(cmd=cmd)

    def wait_until(self, cmd=""):
        self.run("wait-until " + cmd)

    def sync(self, wait="hv"):
        self.run("--wait={} sync".format(wait))

    def start_daemon(self):
        cmd = "--detach --pidfile --log-file --no-leader-only"
        # FIXME: this needs rework!
        # if "remote" in nbctld_config:
        #     ovn_remote = nbctld_config["remote"]
        #     prot = nbctld_config["prot"]
        #     central_ips = [ip.strip() for ip in ovn_remote.split('-')]
        #     # If there is only one ip, then we can use unixctl socket.
        #     if len(central_ips) > 1:
        #         remote = ",".join(["{}:{}:6641".format(prot, r)
        #                           for r in central_ips])
        #         cmd += "--db=" + remote
        #         if prot == "ssl":
        #             cmd += "-p {} -c {} -C {}".format(
        #                 nbctld_config["privkey"], nbctld_config["cert"],
        #                 nbctld_config["cacert"])

        stdout = StringIO()
        self.run(cmd=cmd, stdout=stdout)
        self.socket = stdout.getvalue().rstrip()

    def stop_daemon(self):
        if len(self.socket):
            cmd = "ovs-appctl -t {} exit".format(self.socket)
            self.sb.run(cmd=cmd)


class OvnSbctl:
    def __init__(self, sb):
        self.sb = sb

    def run(self, cmd="", stdout=None):
        self.sb.run(cmd="ovn-sbctl --no-leader-only " + cmd, stdout=stdout)

    def chassis_bound(self, chassis=""):
        cmd = "--bare --columns _uuid find chassis name={}".format(chassis)
        stdout = StringIO()
        self.run(cmd=cmd, stdout=stdout)
        return len(stdout.getvalue().splitlines()) == 1
