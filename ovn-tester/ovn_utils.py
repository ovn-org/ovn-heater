import logging
from collections import namedtuple
from io import StringIO

log = logging.getLogger(__name__)

LRouter = namedtuple('LRouter', ['name'])
LRPort = namedtuple('LRPort', ['name'])
LSwitch = namedtuple('LSwitch', ['name', 'cidr'])
LSPort = namedtuple('LSPort',
                    ['name', 'mac', 'ip', 'plen', 'gw', 'ext_gw',
                     'metadata', 'passive', 'uuid'])
PortGroup = namedtuple('PortGroup', ['name'])
AddressSet = namedtuple('AddressSet', ['name'])
LoadBalancer = namedtuple('LoadBalancer', ['name', 'uuid'])


class OvsVsctl:
    def __init__(self, sb):
        self.sb = sb

    async def run(self, cmd="", prefix="ovs-vsctl ", stdout=None):
        await self.sb.run(cmd=prefix + cmd, stdout=stdout)

    async def add_port(self, port, bridge, internal=True, ifaceid=None):
        name = port.name
        cmd = f'add-port {bridge} {name}'
        if internal:
            cmd += f' -- set interface {name} type=internal'
        if ifaceid:
            cmd += f' -- set Interface {name} external_ids:iface-id={ifaceid}'
        await self.run(cmd=cmd)

    async def del_port(self, port):
        await self.run(f'del-port {port.name}')

    async def bind_vm_port(self, lport):
        cmd = f'bash -c \'ip netns add {lport.name} ; ' \
              f'ip link set {lport.name} netns {lport.name} ; ' \
              f'ip -n {lport.name} link set {lport.name} ' \
              f'address {lport.mac} ; ' \
              f'ip -n {lport.name} addr add {lport.ip}/{lport.plen} ' \
              f'dev {lport.name} ; ' \
              f'ip -n {lport.name} link set {lport.name} up ; ' \
              f'ip -n {lport.name} route add default via {lport.gw}\''
        await self.run(cmd, prefix="")

    async def unbind_vm_port(self, lport):
        await self.run(f'ip netns del {lport.name}', prefix='')


class OvnNbctl:
    def __init__(self, sb):
        self.sb = sb
        self.socket = ""

    def __del__(self):
        # FIXME: the SSH connection might have already been closed here..
        # self.stop_daemon()
        pass

    async def run(self, cmd="", stdout=None):
        prefix = "ovn-nbctl "
        if len(self.socket):
            prefix = prefix + "-u " + self.socket + " "
        await self.sb.run(cmd=prefix + cmd, stdout=stdout)

    async def set_global(self, option, value):
        await self.run(f'set NB_Global . options:{option}={value}')

    async def set_inactivity_probe(self, value):
        await self.run(f'set Connection . inactivity_probe={value}')

    async def lr_add(self, name):
        log.info(f'Creating lrouter {name}')
        await self.run(cmd=f'lr-add {name}')
        return LRouter(name=name)

    async def lr_port_add(self, router, name, mac, ip, plen):
        await self.run(cmd=f'lrp-add {router.name} {name} {mac} {ip}/{plen}')
        return LRPort(name=name)

    async def lr_port_set_gw_chassis(self, rp, chassis, priority=10):
        log.info(f'Setting gw chassis {chassis} for router port {rp.name}')
        await self.run(cmd=f'lrp-set-gateway-chassis {rp.name} '
                       f'{chassis} {priority}')

    async def ls_add(self, name, cidr):
        log.info(f'Creating lswitch {name}')
        await self.run(cmd=f'ls-add {name}')
        return LSwitch(name=name, cidr=cidr)

    async def ls_port_add(self, lswitch, name, router_port=None,
                          mac=None, ip=None, plen=None, gw=None, ext_gw=None,
                          metadata=None, passive=False, security=False):
        cmd = f'lsp-add {lswitch.name} {name}'
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
        await self.run(cmd=cmd)
        stdout = StringIO()
        await self.run(cmd=f'get logical_switch_port {name} _uuid',
                       stdout=stdout)
        uuid = stdout.getvalue().strip()
        return LSPort(name=name, mac=mac, ip=ip, plen=plen,
                      gw=gw, ext_gw=ext_gw, metadata=metadata,
                      passive=passive, uuid=uuid)

    async def ls_port_del(self, port):
        await self.run(cmd=f'lsp-del {port.name}')

    async def ls_port_set_set_options(self, port, options):
        await self.run(cmd=f'lsp-set-options {port.name} {options}')

    async def ls_port_set_set_type(self, port, lsp_type):
        await self.run(cmd=f'lsp-set-type {port.name} {lsp_type}')

    async def port_group_create(self, name):
        await self.run(cmd=f'create port_group name={name}')
        return PortGroup(name=name)

    async def port_group_add(self, pg, lport):
        await self.run(cmd=f'add port_group {pg.name} ports {lport.uuid}')

    async def port_group_add_ports(self, pg, lports):
        MAX_PORTS_IN_BATCH = 500
        for i in range(0, len(lports), MAX_PORTS_IN_BATCH):
            lports_slice = lports[i:i + MAX_PORTS_IN_BATCH]
            port_uuids = " ".join(p.uuid for p in lports_slice)
            await self.run(cmd=f'add port_group {pg.name} ports {port_uuids}')

    async def port_group_del(self, pg):
        await self.run(cmd=f'destroy port_group {pg.name}')

    async def address_set_create(self, name):
        await self.run(cmd=f'create address_set name={name}')
        return AddressSet(name=name)

    async def address_set_add(self, addr_set, addr):
        cmd = f'add Address_Set {addr_set.name} addresses \"{addr}\"'
        await self.run(cmd=cmd)

    async def address_set_add_addrs(self, addr_set, addrs):
        MAC_ADDRS_IN_BATCH = 500
        for i in range(0, len(addrs), MAC_ADDRS_IN_BATCH):
            addrs_slice = ' '.join(addrs[i:i + MAC_ADDRS_IN_BATCH])
            cmd = \
                f'add Address_Set {addr_set.name} addresses \"{addrs_slice}\"'
            await self.run(cmd=cmd)

    async def address_set_remove(self, addr_set, addr):
        cmd = f'remove Address_Set {addr_set.name} addresses \"{addr}\"'
        await self.run(cmd=cmd)

    async def address_set_del(self, addr_set):
        await self.run(cmd=f'destroy Address_Set {addr_set.name}')

    async def acl_add(self, name="", direction="from-lport", priority=100,
                      entity="switch", match="", verdict="allow"):
        await self.run(cmd=f'--type={entity} acl-add {name} '
                       f'{direction} {priority} "{match}" {verdict}')

    async def route_add(self, router, network="0.0.0.0/0", gw="", policy=None):
        if policy:
            cmd = f'--policy={policy} lr-route-add ' \
                f'{router.name} {network} {gw}'
        else:
            cmd = f'lr-route-add {router.name} {network} {gw}'
        await self.run(cmd=cmd)

    async def nat_add(self, router, nat_type="snat", external_ip="",
                      logical_ip=""):
        await self.run(cmd=f'lr-nat-add {router.name} '
                       f'{nat_type} {external_ip} {logical_ip}')

    async def create_lb(self, name, protocol):
        lb_name = f"{name}-{protocol}"
        cmd = f"create Load_Balancer name={lb_name} protocol={protocol}"

        stdout = StringIO()
        await self.run(cmd=cmd, stdout=stdout)
        return LoadBalancer(name=lb_name, uuid=stdout.getvalue().strip())

    async def lb_set_vips(self, lb_uuid, vips):
        vip_str = ''
        for vip, backends in vips.items():
            vip_str += f'vips:\\"{vip}\\"=\\"{",".join(backends)}\\" '
        cmd = f"set Load_Balancer {lb_uuid} {vip_str}"
        await self.run(cmd=cmd)

    async def lb_clear_vips(self, lb_uuid):
        await self.run(cmd=f'clear Load_Balancer {lb_uuid} vips')

    async def lb_add_to_routers(self, lb_uuid, routers):
        cmd = ' -- '.join([f'lr-lb-add {r} {lb_uuid}' for r in routers])
        await self.run(cmd=cmd)

    async def lb_add_to_switches(self, lb_uuid, switches):
        cmd = ' -- '.join([f'ls-lb-add {s} {lb_uuid}' for s in switches])
        await self.run(cmd=cmd)

    async def lb_remove_from_routers(self, lb_uuid, routers):
        cmd = ' -- '.join([f'lr-lb-del {r} {lb_uuid}' for r in routers])
        await self.run(cmd=cmd)

    async def lb_remove_from_switches(self, lb_uuid, switches):
        cmd = ' -- '.join([f'ls-lb-del {s} {lb_uuid}' for s in switches])
        await self.run(cmd=cmd)

    async def wait_until(self, cmd=""):
        await self.run("wait-until " + cmd)

    async def sync(self, wait="hv"):
        await self.run(f'--wait={wait} sync')

    async def start_daemon(self, nb_cluster_ips, enable_ssl):
        if enable_ssl:
            remote = ','.join([f'ssl:{ip}:6641' for ip in nb_cluster_ips])
        else:
            remote = ','.join([f'tcp:{ip}:6641' for ip in nb_cluster_ips])
        # FIXME: hardcoded args, are these really an issue?
        cmd = f'--detach --pidfile --log-file --db={remote} ' \
            f'-p /opt/ovn/ovn-privkey.pem -c /opt/ovn/ovn-cert.pem ' \
            f'-C /opt/ovn/pki/switchca/cacert.pem'
        stdout = StringIO()
        await self.run(cmd=cmd, stdout=stdout)
        self.socket = stdout.getvalue().rstrip()

    async def stop_daemon(self):
        if len(self.socket):
            await self.sb.run(cmd=f'ovs-appctl -t {self.socket} exit')


class OvnSbctl:
    def __init__(self, sb):
        self.sb = sb

    async def run(self, cmd="", stdout=None):
        await self.sb.run(cmd="ovn-sbctl --no-leader-only " + cmd,
                          stdout=stdout)

    async def set_inactivity_probe(self, value):
        await self.run(f'set Connection . inactivity_probe={value}')

    async def chassis_bound(self, chassis=""):
        cmd = f'--bare --columns _uuid find chassis name={chassis}'
        stdout = StringIO()
        await self.run(cmd=cmd, stdout=stdout)
        return len(stdout.getvalue().splitlines()) == 1
