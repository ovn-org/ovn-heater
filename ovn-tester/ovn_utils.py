from collections import namedtuple
from io import StringIO

LRouter = namedtuple('LRouter', ['name'])
LRPort = namedtuple('LRPort', ['name'])
LSwitch = namedtuple('LSwitch', ['name', 'cidr'])
LSPort = namedtuple('LSPort',
                    ['name', 'mac', 'ip', 'plen', 'gw', 'ext_gw',
                     'metadata', 'uuid'])
PortGroup = namedtuple('PortGroup', ['name'])
AddressSet = namedtuple('AddressSet', ['name'])
LoadBalancer = namedtuple('LoadBalancer', ['name', 'uuid'])


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
            cmd += \
                f' -- set Interface {name} external_ids:iface-id={ifaceid}' \
                f' -- set Interface {name} external_ids:iface-status=active' \
                f' -- set Interface {name} admin_state=up'
        self.run(cmd=cmd)

    def del_port(self, port):
        self.run(f'del-port {port.name}')

    def bind_vm_port(self, lport):
        cmd = f'bash -c \'ip netns add {lport.name} ; \
                ip link set {lport.name} netns {lport.name} ; \
                ip -n {lport.name} link set {lport.name} address {lport.mac} ; \
                ip -n {lport.name} addr add {lport.ip}/{lport.plen} dev {lport.name} ; \
                ip -n {lport.name} link set {lport.name} up ; \
                ip -n {lport.name} route add default via {lport.gw}\''
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

    def lr_add(self, name):
        print(f'***** creating lrouter {name} *****')
        self.run(cmd=f'lr-add {name}')
        return LRouter(name=name)

    def lr_port_add(self, router, name, mac, ip, plen):
        self.run(cmd=f'lrp-add {router.name} {name} {mac} {ip}/{plen}')
        return LRPort(name=name)

    def ls_add(self, name, cidr):
        print(f'***** creating lswitch {name} *****')
        self.run(cmd=f'ls-add {name}')
        return LSwitch(name=name, cidr=cidr)

    def ls_port_add(self, lswitch, name, router_port=None,
                    mac=None, ip=None, plen=None, gw=None, ext_gw=None,
                    metadata=None):
        self.run(cmd=f'lsp-add {lswitch.name} {name}')
        if router_port:
            cmd = \
                f'lsp-set-type {name} router' \
                f' -- lsp-set-addresses {name} router' \
                f' -- lsp-set-options {name} router-port={router_port.name}'
            self.run(cmd=cmd)
        elif mac or ip:
            cmd = f'lsp-set-addresses {name} \"'
            if mac:
                cmd += f'{str(mac)} '
            if ip:
                cmd += str(ip)
            cmd += "\""
            self.run(cmd=cmd)
        stdout = StringIO()
        self.run(cmd=f'get logical_switch_port {name} _uuid', stdout=stdout)
        uuid = stdout.getvalue()
        return LSPort(name=name, mac=mac, ip=ip, plen=plen,
                      gw=gw, ext_gw=ext_gw, metadata=metadata, uuid=uuid)

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

    def port_group_del(self, pg):
        self.run(cmd=f'destroy port_group {pg.name}')

    def address_set_create(self, name):
        self.run(cmd=f'create address_set name={name}')
        return AddressSet(name=name)

    def address_set_add(self, addr_set, addrs):
        cmd = f'add Address_Set {addr_set.name} addresses \"{addrs}\"'
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
                f'{router.name} {network} {gw}'
        else:
            cmd = f'lr-route-add {router.name} {network} {gw}'
        self.run(cmd=cmd)

    def nat_add(self, router, nat_type="snat", external_ip="", logical_ip=""):
        self.run(cmd=f'lr-nat-add {router.name} '
                 f'{nat_type} {external_ip} {logical_ip}')

    def create_lb(self, name, protocol):
        lb_name = f"{name}-{protocol}"
        cmd = f"create Load_Balancer name={lb_name} protocol={protocol}"

        stdout = StringIO()
        self.run(cmd=cmd, stdout=stdout)
        return LoadBalancer(name=lb_name, uuid=stdout.getvalue().strip())

    def lb_set_vips(self, lb_uuid, vips):
        vip_str = ''
        for vip, backends in vips.items():
            vip_str += f'vips:\\"{vip}\\"=\\"{",".join(backends)}\\" '
        cmd = f"set Load_Balancer {lb_uuid} {vip_str}"
        self.run(cmd=cmd)

    def lb_clear_vips(self, lb_uuid):
        self.run(cmd=f'clear Load_Balancer {lb_uuid} vips')

    def lb_add_to_router(self, lb_uuid, router):
        cmd = f"lr-lb-add {router} {lb_uuid}"
        self.run(cmd=cmd)

    def lb_add_to_switch(self, lb_uuid, switch):
        cmd = f"ls-lb-add {switch} {lb_uuid}"
        self.run(cmd=cmd)

    def lb_remove_from_router(self, lb_uuid, router):
        cmd = f"lr-lb-del {router} {lb_uuid}"
        self.run(cmd=cmd)

    def lb_remove_from_switch(self, lb_uuid, switch):
        cmd = f"ls-lb-del {switch} {lb_uuid}"
        self.run(cmd=cmd)

    def wait_until(self, cmd=""):
        self.run("wait-until " + cmd)

    def sync(self, wait="hv"):
        self.run(f'--wait={wait} sync')

    def start_daemon(self, nb_cluster_ips):
        remote = ','.join([f'ssl:{ip}:6641' for ip in nb_cluster_ips])
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

    def chassis_bound(self, chassis=""):
        cmd = f'--bare --columns _uuid find chassis name={chassis}'
        stdout = StringIO()
        self.run(cmd=cmd, stdout=stdout)
        return len(stdout.getvalue().splitlines()) == 1
