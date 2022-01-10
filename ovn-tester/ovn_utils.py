import logging
from collections import namedtuple
from io import StringIO

log = logging.getLogger(__name__)

LRouter = namedtuple('LRouter', ['uuid', 'name'])
LRPort = namedtuple('LRPort', ['name'])
LSwitch = namedtuple('LSwitch', ['uuid', 'name', 'cidr'])
LSPort = namedtuple('LSPort',
                    ['name', 'mac', 'ip', 'plen', 'gw', 'ext_gw',
                     'metadata', 'passive', 'uuid'])
PortGroup = namedtuple('PortGroup', ['name'])
AddressSet = namedtuple('AddressSet', ['name'])
LoadBalancer = namedtuple('LoadBalancer', ['name', 'uuid'])
LoadBalancerGroup = namedtuple('LoadBalancerGroup', ['name', 'uuid'])


class OvsVsctl:
    def __init__(self, sb):
        self.sb = sb

    def run(self, cmd="", prefix="ovs-vsctl ", stdout=None):
        self.sb.run(cmd=prefix + cmd, stdout=stdout)

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
        cmd = f'bash -c \'ip netns add {lport.name} ; ' \
              f'ip link set {lport.name} netns {lport.name} ; ' \
              f'ip -n {lport.name} link set {lport.name} ' \
              f'address {lport.mac} ; ' \
              f'ip -n {lport.name} addr add {lport.ip}/{lport.plen} ' \
              f'dev {lport.name} ; ' \
              f'ip -n {lport.name} link set {lport.name} up ; ' \
              f'ip -n {lport.name} route add default via {lport.gw}\''
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

    def run(self, cmd="", stdout=None):
        prefix = "ovn-nbctl "
        if len(self.socket):
            prefix = prefix + "-u " + self.socket + " "
        self.sb.run(cmd=prefix + cmd, stdout=stdout)

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

    def lr_port_add(self, router, name, mac, ip, plen):
        self.run(cmd=f'lrp-add {router.uuid} {name} {mac} {ip}/{plen}')
        return LRPort(name=name)

    def lr_port_set_gw_chassis(self, rp, chassis, priority=10):
        log.info(f'Setting gw chassis {chassis} for router port {rp.name}')
        self.run(cmd=f'lrp-set-gateway-chassis {rp.name} {chassis} {priority}')

    def ls_add(self, name, cidr):
        log.info(f'Creating lswitch {name}')

        cmd = f'create Logical_Switch name={name}'
        stdout = StringIO()
        self.run(cmd=cmd, stdout=stdout)
        return LSwitch(name=name, cidr=cidr, uuid=stdout.getvalue().strip())

    def ls_port_add(self, lswitch, name, router_port=None,
                    mac=None, ip=None, plen=None, gw=None, ext_gw=None,
                    metadata=None, passive=False, security=False):
        cmd = f'lsp-add {lswitch.uuid} {name}'
        if router_port:
            cmd += \
                f' -- lsp-set-type {name} router' \
                f' -- lsp-set-addresses {name} router' \
                f' -- lsp-set-options {name} router-port={router_port.name}'
        elif mac or ip:
            addresses = []
            if mac:
                addresses.append(mac)
            if ip:
                addresses.append(str(ip))
            addresses = " ".join(addresses)
            cmd += f' -- lsp-set-addresses {name} \"{addresses}\"'
            if security:
                cmd += f' -- lsp-set-port-security {name} \"{addresses}\"'
        self.run(cmd=cmd)
        stdout = StringIO()
        self.run(cmd=f'get logical_switch_port {name} _uuid', stdout=stdout)
        uuid = stdout.getvalue().strip()
        return LSPort(name=name, mac=mac, ip=ip, plen=plen,
                      gw=gw, ext_gw=ext_gw, metadata=metadata,
                      passive=passive, uuid=uuid)

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
            lports_slice = lports[i:i + MAX_PORTS_IN_BATCH]
            port_uuids = " ".join(p.uuid for p in lports_slice)
            self.run(cmd=f'add port_group {pg.name} ports {port_uuids}')

    def port_group_del(self, pg):
        self.run(cmd=f'destroy port_group {pg.name}')

    def address_set_create(self, name):
        self.run(cmd=f'create address_set name={name}')
        return AddressSet(name=name)

    def address_set_add(self, addr_set, addr):
        cmd = f'add Address_Set {addr_set.name} addresses \"{addr}\"'
        self.run(cmd=cmd)

    def address_set_add_addrs(self, addr_set, addrs):
        MAC_ADDRS_IN_BATCH = 500
        for i in range(0, len(addrs), MAC_ADDRS_IN_BATCH):
            addrs_slice = ' '.join(addrs[i:i + MAC_ADDRS_IN_BATCH])
            cmd = \
                f'add Address_Set {addr_set.name} addresses \"{addrs_slice}\"'
            self.run(cmd=cmd)

    def address_set_remove(self, addr_set, addr):
        cmd = f'remove Address_Set {addr_set.name} addresses \"{addr}\"'
        self.run(cmd=cmd)

    def address_set_del(self, addr_set):
        self.run(cmd=f'destroy Address_Set {addr_set.name}')

    def acl_add(self, name="", direction="from-lport", priority=100,
                entity="switch", match="", verdict="allow"):
        self.run(cmd=f'--type={entity} acl-add {name} '
                 f'{direction} {priority} "{match}" {verdict}')

    def route_add(self, router, network="0.0.0.0/0", gw="", policy=None):
        if policy:
            cmd = f'--policy={policy} lr-route-add ' \
                f'{router.uuid} {network} {gw}'
        else:
            cmd = f'lr-route-add {router.uuid} {network} {gw}'
        self.run(cmd=cmd)

    def nat_add(self, router, nat_type="snat", external_ip="", logical_ip=""):
        self.run(cmd=f'lr-nat-add {router.uuid} '
                 f'{nat_type} {external_ip} {logical_ip}')

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

    def sync(self, wait="hv"):
        self.run(f'--wait={wait} sync')

    def start_daemon(self, nb_cluster_ips, enable_ssl):
        if enable_ssl:
            remote = ','.join([f'ssl:{ip}:6641' for ip in nb_cluster_ips])
        else:
            remote = ','.join([f'tcp:{ip}:6641' for ip in nb_cluster_ips])
        # FIXME: hardcoded args, are these really an issue?
        cmd = f'--detach --pidfile --log-file --db={remote} ' \
            f'-p /opt/ovn/ovn-privkey.pem -c /opt/ovn/ovn-cert.pem ' \
            f'-C /opt/ovn/pki/switchca/cacert.pem'
        stdout = StringIO()
        self.run(cmd=cmd, stdout=stdout)
        self.socket = stdout.getvalue().rstrip()

    def stop_daemon(self):
        if len(self.socket):
            self.sb.run(cmd=f'ovs-appctl -t {self.socket} exit')


class OvnSbctl:
    def __init__(self, sb):
        self.sb = sb

    def run(self, cmd="", stdout=None):
        self.sb.run(cmd="ovn-sbctl --no-leader-only " + cmd, stdout=stdout)

    def set_inactivity_probe(self, value):
        self.run(f'set Connection . inactivity_probe={value}')

    def chassis_bound(self, chassis=""):
        cmd = f'--bare --columns _uuid find chassis name={chassis}'
        stdout = StringIO()
        self.run(cmd=cmd, stdout=stdout)
        return len(stdout.getvalue().splitlines()) == 1
