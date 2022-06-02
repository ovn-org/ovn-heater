import logging
import netaddr
import ovn_exceptions
from collections import namedtuple
from io import StringIO

log = logging.getLogger(__name__)

LRouter = namedtuple('LRouter', ['uuid', 'name'])
LRPort = namedtuple('LRPort', ['name'])
LSwitch = namedtuple('LSwitch', ['uuid', 'name', 'cidr', 'cidr6'])
LSPort = namedtuple(
    'LSPort',
    [
        'name',
        'mac',
        'ip',
        'plen',
        'gw',
        'ext_gw',
        'ip6',
        'plen6',
        'gw6',
        'ext_gw6',
        'metadata',
        'passive',
        'uuid',
    ],
)
PortGroup = namedtuple('PortGroup', ['name'])
AddressSet = namedtuple('AddressSet', ['name'])
LoadBalancer = namedtuple('LoadBalancer', ['name', 'uuid'])
LoadBalancerGroup = namedtuple('LoadBalancerGroup', ['name', 'uuid'])

DEFAULT_CTL_TIMEOUT = 60


DualStackIP = namedtuple('DualStackIP', ['ip4', 'plen4', 'ip6', 'plen6'])


class PhysCtl:
    def __init__(self, sb):
        self.sb = sb

    def run(self, cmd="", stdout=None, timeout=DEFAULT_CTL_TIMEOUT):
        self.sb.run(cmd=cmd, stdout=stdout, timeout=timeout)

    def external_host_provision(self, ip, gw, netns='ext-ns'):
        log.info(f'Adding external host on {self.sb.container}')
        cmd = (
            f'ip link add veth0 type veth peer name veth1; '
            f'ip netns add {netns}; '
            f'ip link set netns {netns} dev veth0; '
            f'ip netns exec {netns} ip link set dev veth0 up; '
        )

        if ip.ip4:
            cmd += (
                f'ip netns exec ext-ns ip addr add {ip.ip4}/{ip.plen4}'
                f' dev veth0; '
            )
        if gw.ip4:
            cmd += f'ip netns exec ext-ns ip route add default via {gw.ip4}; '

        if ip.ip6:
            cmd += (
                f'ip netns exec ext-ns ip addr add {ip.ip6}/{ip.plen6}'
                f' dev veth0; '
            )
        if gw.ip6:
            cmd += f'ip netns exec ext-ns ip route add default via {gw.ip6}; '

        cmd += 'ip link set dev veth1 up; ' 'ovs-vsctl add-port br-ex veth1'

        # Run as a single invocation:
        cmd = f'bash -c \'{cmd}\''
        self.run(cmd=cmd)


class DualStackSubnet:
    def __init__(self, n4=None, n6=None):
        self.n4 = n4
        self.n6 = n6

    @classmethod
    def next(cls, n, index=0):
        n4 = n.n4.next(index) if n.n4 else None
        n6 = n.n6.next(index) if n.n6 else None
        return cls(n4, n6)

    def forward(self, index=0):
        if self.n4 and self.n6:
            return DualStackIP(
                netaddr.IPAddress(self.n4.first + index),
                self.n4.prefixlen,
                netaddr.IPAddress(self.n6.first + index),
                self.n6.prefixlen,
            )
        if self.n4 and not self.n6:
            return DualStackIP(
                netaddr.IPAddress(self.n4.first + index),
                self.n4.prefixlen,
                None,
                None,
            )
        if not self.n4 and self.n6:
            return DualStackIP(
                None,
                None,
                netaddr.IPAddress(self.n6.first + index),
                self.n6.prefixlen,
            )
        raise ovn_exceptions.OvnInvalidConfigException("invalid configuration")

    def reverse(self, index=1):
        if self.n4 and self.n6:
            return DualStackIP(
                netaddr.IPAddress(self.n4.last - index),
                self.n4.prefixlen,
                netaddr.IPAddress(self.n6.last - index),
                self.n6.prefixlen,
            )
        if self.n4 and not self.n6:
            return DualStackIP(
                netaddr.IPAddress(self.n4.last - index),
                self.n4.prefixlen,
                None,
                None,
            )
        if not self.n4 and self.n6:
            return DualStackIP(
                None,
                None,
                netaddr.IPAddress(self.n6.last - index),
                self.n6.prefixlen,
            )
        raise ovn_exceptions.OvnInvalidConfigException("invalid configuration")


class OvsVsctl:
    def __init__(self, sb):
        self.sb = sb

    def run(
        self,
        cmd="",
        prefix="ovs-vsctl ",
        stdout=None,
        timeout=DEFAULT_CTL_TIMEOUT,
    ):
        self.sb.run(cmd=prefix + cmd, stdout=stdout, timeout=timeout)

    def add_port(self, port, bridge, internal=True, ifaceid=None):
        name = port.name
        cmd = f'add-port {bridge} {name}'
        if internal:
            cmd += f' -- set interface {name} type=internal'
        if ifaceid:
            cmd += f' -- set Interface {name} external_ids:iface-id={ifaceid}'
        self.run(cmd=cmd)

    def del_port(self, port):
        self.run(f'del-port {port.name}')

    def bind_vm_port(self, lport):
        cmd = (
            f'ip netns add {lport.name}; '
            f'ip link set {lport.name} netns {lport.name}; '
            f'ip netns exec {lport.name} ip link set {lport.name} '
            f'address {lport.mac}; '
            f'ip netns exec {lport.name} ip link set {lport.name} up'
        )
        if lport.ip:
            cmd += (
                f'; ip netns exec {lport.name} ip addr add '
                f'{lport.ip}/{lport.plen} dev {lport.name}'
            )
            cmd += (
                f'; ip netns exec {lport.name} ip route add '
                f'default via {lport.gw}'
            )
        if lport.ip6:
            cmd += (
                f'; ip netns exec {lport.name} ip addr add '
                f'{lport.ip6}/{lport.plen6} dev {lport.name} nodad'
            )
            cmd += (
                f'; ip netns exec {lport.name} ip route add '
                f'default via {lport.gw6}'
            )
        self.run(cmd, prefix="")

    def unbind_vm_port(self, lport):
        self.run(f'ip netns del {lport.name}', prefix='')


class OvnNbctl:
    def __init__(self, sb):
        self.sb = sb
        self.socket = ""

    def __del__(self):
        # FIXME: the SSH connection might have already been closed here..
        # self.stop_daemon()
        pass

    def run(self, cmd="", stdout=None, timeout=DEFAULT_CTL_TIMEOUT):
        prefix = "ovn-nbctl "
        if len(self.socket):
            prefix = prefix + "-u " + self.socket + " "
        self.sb.run(cmd=prefix + cmd, stdout=stdout, timeout=timeout)

    def set_global(self, option, value):
        self.run(f'set NB_Global . options:{option}={value}')

    def set_inactivity_probe(self, value):
        self.run(f'set Connection . inactivity_probe={value}')

    def lr_add(self, name):
        log.info(f'Creating lrouter {name}')

        cmd = f'create Logical_Router name={name}'
        stdout = StringIO()
        self.run(cmd=cmd, stdout=stdout)
        return LRouter(name=name, uuid=stdout.getvalue().strip())

    def lr_port_add(self, router, name, mac, dual_ip=None):
        cmd = f'lrp-add {router.uuid} {name} {mac}'
        if dual_ip.ip4 and dual_ip.plen4:
            cmd += f' {dual_ip.ip4}/{dual_ip.plen4} '
        if dual_ip.ip6 and dual_ip.plen6:
            cmd += f' {dual_ip.ip6}/{dual_ip.plen6} '
        self.run(cmd=cmd)
        return LRPort(name=name)

    def lr_port_set_gw_chassis(self, rp, chassis, priority=10):
        log.info(f'Setting gw chassis {chassis} for router port {rp.name}')
        self.run(cmd=f'lrp-set-gateway-chassis {rp.name} {chassis} {priority}')

    def ls_add(self, name, net_s):
        log.info(f'Creating lswitch {name}')

        cmd = f'create Logical_Switch name={name}'
        stdout = StringIO()
        self.run(cmd=cmd, stdout=stdout)
        return LSwitch(
            name=name,
            cidr=net_s.n4,
            cidr6=net_s.n6,
            uuid=stdout.getvalue().strip(),
        )

    def ls_port_add(
        self,
        lswitch,
        name,
        router_port=None,
        mac=None,
        ip=None,
        gw=None,
        ext_gw=None,
        metadata=None,
        passive=False,
        security=False,
        localnet=False,
    ):
        cmd = f'lsp-add {lswitch.uuid} {name}'
        if router_port:
            cmd += (
                f' -- lsp-set-type {name} router'
                f' -- lsp-set-addresses {name} router'
                f' -- lsp-set-options {name} router-port={router_port.name}'
            )
        elif mac or ip or localnet:
            addresses = []
            if mac:
                addresses.append(mac)
            if localnet:
                addresses.append("unknown")
            if ip and ip.ip4:
                addresses.append(str(ip.ip4))
            if ip and ip.ip6:
                addresses.append(str(ip.ip6))

            addresses = " ".join(addresses)
            cmd += f' -- lsp-set-addresses {name} \"{addresses}\"'
            if security:
                cmd += f' -- lsp-set-port-security {name} \"{addresses}\"'
        self.run(cmd=cmd)
        stdout = StringIO()
        self.run(cmd=f'get logical_switch_port {name} _uuid', stdout=stdout)
        uuid = stdout.getvalue().strip()

        ip4 = "unknown" if localnet else ip.ip4 if ip else None
        plen4 = ip.plen4 if ip else None
        gw4 = gw.ip4 if gw else None
        ext_gw4 = ext_gw.ip4 if ext_gw else None

        ip6 = ip.ip6 if ip else None
        plen6 = ip.plen6 if ip else None
        gw6 = gw.ip6 if gw else None
        ext_gw6 = ext_gw.ip6 if ext_gw else None

        return LSPort(
            name=name,
            mac=mac,
            ip=ip4,
            plen=plen4,
            gw=gw4,
            ext_gw=ext_gw4,
            ip6=ip6,
            plen6=plen6,
            gw6=gw6,
            ext_gw6=ext_gw6,
            metadata=metadata,
            passive=passive,
            uuid=uuid,
        )

    def ls_port_del(self, port):
        self.run(cmd=f'lsp-del {port.name}')

    def ls_port_set_set_options(self, port, options):
        self.run(cmd=f'lsp-set-options {port.name} {options}')

    def ls_port_set_set_type(self, port, lsp_type):
        self.run(cmd=f'lsp-set-type {port.name} {lsp_type}')

    def port_group_create(self, name):
        self.run(cmd=f'create port_group name={name}')
        return PortGroup(name=name)

    def port_group_add(self, pg, lport):
        self.run(cmd=f'add port_group {pg.name} ports {lport.uuid}')

    def port_group_add_ports(self, pg, lports):
        MAX_PORTS_IN_BATCH = 500
        for i in range(0, len(lports), MAX_PORTS_IN_BATCH):
            lports_slice = lports[i : i + MAX_PORTS_IN_BATCH]
            port_uuids = " ".join(p.uuid for p in lports_slice)
            self.run(cmd=f'add port_group {pg.name} ports {port_uuids}')

    def port_group_del(self, pg):
        self.run(cmd=f'destroy port_group {pg.name}')

    def address_set_create(self, name):
        self.run(cmd=f'create address_set name={name}')
        return AddressSet(name=name)

    def address_set_add(self, addr_set, addr):
        cmd = f'add Address_Set {addr_set.name} addresses \'\"{addr}\"\''
        self.run(cmd=cmd)

    def address_set_add_addrs(self, addr_set, addrs):
        MAX_ADDRS_IN_BATCH = 500
        for i in range(0, len(addrs), MAX_ADDRS_IN_BATCH):
            addrs_slice = [
                f'\"{a}\"' for a in addrs[i : i + MAX_ADDRS_IN_BATCH]
            ]
            addrs_str = ','.join(addrs_slice)
            cmd = f'add Address_Set {addr_set.name} addresses \'{addrs_str}\''
            self.run(cmd=cmd)

    def address_set_remove(self, addr_set, addr):
        cmd = f'remove Address_Set {addr_set.name} addresses \'\"{addr}\"\''
        self.run(cmd=cmd)

    def address_set_del(self, addr_set):
        self.run(cmd=f'destroy Address_Set {addr_set.name}')

    def acl_add(
        self,
        name="",
        direction="from-lport",
        priority=100,
        entity="switch",
        match="",
        verdict="allow",
    ):
        self.run(
            cmd=f'--type={entity} acl-add {name} '
            f'{direction} {priority} "{match}" {verdict}'
        )

    def route_add(self, router, network, gw, policy=None):
        prefix = f'--policy={policy} ' if policy else ''
        if network.n4 and gw.ip4:
            self.run(
                cmd=f'{prefix} lr-route-add {router.uuid} '
                f'{network.n4} {gw.ip4}'
            )
        if network.n6 and gw.ip6:
            self.run(
                cmd=f'{prefix} lr-route-add {router.uuid} '
                f'{network.n6} {gw.ip6}'
            )

    def nat_add(self, router, external_ip, logical_net, nat_type="snat"):
        if external_ip.ip4 and logical_net.n4:
            self.run(
                cmd=f'lr-nat-add {router.uuid} '
                f'{nat_type} {external_ip.ip4} '
                f'{logical_net.n4}'
            )
        if external_ip.ip6 and logical_net.n6:
            self.run(
                cmd=f'lr-nat-add {router.uuid} '
                f'{nat_type} {external_ip.ip6} '
                f'{logical_net.n6}'
            )

    def create_lb(self, name, protocol):
        lb_name = f"{name}-{protocol}"
        cmd = f"create Load_Balancer name={lb_name} protocol={protocol}"

        stdout = StringIO()
        self.run(cmd=cmd, stdout=stdout)
        return LoadBalancer(name=lb_name, uuid=stdout.getvalue().strip())

    def create_lbg(self, name):
        cmd = f'create Load_Balancer_Group name={name}'
        stdout = StringIO()
        self.run(cmd=cmd, stdout=stdout)
        return LoadBalancerGroup(name=name, uuid=stdout.getvalue().strip())

    def lbg_add_lb(self, lbg, lb):
        cmd = f'add Load_Balancer_Group {lbg.uuid} load_balancer {lb.uuid}'
        self.run(cmd=cmd)

    def ls_add_lbg(self, ls, lbg):
        cmd = f'add Logical_Switch {ls.uuid} load_balancer_group {lbg.uuid}'
        self.run(cmd=cmd)

    def lr_add_lbg(self, lr, lbg):
        cmd = f'add Logical_Router {lr.uuid} load_balancer_group {lbg.uuid}'
        self.run(cmd=cmd)

    def lr_set_options(self, router, options):
        opt = ''
        for key, value in options.items():
            opt = opt + f' options:{key}={value}'

        self.run(f'set Logical_Router {router.name} {opt}')

    def lb_set_vips(self, lb, vips):
        vip_str = ''
        for vip, backends in vips.items():
            vip_str += f'vips:\\"{vip}\\"=\\"{",".join(backends)}\\" '
        cmd = f"set Load_Balancer {lb.uuid} {vip_str}"
        self.run(cmd=cmd)

    def lb_clear_vips(self, lb):
        self.run(cmd=f'clear Load_Balancer {lb.uuid} vips')

    def lb_add_to_routers(self, lb, routers):
        cmd = ' -- '.join([f'lr-lb-add {r} {lb.uuid}' for r in routers])
        self.run(cmd=cmd)

    def lb_add_to_switches(self, lb, switches):
        cmd = ' -- '.join([f'ls-lb-add {s} {lb.uuid}' for s in switches])
        self.run(cmd=cmd)

    def lb_remove_from_routers(self, lb, routers):
        cmd = ' -- '.join([f'lr-lb-del {r} {lb.uuid}' for r in routers])
        self.run(cmd=cmd)

    def lb_remove_from_switches(self, lb, switches):
        cmd = ' -- '.join([f'ls-lb-del {s} {lb.uuid}' for s in switches])
        self.run(cmd=cmd)

    def wait_until(self, cmd=""):
        self.run("wait-until " + cmd)

    def sync(self, wait="hv", timeout=DEFAULT_CTL_TIMEOUT):
        self.run(
            f'--timeout={timeout} --wait={wait} sync', timeout=(timeout + 1)
        )

    def start_daemon(self, nb_cluster_ips, enable_ssl):
        if enable_ssl:
            remote = ','.join([f'ssl:{ip}:6641' for ip in nb_cluster_ips])
        else:
            remote = ','.join([f'tcp:{ip}:6641' for ip in nb_cluster_ips])
        # FIXME: hardcoded args, are these really an issue?
        cmd = (
            f'--detach --pidfile --log-file --db={remote} '
            f'-p /opt/ovn/ovn-privkey.pem -c /opt/ovn/ovn-cert.pem '
            f'-C /opt/ovn/pki/switchca/cacert.pem'
        )
        stdout = StringIO()
        self.run(cmd=cmd, stdout=stdout)
        self.socket = stdout.getvalue().rstrip()

    def stop_daemon(self):
        if len(self.socket):
            self.sb.run(cmd=f'ovs-appctl -t {self.socket} exit')


class OvnSbctl:
    def __init__(self, sb):
        self.sb = sb

    def run(self, cmd="", stdout=None, timeout=DEFAULT_CTL_TIMEOUT):
        self.sb.run(
            cmd="ovn-sbctl --no-leader-only " + cmd,
            stdout=stdout,
            timeout=timeout,
        )

    def set_inactivity_probe(self, value):
        self.run(f'set Connection . inactivity_probe={value}')

    def chassis_bound(self, chassis=""):
        cmd = f'--bare --columns _uuid find chassis name={chassis}'
        stdout = StringIO()
        self.run(cmd=cmd, stdout=stdout)
        return len(stdout.getvalue().splitlines()) == 1
