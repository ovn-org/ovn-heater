import logging
import ovn_exceptions
import ovn_sandbox
import ovn_stats
import ovn_utils
import ovn_load_balancer as lb
import time
import netaddr
from collections import namedtuple
from collections import defaultdict
from randmac import RandMac
from datetime import datetime

log = logging.getLogger(__name__)


ClusterConfig = namedtuple(
    'ClusterConfig',
    [
        'monitor_all',
        'logical_dp_groups',
        'clustered_db',
        'datapath_type',
        'raft_election_to',
        'northd_probe_interval',
        'northd_threads',
        'db_inactivity_probe',
        'node_net',
        'enable_ssl',
        'node_remote',
        'node_timeout_s',
        'internal_net',
        'external_net',
        'gw_net',
        'cluster_net',
        'n_workers',
        'n_relays',
        'vips',
        'vips6',
        'vip_subnet',
        'static_vips',
        'static_vips6',
        'use_ovsdb_etcd',
        'ssl_private_key',
        'ssl_cert',
        'ssl_cacert',
    ],
)


BrExConfig = namedtuple('BrExConfig', ['physical_net'])


class Node(ovn_sandbox.Sandbox):
    def __init__(self, phys_node, container, mgmt_net, mgmt_ip):
        super(Node, self).__init__(phys_node, container)
        self.container = container
        self.mgmt_net = mgmt_net
        self.mgmt_ip = mgmt_ip


class CentralNode(Node):
    def __init__(
        self, phys_node, db_containers, relay_containers, mgmt_net, mgmt_ip
    ):
        super(CentralNode, self).__init__(
            phys_node, db_containers[0], mgmt_net, mgmt_ip
        )
        self.db_containers = db_containers
        self.relay_containers = relay_containers

    def start(self, cluster_cfg):
        log.info('Configuring central node')
        self.set_raft_election_timeout(cluster_cfg.raft_election_to)
        self.enable_trim_on_compaction()
        self.set_northd_threads(cluster_cfg.northd_threads)

    def set_northd_threads(self, n_threads):
        log.info(f'Configuring northd to use {n_threads} threads')
        for container in self.db_containers:
            self.phys_node.run(
                f'docker exec {container} ovn-appctl -t '
                f'ovn-northd parallel-build/set-n-threads '
                f'{n_threads}'
            )

    def set_raft_election_timeout(self, timeout_s):
        for timeout in range(1000, (timeout_s + 1) * 1000, 1000):
            log.info(f'Setting RAFT election timeout to {timeout}ms')
            self.run(
                cmd=f'ovs-appctl -t '
                f'/run/ovn/ovnnb_db.ctl cluster/change-election-timer '
                f'OVN_Northbound {timeout}'
            )
            self.run(
                cmd=f'ovs-appctl -t '
                f'/run/ovn/ovnsb_db.ctl cluster/change-election-timer '
                f'OVN_Southbound {timeout}'
            )
            time.sleep(1)

    def enable_trim_on_compaction(self):
        log.info('Setting DB trim-on-compaction')
        for db_container in self.db_containers:
            self.phys_node.run(
                f'docker exec {db_container} ovs-appctl -t '
                f'/run/ovn/ovnnb_db.ctl '
                f'ovsdb-server/memory-trim-on-compaction on'
            )
            self.phys_node.run(
                f'docker exec {db_container} ovs-appctl -t '
                f'/run/ovn/ovnsb_db.ctl '
                f'ovsdb-server/memory-trim-on-compaction on'
            )
        for relay_container in self.relay_containers:
            self.phys_node.run(
                f'docker exec {relay_container} ovs-appctl -t '
                f'/run/ovn/ovnsb_db.ctl '
                f'ovsdb-server/memory-trim-on-compaction on'
            )

    def get_connection_string(self, cluster_cfg, port):
        protocol = "ssl" if cluster_cfg.enable_ssl else "tcp"
        ip = self.mgmt_ip
        num_conns = 3 if cluster_cfg.clustered_db else 1
        conns = [f"{protocol}:{ip + idx}:{port}" for idx in range(num_conns)]
        return ",".join(conns)

    def central_containers(self):
        return self.db_containers


class WorkerNode(Node):
    def __init__(
        self,
        phys_node,
        container,
        mgmt_net,
        mgmt_ip,
        int_net,
        ext_net,
        gw_net,
        unique_id,
    ):
        super(WorkerNode, self).__init__(
            phys_node, container, mgmt_net, mgmt_ip
        )
        self.int_net = int_net
        self.ext_net = ext_net
        self.gw_net = gw_net
        self.id = unique_id
        self.switch = None
        self.gw_router = None
        self.ext_switch = None
        self.lports = []
        self.next_lport_index = 0
        self.vsctl = None

    def start(self, cluster_cfg):
        self.vsctl = ovn_utils.OvsVsctl(
            self,
            self.get_connection_string(cluster_cfg, 6640),
            cluster_cfg.db_inactivity_probe // 1000,
        )

    @ovn_stats.timeit
    def connect(self, cluster_cfg):
        log.info(
            f'Connecting worker {self.container}: '
            f'ovn-remote = {cluster_cfg.node_remote}'
        )
        self.vsctl.set_global_external_id(
            'ovn-remote', f'{cluster_cfg.node_remote}'
        )

    def configure_localnet(self, physical_net):
        log.info(f'Creating localnet on {self.container}')
        self.vsctl.set_global_external_id(
            'ovn-bridge-mappings', f'{physical_net}:br-ex'
        )

    def configure(self, physical_net):
        self.configure_localnet(physical_net)
        phys_ctl = ovn_utils.PhysCtl(self)
        phys_ctl.external_host_provision(
            ip=self.ext_net.reverse(2), gw=self.ext_net.reverse()
        )

    @ovn_stats.timeit
    def wait(self, sbctl, timeout_s):
        for _ in range(timeout_s * 10):
            if sbctl.chassis_bound(self.container):
                return
            time.sleep(0.1)
        raise ovn_exceptions.OvnChassisTimeoutException()

    @ovn_stats.timeit
    def provision(self, cluster):
        self.connect(cluster.cluster_cfg)
        self.wait(cluster.sbctl, cluster.cluster_cfg.node_timeout_s)

        # Create a node switch and connect it to the cluster router.
        self.switch = cluster.nbctl.ls_add(
            f'lswitch-{self.container}', net_s=self.int_net
        )
        lrp_name = f'rtr-to-node-{self.container}'
        ls_rp_name = f'node-to-rtr-{self.container}'
        self.rp = cluster.nbctl.lr_port_add(
            cluster.router, lrp_name, RandMac(), self.int_net.reverse()
        )
        self.ls_rp = cluster.nbctl.ls_port_add(
            self.switch, ls_rp_name, self.rp
        )

        # Make the lrp as distributed gateway router port.
        cluster.nbctl.lr_port_set_gw_chassis(self.rp, self.container)

        # Create a gw router and connect it to the cluster join switch.
        self.gw_router = cluster.nbctl.lr_add(f'gwrouter-{self.container}')
        cluster.nbctl.lr_set_options(
            self.gw_router,
            {
                'always_learn_from_arp_request': 'false',
                'dynamic_neigh_routers': 'true',
                'chassis': self.container,
                'lb_force_snat_ip': 'router_ip',
                'snat-ct-zone': 0,
            },
        )
        join_grp_name = f'gw-to-join-{self.container}'
        join_ls_grp_name = f'join-to-gw-{self.container}'

        gr_gw = self.gw_net.reverse(self.id + 2)
        self.gw_rp = cluster.nbctl.lr_port_add(
            self.gw_router, join_grp_name, RandMac(), gr_gw
        )
        self.join_gw_rp = cluster.nbctl.ls_port_add(
            cluster.join_switch, join_ls_grp_name, self.gw_rp
        )

        # Create an external switch connecting the gateway router to the
        # physnet.
        self.ext_switch = cluster.nbctl.ls_add(
            f'ext-{self.container}', net_s=self.ext_net
        )
        ext_lrp_name = f'gw-to-ext-{self.container}'
        ext_ls_rp_name = f'ext-to-gw-{self.container}'
        self.ext_rp = cluster.nbctl.lr_port_add(
            self.gw_router, ext_lrp_name, RandMac(), self.ext_net.reverse()
        )
        self.ext_gw_rp = cluster.nbctl.ls_port_add(
            self.ext_switch, ext_ls_rp_name, self.ext_rp
        )

        # Configure physnet.
        self.physnet_port = cluster.nbctl.ls_port_add(
            self.ext_switch,
            f'provnet-{self.container}',
            localnet=True,
        )
        cluster.nbctl.ls_port_set_set_type(self.physnet_port, 'localnet')
        cluster.nbctl.ls_port_set_set_options(
            self.physnet_port, f'network_name={cluster.brex_cfg.physical_net}'
        )

        # Route for traffic entering the cluster.
        cluster.nbctl.route_add(
            self.gw_router, cluster.net, self.gw_net.reverse()
        )

        # Default route to get out of cluster via physnet.
        cluster.nbctl.route_add(
            self.gw_router,
            ovn_utils.DualStackSubnet(
                netaddr.IPNetwork("0.0.0.0/0"), netaddr.IPNetwork("::/0")
            ),
            self.ext_net.reverse(2),
        )

        # Route for traffic that needs to exit the cluster
        # (via gw router).
        cluster.nbctl.route_add(
            cluster.router, self.int_net, gr_gw, policy="src-ip"
        )

        # SNAT traffic leaving the cluster.
        cluster.nbctl.nat_add(self.gw_router, gr_gw, cluster.net)

    @ovn_stats.timeit
    def provision_port(self, cluster, passive=False):
        name = f'lp-{self.id}-{self.next_lport_index}'

        log.info(f'Creating lport {name}')
        lport = cluster.nbctl.ls_port_add(
            self.switch,
            name,
            mac=str(RandMac()),
            ip=self.int_net.forward(self.next_lport_index + 1),
            gw=self.int_net.reverse(),
            ext_gw=self.ext_net.reverse(2),
            metadata=self,
            passive=passive,
            security=True,
        )

        self.lports.append(lport)
        self.next_lport_index += 1
        return lport

    @ovn_stats.timeit
    def unprovision_port(self, cluster, port):
        cluster.nbctl.ls_port_del(port)
        self.unbind_port(port)
        self.lports.remove(port)

    @ovn_stats.timeit
    def provision_load_balancers(self, cluster, ports, global_cfg):
        # Add one port IP as a backend to the cluster load balancer.
        if global_cfg.run_ipv4:
            port_ips = (
                f'{port.ip}:{DEFAULT_BACKEND_PORT}'
                for port in ports
                if port.ip is not None
            )
            cluster_vips = cluster.cluster_cfg.vips.keys()
            cluster.load_balancer.add_backends_to_vip(port_ips, cluster_vips)
            cluster.load_balancer.add_to_switches([self.switch.name])
            cluster.load_balancer.add_to_routers([self.gw_router.name])

        if global_cfg.run_ipv6:
            port_ips6 = (
                f'[{port.ip6}]:{DEFAULT_BACKEND_PORT}'
                for port in ports
                if port.ip6 is not None
            )
            cluster_vips6 = cluster.cluster_cfg.vips6.keys()
            cluster.load_balancer6.add_backends_to_vip(
                port_ips6, cluster_vips6
            )
            cluster.load_balancer6.add_to_switches([self.switch.name])
            cluster.load_balancer6.add_to_routers([self.gw_router.name])

        # GW Load balancer has no VIPs/backends configured on it, since
        # this load balancer is used for hostnetwork services. We're not
        # using those right now so the load blaancer is empty.
        if global_cfg.run_ipv4:
            self.gw_load_balancer = lb.OvnLoadBalancer(
                f'lb-{self.gw_router.name}', cluster.nbctl
            )
            self.gw_load_balancer.add_to_routers([self.gw_router.name])
        if global_cfg.run_ipv6:
            self.gw_load_balancer6 = lb.OvnLoadBalancer(
                f'lb-{self.gw_router.name}6', cluster.nbctl
            )
            self.gw_load_balancer6.add_to_routers([self.gw_router.name])

    @ovn_stats.timeit
    def bind_port(self, port):
        log.info(f'Binding lport {port.name} on {self.container}')
        self.vsctl.add_port(port, 'br-int', internal=True, ifaceid=port.name)
        # Skip creating a netns for "passive" ports, we won't be sending
        # traffic on those.
        if not port.passive:
            self.vsctl.bind_vm_port(port)

    @ovn_stats.timeit
    def unbind_port(self, port):
        if not port.passive:
            self.vsctl.unbind_vm_port(port)
        self.vsctl.del_port(port)

    def provision_ports(self, cluster, n_ports, passive=False):
        ports = [self.provision_port(cluster, passive) for i in range(n_ports)]
        for port in ports:
            self.bind_port(port)
        return ports

    def run_ping(self, cluster, src, dest):
        log.info(f'Pinging from {src} to {dest}')

        # FIXME
        # iputils is inconsistent when working with sub-second timeouts.
        # The behavior of ping's "-W" option changed a couple of times already.
        # https://github.com/iputils/iputils/issues/290
        # Until that's stable use "timeout 0.1s" instead.
        cmd = f'ip netns exec {src} timeout 0.1s ping -q -c 1 {dest}'
        start_time = datetime.now()
        while True:
            try:
                self.run(cmd=cmd, raise_on_error=True)
                break
            except ovn_exceptions.SSHError:
                pass

            duration = (datetime.now() - start_time).seconds
            if duration > cluster.cluster_cfg.node_timeout_s:
                log.error(
                    f'Timeout waiting for {src} ' f'to be able to ping {dest}'
                )
                raise ovn_exceptions.OvnPingTimeoutException()

    @ovn_stats.timeit
    def ping_port(self, cluster, port, dest):
        self.run_ping(cluster, port.name, dest)

    @ovn_stats.timeit
    def ping_external(self, cluster, port):
        if port.ip:
            self.run_ping(cluster, 'ext-ns', port.ip)
        if port.ip6:
            self.run_ping(cluster, 'ext-ns', port.ip6)

    def ping_ports(self, cluster, ports):
        for port in ports:
            if port.ip:
                self.ping_port(cluster, port, dest=port.ext_gw)
            if port.ip6:
                self.ping_port(cluster, port, dest=port.ext_gw6)

    def get_connection_string(self, cluster_cfg, port):
        protocol = "ssl" if cluster_cfg.enable_ssl else "tcp"
        offset = 0
        offset += 3 if cluster_cfg.clustered_db else 1
        offset += cluster_cfg.n_relays
        return f"{protocol}:{self.mgmt_ip + offset}:{port}"


ACL_DEFAULT_DENY_PRIO = 1
ACL_DEFAULT_ALLOW_ARP_PRIO = 2
ACL_NETPOL_ALLOW_PRIO = 3
DEFAULT_NS_VIP_SUBNET = netaddr.IPNetwork('30.0.0.0/16')
DEFAULT_NS_VIP_SUBNET6 = netaddr.IPNetwork('30::/32')
DEFAULT_VIP_PORT = 80
DEFAULT_BACKEND_PORT = 8080


class Namespace(object):
    def __init__(self, cluster, name, global_cfg):
        self.cluster = cluster
        self.nbctl = cluster.nbctl
        self.ports = []
        self.enforcing = False
        self.pg_def_deny_igr = self.nbctl.port_group_create(
            f'pg_deny_igr_{name}'
        )
        self.pg_def_deny_egr = self.nbctl.port_group_create(
            f'pg_deny_egr_{name}'
        )
        self.pg = self.nbctl.port_group_create(f'pg_{name}')
        self.addr_set4 = (
            self.nbctl.address_set_create(f'as_{name}')
            if global_cfg.run_ipv4
            else None
        )
        self.addr_set6 = (
            self.nbctl.address_set_create(f'as6_{name}')
            if global_cfg.run_ipv6
            else None
        )
        self.sub_as = []
        self.sub_pg = []
        self.load_balancer = None
        self.cluster.n_ns += 1
        self.name = name

    @ovn_stats.timeit
    def add_ports(self, ports):
        self.ports.extend(ports)
        # Always add port IPs to the address set but not to the PGs.
        # Simulate what OpenShift does, which is: create the port groups
        # when the first network policy is applied.
        if self.addr_set4:
            self.nbctl.address_set_add_addrs(
                self.addr_set4, [str(p.ip) for p in ports]
            )
        if self.addr_set6:
            self.nbctl.address_set_add_addrs(
                self.addr_set6, [str(p.ip6) for p in ports]
            )
        if self.enforcing:
            self.nbctl.port_group_add_ports(self.pg_def_deny_igr, ports)
            self.nbctl.port_group_add_ports(self.pg_def_deny_egr, ports)
            self.nbctl.port_group_add_ports(self.pg, ports)

    def unprovision(self):
        # ACLs are garbage collected by OVSDB as soon as all the records
        # referencing them are removed.
        self.cluster.unprovision_ports(self.ports)
        self.nbctl.port_group_del(self.pg_def_deny_igr)
        self.nbctl.port_group_del(self.pg_def_deny_egr)
        self.nbctl.port_group_del(self.pg)
        if self.addr_set4:
            self.nbctl.address_set_del(self.addr_set4)
        if self.addr_set6:
            self.nbctl.address_set_del(self.addr_set6)
        for pg in self.sub_pg:
            self.nbctl.port_group_del(pg)
        for addr_set in self.sub_as:
            self.nbctl.address_set_del(addr_set)

    def unprovision_ports(self, ports):
        '''Unprovision a subset of ports in the namespace without having to
        unprovision the entire namespace or any of its network policies.'''

        for port in ports:
            self.ports.remove(port)

        self.cluster.unprovision_ports(ports)

    def enforce(self):
        if self.enforcing:
            return
        self.enforcing = True
        self.nbctl.port_group_add_ports(self.pg_def_deny_igr, self.ports)
        self.nbctl.port_group_add_ports(self.pg_def_deny_egr, self.ports)
        self.nbctl.port_group_add_ports(self.pg, self.ports)

    def create_sub_ns(self, ports, global_cfg):
        n_sub_pgs = len(self.sub_pg)
        suffix = f'{self.name}_{n_sub_pgs}'
        pg = self.nbctl.port_group_create(f'sub_pg_{suffix}')
        self.nbctl.port_group_add_ports(pg, ports)
        self.sub_pg.append(pg)
        if global_cfg.run_ipv4:
            addr_set = self.nbctl.address_set_create(f'sub_as_{suffix}')
            self.nbctl.address_set_add_addrs(
                addr_set, [str(p.ip) for p in ports]
            )
            self.sub_as.append(addr_set)
        if global_cfg.run_ipv6:
            addr_set = self.nbctl.address_set_create(f'sub_as_{suffix}6')
            self.nbctl.address_set_add_addrs(
                addr_set, [str(p.ip6) for p in ports]
            )
            self.sub_as.append(addr_set)
        return n_sub_pgs

    @ovn_stats.timeit
    def default_deny(self, family):
        self.enforce()

        addr_set = f'self.addr_set{family}.name'
        self.nbctl.acl_add(
            self.pg_def_deny_igr.name,
            'to-lport',
            ACL_DEFAULT_DENY_PRIO,
            'port-group',
            f'ip4.src == \\${addr_set} && '
            f'outport == @{self.pg_def_deny_igr.name}',
            'drop',
        )
        self.nbctl.acl_add(
            self.pg_def_deny_egr.name,
            'to-lport',
            ACL_DEFAULT_DENY_PRIO,
            'port-group',
            f'ip4.dst == \\${addr_set} && '
            f'inport == @{self.pg_def_deny_egr.name}',
            'drop',
        )
        self.nbctl.acl_add(
            self.pg_def_deny_igr.name,
            'to-lport',
            ACL_DEFAULT_ALLOW_ARP_PRIO,
            'port-group',
            f'outport == @{self.pg_def_deny_igr.name} && arp',
            'allow',
        )
        self.nbctl.acl_add(
            self.pg_def_deny_egr.name,
            'to-lport',
            ACL_DEFAULT_ALLOW_ARP_PRIO,
            'port-group',
            f'inport == @{self.pg_def_deny_egr.name} && arp',
            'allow',
        )

    @ovn_stats.timeit
    def allow_within_namespace(self, family):
        self.enforce()

        addr_set = f'self.addr_set{family}.name'
        self.nbctl.acl_add(
            self.pg.name,
            'to-lport',
            ACL_NETPOL_ALLOW_PRIO,
            'port-group',
            f'ip4.src == \\${addr_set} && outport == @{self.pg.name}',
            'allow-related',
        )
        self.nbctl.acl_add(
            self.pg.name,
            'to-lport',
            ACL_NETPOL_ALLOW_PRIO,
            'port-group',
            f'ip4.dst == \\${addr_set} && inport == @{self.pg.name}',
            'allow-related',
        )

    @ovn_stats.timeit
    def allow_cross_namespace(self, ns, family):
        self.enforce()

        addr_set = f'self.addr_set{family}.name'
        self.nbctl.acl_add(
            self.pg.name,
            'to-lport',
            ACL_NETPOL_ALLOW_PRIO,
            'port-group',
            f'ip4.src == \\${addr_set} && outport == @{ns.pg.name}',
            'allow-related',
        )
        ns_addr_set = f'ns.addr_set{family}.name'
        self.nbctl.acl_add(
            self.pg.name,
            'to-lport',
            ACL_NETPOL_ALLOW_PRIO,
            'port-group',
            f'ip4.dst == \\${ns_addr_set} && inport == @{self.pg.name}',
            'allow-related',
        )

    @ovn_stats.timeit
    def allow_sub_namespace(self, src, dst, family):
        self.nbctl.acl_add(
            self.pg.name,
            'to-lport',
            ACL_NETPOL_ALLOW_PRIO,
            'port-group',
            f'ip{family}.src == \\${self.sub_as[src].name} && '
            f'outport == @{self.sub_pg[dst].name}',
            'allow-related',
        )
        self.nbctl.acl_add(
            self.pg.name,
            'to-lport',
            ACL_NETPOL_ALLOW_PRIO,
            'port-group',
            f'ip{family}.dst == \\${self.sub_as[dst].name} && '
            f'inport == @{self.sub_pg[src].name}',
            'allow-related',
        )

    @ovn_stats.timeit
    def allow_from_external(
        self, external_ips, include_ext_gw=False, family=4
    ):
        self.enforce()
        # If requested, include the ext-gw of the first port in the namespace
        # so we can check that this rule is enforced.
        if include_ext_gw:
            assert len(self.ports) > 0
            if family == 4 and self.ports[0].ext_gw:
                external_ips.append(self.ports[0].ext_gw)
            elif family == 6 and self.ports[0].ext_gw6:
                external_ips.append(self.ports[0].ext_gw6)
        ips = [str(ip) for ip in external_ips]
        self.nbctl.acl_add(
            self.pg.name,
            'to-lport',
            ACL_NETPOL_ALLOW_PRIO,
            'port-group',
            f'ip.{family} == {{{",".join(ips)}}} && '
            f'outport == @{self.pg.name}',
            'allow-related',
        )

    @ovn_stats.timeit
    def check_enforcing_internal(self):
        # "Random" check that first pod can reach last pod in the namespace.
        if len(self.ports) > 1:
            src = self.ports[0]
            dst = self.ports[-1]
            worker = src.metadata
            if src.ip:
                worker.ping_port(self.cluster, src, dst.ip)
            if src.ip6:
                worker.ping_port(self.cluster, src, dst.ip6)

    @ovn_stats.timeit
    def check_enforcing_external(self):
        if len(self.ports) > 0:
            dst = self.ports[0]
            worker = dst.metadata
            worker.ping_external(self.cluster, dst)

    @ovn_stats.timeit
    def check_enforcing_cross_ns(self, ns):
        if len(self.ports) > 0 and len(ns.ports) > 0:
            dst = ns.ports[0]
            src = self.ports[0]
            worker = src.metadata
            if src.ip and dst.ip:
                worker.ping_port(self.cluster, src, dst.ip)
            if src.ip6 and dst.ip6:
                worker.ping_port(self.cluster, src, dst.ip6)

    def create_load_balancer(self):
        self.load_balancer = lb.OvnLoadBalancer(f'lb_{self.name}', self.nbctl)

    @ovn_stats.timeit
    def provision_vips_to_load_balancers(self, backend_lists, version):
        vip_ns_subnet = DEFAULT_NS_VIP_SUBNET
        if version == 6:
            vip_ns_subnet = DEFAULT_NS_VIP_SUBNET6
        vip_net = vip_ns_subnet.next(self.cluster.n_ns)
        n_vips = len(self.load_balancer.vips.keys())
        vip_ip = vip_net.ip.__add__(n_vips + 1)

        if version == 6:
            vips = {
                f'[{vip_ip + i}]:{DEFAULT_VIP_PORT}': [
                    f'[{p.ip6}]:{DEFAULT_BACKEND_PORT}' for p in ports
                ]
                for i, ports in enumerate(backend_lists)
            }
            self.load_balancer.add_vips(vips)
        else:
            vips = {
                f'{vip_ip + i}:{DEFAULT_VIP_PORT}': [
                    f'{p.ip}:{DEFAULT_BACKEND_PORT}' for p in ports
                ]
                for i, ports in enumerate(backend_lists)
            }
            self.load_balancer.add_vips(vips)


class Cluster(object):
    def __init__(self, central_node, worker_nodes, cluster_cfg, brex_cfg):
        # In clustered mode use the first node for provisioning.
        self.central_node = central_node
        self.worker_nodes = worker_nodes
        self.cluster_cfg = cluster_cfg
        self.brex_cfg = brex_cfg
        self.nbctl = None
        self.sbctl = None
        self.net = cluster_cfg.cluster_net
        self.router = None
        self.load_balancer = None
        self.load_balancer6 = None
        self.join_switch = None
        self.last_selected_worker = 0
        self.n_ns = 0

    def start(self):
        self.central_node.start(self.cluster_cfg)
        nb_conn = self.central_node.get_connection_string(
            self.cluster_cfg, 6641
        )
        inactivity_probe = self.cluster_cfg.db_inactivity_probe // 1000
        self.nbctl = ovn_utils.OvnNbctl(
            self.central_node, nb_conn, inactivity_probe
        )

        sb_conn = self.central_node.get_connection_string(
            self.cluster_cfg, 6642
        )
        self.sbctl = ovn_utils.OvnSbctl(
            self.central_node, sb_conn, inactivity_probe
        )
        for w in self.worker_nodes:
            w.start(self.cluster_cfg)
            w.configure(self.brex_cfg.physical_net)

        self.nbctl.set_global(
            'use_logical_dp_groups', self.cluster_cfg.logical_dp_groups
        )
        self.nbctl.set_global(
            'northd_probe_interval', self.cluster_cfg.northd_probe_interval
        )
        self.nbctl.set_inactivity_probe(self.cluster_cfg.db_inactivity_probe)
        self.sbctl.set_inactivity_probe(self.cluster_cfg.db_inactivity_probe)

    def create_cluster_router(self, rtr_name):
        self.router = self.nbctl.lr_add(rtr_name)
        self.nbctl.lr_set_options(
            self.router,
            {
                'always_learn_from_arp_request': 'false',
            },
        )

    def create_cluster_load_balancer(self, lb_name, global_cfg):
        if global_cfg.run_ipv4:
            self.load_balancer = lb.OvnLoadBalancer(
                lb_name, self.nbctl, self.cluster_cfg.vips
            )
            self.load_balancer.add_vips(self.cluster_cfg.static_vips)

        if global_cfg.run_ipv6:
            self.load_balancer6 = lb.OvnLoadBalancer(
                f'{lb_name}6', self.nbctl, self.cluster_cfg.vips6
            )
            self.load_balancer6.add_vips(self.cluster_cfg.static_vips6)

    def create_cluster_join_switch(self, sw_name):
        self.join_switch = self.nbctl.ls_add(
            sw_name, net_s=self.cluster_cfg.gw_net
        )

        self.join_rp = self.nbctl.lr_port_add(
            self.router,
            'rtr-to-join',
            RandMac(),
            self.cluster_cfg.gw_net.reverse(),
        )
        self.join_ls_rp = self.nbctl.ls_port_add(
            self.join_switch, 'join-to-rtr', self.join_rp
        )

    def provision_ports(self, n_ports, passive=False):
        return [
            self.select_worker_for_port().provision_ports(self, 1, passive)[0]
            for _ in range(n_ports)
        ]

    def unprovision_ports(self, ports):
        for port in ports:
            worker = port.metadata
            worker.unprovision_port(self, port)

    def ping_ports(self, ports):
        ports_per_worker = defaultdict(list)
        for p in ports:
            ports_per_worker[p.metadata].append(p)
        for w, ports in ports_per_worker.items():
            w.ping_ports(self, ports)

    @ovn_stats.timeit
    def provision_vips_to_load_balancers(self, backend_lists):
        n_vips = len(self.load_balancer.vips.keys())
        vip_ip = self.cluster_cfg.vip_subnet.ip.__add__(n_vips + 1)

        vips = {
            f'{vip_ip + i}:{DEFAULT_VIP_PORT}': [
                f'{p.ip}:{DEFAULT_BACKEND_PORT}' for p in ports
            ]
            for i, ports in enumerate(backend_lists)
        }
        self.load_balancer.add_vips(vips)

    def unprovision_vips(self):
        if self.load_balancer:
            self.load_balancer.clear_vips()
            self.load_balancer.add_vips(self.cluster_cfg.static_vips)
        if self.load_balancer6:
            self.load_balancer6.clear_vips()
            self.load_balancer6.add_vips(self.cluster_cfg.static_vips6)

    def select_worker_for_port(self):
        self.last_selected_worker += 1
        self.last_selected_worker %= len(self.worker_nodes)
        return self.worker_nodes[self.last_selected_worker]

    def provision_lb_group(self):
        self.lb_group = lb.OvnLoadBalancerGroup('cluster-lb-group', self.nbctl)
        for w in self.worker_nodes:
            self.nbctl.ls_add_lbg(w.switch, self.lb_group.lbg)
            self.nbctl.lr_add_lbg(w.gw_router, self.lb_group.lbg)

    def provision_lb(self, lb):
        log.info(f'Creating load balancer {lb.name}')
        self.lb_group.add_lb(lb)
