import paramiko
from io import StringIO

class OvnInvalidConfigException(Exception):
    pass

class OvnPingTimeoutException(Exception):
    pass

class SSHError(Exception):
    pass

class SSH:
    def __init__(self, node = {}):
        ip = node.get("ip", "127.0.0.1")
        username = node.get("user", "root")
        password = node.get("password", "")
        port = node.get("port", 22)

        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect(ip, username = username, password = password,
                         port = port)

    def run(self, cmd = "", stdout = None, raise_on_error = False):
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

class RemoteConn:
    def __init__(self, node = {}, ssh = None, container = None, log = False):
        self.ssh = SSH(node) if not ssh else ssh
        self.container = container
        self.log = log

    def run(self, cmd = "", stdout = None, raise_on_error = False):
        if self.container:
            command = 'docker exec ' + self.container + ' ' + cmd
        else:
            command = cmd

        if self.log:
            print(command)

        self.ssh.run(cmd = command, stdout = stdout,
                     raise_on_error = raise_on_error)

class OvsVsctl:
    def __init__(self, node = {}, ssh = None, container = None, log = False):
        self.ssh = RemoteConn(node = node, ssh = ssh,
                              container = container, log = log)

    def run(self, cmd = "", prefix = "ovs-vsctl ", stdout = None):
        self.ssh.run(cmd = prefix + cmd, stdout = stdout)

    def add_port(self, name = "", brige = "", internal = True,
                 ifaceid = None):
        cmd = "add-port {} {}".format(brige, name)
        if internal:
            cmd = cmd + " -- set interface {} type=internal".format(name)
        if ifaceid:
            cmd = cmd + " -- set Interface {} external_ids:iface-id={}".format(
                    name, ifaceid)
            cmd = cmd + " -- set Interface {} external_ids:iface-status=active".format(name)
            cmd = cmd + " -- set Interface {} admin_state=up".format(name)
        self.run(cmd = cmd)

    def bind_vm_port(self, lport = None):
        self.run('ethtool -K {p} tx off &> /dev/null'.format(p=lport["name"]),
                 prefix = "")
        self.run('ip netns add {p}'.format(p=lport["name"]), prefix = "")
        self.run('ip link set {p} netns {p}'.format(p=lport["name"]),
                 prefix = "")
        self.run('ip netns exec {p} ip link set {p} address {m}'.format(
            p=lport["name"], m=lport["mac"]), prefix = "")
        self.run('ip netns exec {p} ip addr add {ip} dev {p}'.format(
            p=lport["name"], ip=lport["ip"]), prefix = "")
        self.run('ip netns exec {p} ip link set {p} up'.format(
            p=lport["name"]), prefix = "")

        self.run('ip netns exec {p} ip route add default via {gw}'.format(
            p=lport["name"], gw=lport["gw"]), prefix = "")

class OvnNbctl:
    def __init__(self, node = {}, container = None, log = False):
        self.ssh = RemoteConn(node = node, container = container, log = log)
        self.socket = ""

    def __del__(self):
        self.stop_daemon()

    def run(self, cmd = "", stdout = None):
        prefix = "ovn-nbctl "
        if len(self.socket):
            prefix = prefix + "-u " + self.socket + " "
        self.ssh.run(cmd = prefix + cmd, stdout = stdout)

    def lr_add(self, name = ""):
        self.run(cmd = "lr-add {}".format(name))
        return { "name": name }

    def lr_port_add(self, router = "", name = "", mac = None, ip = None):
        self.run(cmd = "lrp-add {} {} {} {}".format(router, name, mac, ip))
        return { "name": name }

    def ls_add(self, name = ""):
        self.run(cmd = "ls-add {}".format(name))
        return { "name": name }

    def ls_port_add(self, lswitch = "", name = "", router_port = None,
                    mac = "", ip = "", gw = "", ext_gw = ""):
        self.run(cmd = "lsp-add {} {}".format(lswitch, name))
        if router_port:
            cmd = "lsp-set-type {} router".format(name)
            cmd = cmd + " -- lsp-set-addresses {} router".format(name)
            cmd = cmd + " -- lsp-set-options {} router-port={}".format(name, router_port)
            self.run(cmd = cmd)
        elif len(mac) or len(ip):
            cmd = "lsp-set-addresses {} \"".format(name)
            if len(mac):
                cmd = cmd + mac + " "
            if len(ip):
                cmd = cmd + ip
            cmd = cmd + "\""
            self.run(cmd = cmd)
        stdout = StringIO()
        cmd = "get logical_switch_port {} _uuid".format(name)
        self.run(cmd = cmd, stdout = stdout)
        uuid = stdout.getvalue()
        return { "name" : name, "mac" : mac, "ip" : ip, "gw" : gw, "ext-gw": ext_gw, "uuid" : uuid }

    def ls_port_set_set_options(self, name = "", options = ""):
        self.run("lsp-set-options {} {}".format(name, options))

    def ls_port_set_set_type(self, name = "", lsp_type = ""):
        self.run("lsp-set-type {} {}".format(name, lsp_type))

    def port_group_add(self, name = "", lport = None, create = True):
        if (create):
            self.run(cmd = "pg-add {} {}".format(name, lport["name"]))
        else:
            cmd = "add port_group {} ports {}".format(name, lport["uuid"])
            self.run(cmd = cmd)

    def address_set_add(self, name = "", addrs = "", create = True):
        if create:
            cmd = "create Address_Set name=\"" + name + "\" addresses=\"" + addrs + "\""
        else:
            cmd =  "add Address_Set \"" + name + "\" addresses \"" + addrs + "\""
        self.run(cmd = cmd)

    def acl_add(self, name = "", direction = "from-lport", priority = 100,
                entity = "switch", match = "", verdict = "allow"):
        cmd = "--type={} acl-add {} {} {} \"{}\" {}".format(entity, name, direction,
                                                            str(priority), match,
                                                            verdict)
        self.run(cmd = cmd)

    def route_add(self, name = "", network = "0.0.0.0/0", gw = "",
                  policy = None):
        if policy:
            cmd = "--policy={} lr-route-add {} {} {}".format(policy, name,
                                                             network, gw)
        else:
            cmd = "lr-route-add {} {} {}".format(name, network, gw)
        self.run(cmd = cmd)

    def nat_add(self, name, nat_type = "snat", external_ip = "",
                logical_ip = ""):

       cmd = "lr-nat-add {} {} {} {}".format(name, nat_type, external_ip,
                                             logical_ip)
       self.run(cmd = cmd)

    def wait_until(self, cmd = ""):
        self.run("wait-until " + cmd)

    def sync(self, wait = "hv"):
       self.run("--wait={} sync".format(wait))

    def start_daemon(self, nbctld_config = {}):
        cmd = "--detach --pidfile --log-file"
        if "remote" in nbctld_config:
            ovn_remote = nbctld_config["remote"]
            prot = nbctld_config["prot"]
            central_ips = [ip.strip() for ip in ovn_remote.split('-')]
            # If there is only one ip, then we can use unixctl socket.
            if len(central_ips) > 1:
                remote = ",".join(["{}:{}:6641".format(prot, r)
                                  for r in central_ips])
                cmd += "--db=" + remote
                if prot == "ssl":
                    cmd += "-p {} -c {} -C {}".format(
                        nbctld_config["privkey"], nbctld_config["cert"],
                        nbctld_config["cacert"])

        stdout = StringIO()
        self.run(cmd = cmd, stdout = stdout)
        self.socket = stdout.getvalue().rstrip()

    def stop_daemon(self):
        if len(self.socket):
            cmd = "ovs-appctl -t {} exit".format(self.socket)
            self.ssh.run(cmd = cmd)

class OvnSbctl:
    def __init__(self, node = {}, container = None, log = False):
        self.ssh = RemoteConn(node = node, container = container, log = log)

    def run(self, cmd = "", stdout = None):
        self.ssh.run(cmd = "ovn-sbctl --no-leader-only " + cmd, stdout = stdout)

    def chassis_bound(self, chassis = ""):
        cmd = "--bare --columns _uuid find chassis name={}".format(chassis)
        stdout = StringIO()
        self.run(cmd = cmd, stdout = stdout)
        return len(stdout.getvalue().splitlines()) == 1
