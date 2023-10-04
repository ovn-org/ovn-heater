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
        'log_txns_db',
        'datapath_type',
        'raft_election_to',
        'northd_probe_interval',
        'northd_threads',
        'db_inactivity_probe',
        'node_net',
        'enable_ssl',
        'node_timeout_s',
        'internal_net',
        'external_net',
        'gw_net',
        'ts_net',
        'cluster_net',
        'n_workers',
        'n_relays',
        'n_az',
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
    def __init__(self, phys_node, container, mgmt_ip, protocol):
        super().__init__(phys_node, container)
        self.container = container
        self.mgmt_ip = netaddr.IPAddress(mgmt_ip)
        self.protocol = protocol


class CentralNode(Node):
    def __init__(self, phys_node, container, mgmt_ip, protocol):
        super().__init__(phys_node, container, mgmt_ip, protocol)

    def start(self, cluster_cfg, update_election_timeout=False):
        log.info('Configuring central node')
        if cluster_cfg.clustered_db and update_election_timeout:
            self.set_raft_election_timeout(cluster_cfg.raft_election_to)
        self.enable_trim_on_compaction()
        self.set_northd_threads(cluster_cfg.northd_threads)
        if cluster_cfg.log_txns_db:
            self.enable_txns_db_logging()

    def set_northd_threads(self, n_threads):
        log.info(f'Configuring northd to use {n_threads} threads')
        self.phys_node.run(
            f'podman exec {self.container} ovn-appctl -t '
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
        self.phys_node.run(
            f'podman exec {self.container} ovs-appctl -t '
            f'/run/ovn/ovnnb_db.ctl '
            f'ovsdb-server/memory-trim-on-compaction on'
        )
        self.phys_node.run(
            f'podman exec {self.container} ovs-appctl -t '
            f'/run/ovn/ovnsb_db.ctl '
            f'ovsdb-server/memory-trim-on-compaction on'
        )

    def enable_txns_db_logging(self):
        log.info('Enable DB txn logging')
        self.run(
            cmd='ovs-appctl -t /run/ovn/ovnnb_db.ctl '
            'ovsdb-server/tlog-set OVN_Northbound:Logical_Switch_Port on'
        )
        self.run(
            cmd='ovs-appctl -t /run/ovn/ovnnb_db.ctl '
            'vlog/disable-rate-limit transaction'
        )
        self.run(
            cmd='ovs-appctl -t /run/ovn/ovnsb_db.ctl '
            'ovsdb-server/tlog-set OVN_Southbound:Port_Binding on'
        )
        self.run(
            cmd='ovs-appctl -t /run/ovn/ovnsb_db.ctl '
            'vlog/disable-rate-limit transaction'
        )

    def get_connection_string(self, port):
        return f'{self.protocol}:{self.mgmt_ip}:{port}'


class RelayNode(Node):
    def __init__(self, phys_node, container, mgmt_ip, protocol):
        super().__init__(phys_node, container, mgmt_ip, protocol)

    def start(self):
        log.info(f'Configuring relay node {self.container}')
        self.enable_trim_on_compaction()

    def get_connection_string(self, port):
        return f'{self.protocol}:{self.mgmt_ip}:{port}'

    def enable_trim_on_compaction(self):
        log.info('Setting DB trim-on-compaction')
        self.phys_node.run(
            f'podman exec {self.container} ovs-appctl -t '
            f'/run/ovn/ovnsb_db.ctl '
            f'ovsdb-server/memory-trim-on-compaction on'
        )


class ChassisNode(Node):
    def __init__(
        self,
        phys_node,
        container,
        mgmt_ip,
        protocol,
    ):
        super().__init__(phys_node, container, mgmt_ip, protocol)
        self.switch = None
        self.gw_router = None
        self.ext_switch = None
        self.lports = []
        self.next_lport_index = 0
        self.vsctl = None

    def start(self, cluster_cfg):
        self.vsctl = ovn_utils.OvsVsctl(
            self,
            self.get_connection_string(6640),
            cluster_cfg.db_inactivity_probe // 1000,
        )

    @ovn_stats.timeit
    def connect(self, remote):
        log.info(
            f'Connecting worker {self.container}: ' f'ovn-remote = {remote}'
        )
        self.vsctl.set_global_external_id('ovn-remote', f'{remote}')

    def configure_localnet(self, physical_net):
        log.info(f'Creating localnet on {self.container}')
        self.vsctl.set_global_external_id(
            'ovn-bridge-mappings', f'{physical_net}:br-ex'
        )

    @ovn_stats.timeit
    def wait(self, sbctl, timeout_s):
        for _ in range(timeout_s * 10):
            if sbctl.chassis_bound(self.container):
                return
            time.sleep(0.1)
        raise ovn_exceptions.OvnChassisTimeoutException()

    @ovn_stats.timeit
    def unprovision_port(self, cluster, port):
        cluster.nbctl.ls_port_del(port)
        self.unbind_port(port)
        self.lports.remove(port)

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

    def ping_ports(self, cluster, ports):
        for port in ports:
            if port.ip:
                self.ping_port(cluster, port, dest=port.ext_gw)
            if port.ip6:
                self.ping_port(cluster, port, dest=port.ext_gw6)

    def get_connection_string(self, port):
        return f"{self.protocol}:{self.mgmt_ip}:{port}"

    def configure(self, physical_net):
        raise NotImplementedError

    @ovn_stats.timeit
    def provision(self, cluster):
        raise NotImplementedError

    @ovn_stats.timeit
    def provision_port(self, cluster, passive=False):
        raise NotImplementedError

    @ovn_stats.timeit
    def ping_external(self, cluster, port):
        raise NotImplementedError


ACL_DEFAULT_DENY_PRIO = 1
ACL_DEFAULT_ALLOW_ARP_PRIO = 2
ACL_NETPOL_ALLOW_PRIO = 3
DEFAULT_NS_VIP_SUBNET = netaddr.IPNetwork('30.0.0.0/16')
DEFAULT_NS_VIP_SUBNET6 = netaddr.IPNetwork('30::/32')
DEFAULT_VIP_PORT = 80
DEFAULT_BACKEND_PORT = 8080


class Namespace:
    def __init__(self, clusters, name, global_cfg):
        self.clusters = clusters
        self.nbctl = [cluster.nbctl for cluster in clusters]
        self.ports = [[] for _ in range(len(clusters))]
        self.enforcing = False
        self.pg_def_deny_igr = [
            nbctl.port_group_create(f'pg_deny_igr_{name}')
            for nbctl in self.nbctl
        ]
        self.pg_def_deny_egr = [
            nbctl.port_group_create(f'pg_deny_egr_{name}')
            for nbctl in self.nbctl
        ]
        self.pg = [
            nbctl.port_group_create(f'pg_{name}') for nbctl in self.nbctl
        ]
        self.addr_set4 = [
            (
                nbctl.address_set_create(f'as_{name}')
                if global_cfg.run_ipv4
                else None
            )
            for nbctl in self.nbctl
        ]
        self.addr_set6 = [
            (
                nbctl.address_set_create(f'as6_{name}')
                if global_cfg.run_ipv6
                else None
            )
            for nbctl in self.nbctl
        ]
        self.sub_as = [[] for _ in range(len(clusters))]
        self.sub_pg = [[] for _ in range(len(clusters))]
        self.load_balancer = None
        for cluster in self.clusters:
            cluster.n_ns += 1
        self.name = name

    @ovn_stats.timeit
    def add_ports(self, ports, az=0):
        self.ports[az].extend(ports)
        # Always add port IPs to the address set but not to the PGs.
        # Simulate what OpenShift does, which is: create the port groups
        # when the first network policy is applied.
        if self.addr_set4:
            for i, nbctl in enumerate(self.nbctl):
                nbctl.address_set_add_addrs(
                    self.addr_set4[i], [str(p.ip) for p in ports]
                )
        if self.addr_set6:
            for i, nbctl in enumerate(self.nbctl):
                nbctl.address_set_add_addrs(
                    self.addr_set6[i], [str(p.ip6) for p in ports]
                )
        if self.enforcing:
            self.nbctl[az].port_group_add_ports(
                self.pg_def_deny_igr[az], ports
            )
            self.nbctl[az].port_group_add_ports(
                self.pg_def_deny_egr[az], ports
            )
            self.nbctl[az].port_group_add_ports(self.pg[az], ports)

    def unprovision(self):
        # ACLs are garbage collected by OVSDB as soon as all the records
        # referencing them are removed.
        for i, cluster in enumerate(self.clusters):
            cluster.unprovision_ports(self.ports[i])
        for i, nbctl in enumerate(self.nbctl):
            nbctl.port_group_del(self.pg_def_deny_igr[i])
            nbctl.port_group_del(self.pg_def_deny_egr[i])
            nbctl.port_group_del(self.pg[i])
            if self.addr_set4:
                nbctl.address_set_del(self.addr_set4[i])
            if self.addr_set6:
                nbctl.address_set_del(self.addr_set6[i])
            nbctl.port_group_del(self.sub_pg[i])
            nbctl.address_set_del(self.sub_as[i])

    def unprovision_ports(self, ports, az=0):
        '''Unprovision a subset of ports in the namespace without having to
        unprovision the entire namespace or any of its network policies.'''

        for port in ports:
            self.ports[az].remove(port)

        self.clusters[az].unprovision_ports(ports)

    def enforce(self):
        if self.enforcing:
            return
        self.enforcing = True
        for i, nbctl in enumerate(self.nbctl):
            nbctl.port_group_add_ports(self.pg_def_deny_igr[i], self.ports[i])
            nbctl.port_group_add_ports(self.pg_def_deny_egr[i], self.ports[i])
            nbctl.port_group_add_ports(self.pg[i], self.ports[i])

    def create_sub_ns(self, ports, global_cfg, az=0):
        n_sub_pgs = len(self.sub_pg[az])
        suffix = f'{self.name}_{n_sub_pgs}'
        pg = self.nbctl[az].port_group_create(f'sub_pg_{suffix}')
        self.nbctl[az].port_group_add_ports(pg, ports)
        self.sub_pg[az].append(pg)
        for i, nbctl in enumerate(self.nbctl):
            if global_cfg.run_ipv4:
                addr_set = nbctl.address_set_create(f'sub_as_{suffix}')
                nbctl.address_set_add_addrs(
                    addr_set, [str(p.ip) for p in ports]
                )
                self.sub_as[i].append(addr_set)
            if global_cfg.run_ipv6:
                addr_set = nbctl.address_set_create(f'sub_as_{suffix}6')
                nbctl.address_set_add_addrs(
                    addr_set, [str(p.ip6) for p in ports]
                )
                self.sub_as[i].append(addr_set)
        return n_sub_pgs

    @ovn_stats.timeit
    def default_deny(self, family, az=0):
        self.enforce()

        addr_set = f'self.addr_set{family}.name'
        self.nbctl[az].acl_add(
            self.pg_def_deny_igr[az].name,
            'to-lport',
            ACL_DEFAULT_DENY_PRIO,
            'port-group',
            f'ip4.src == \\${addr_set} && '
            f'outport == @{self.pg_def_deny_igr[az].name}',
            'drop',
        )
        self.nbctl[az].acl_add(
            self.pg_def_deny_egr[az].name,
            'to-lport',
            ACL_DEFAULT_DENY_PRIO,
            'port-group',
            f'ip4.dst == \\${addr_set} && '
            f'inport == @{self.pg_def_deny_egr[az].name}',
            'drop',
        )
        self.nbctl[az].acl_add(
            self.pg_def_deny_igr[az].name,
            'to-lport',
            ACL_DEFAULT_ALLOW_ARP_PRIO,
            'port-group',
            f'outport == @{self.pg_def_deny_igr[az].name} && arp',
            'allow',
        )
        self.nbctl[az].acl_add(
            self.pg_def_deny_egr[az].name,
            'to-lport',
            ACL_DEFAULT_ALLOW_ARP_PRIO,
            'port-group',
            f'inport == @{self.pg_def_deny_egr[az].name} && arp',
            'allow',
        )

    @ovn_stats.timeit
    def allow_within_namespace(self, family, az=0):
        self.enforce()

        addr_set = f'self.addr_set{family}.name'
        self.nbctl[az].acl_add(
            self.pg[az].name,
            'to-lport',
            ACL_NETPOL_ALLOW_PRIO,
            'port-group',
            f'ip4.src == \\${addr_set} && ' f'outport == @{self.pg[az].name}',
            'allow-related',
        )
        self.nbctl[az].acl_add(
            self.pg[az].name,
            'to-lport',
            ACL_NETPOL_ALLOW_PRIO,
            'port-group',
            f'ip4.dst == \\${addr_set} && ' f'inport == @{self.pg[az].name}',
            'allow-related',
        )

    @ovn_stats.timeit
    def allow_cross_namespace(self, ns, family):
        self.enforce()

        for az, nbctl in enumerate(self.nbctl):
            if len(self.ports[az]) == 0:
                continue
            addr_set = f'self.addr_set{family}.name'
            nbctl[az].acl_add(
                self.pg[az].name,
                'to-lport',
                ACL_NETPOL_ALLOW_PRIO,
                'port-group',
                f'ip4.src == \\${addr_set} && '
                f'outport == @{ns.pg[az].name}',
                'allow-related',
            )
            ns_addr_set = f'ns.addr_set{family}.name'
            nbctl[az].acl_add(
                self.pg[az].name,
                'to-lport',
                ACL_NETPOL_ALLOW_PRIO,
                'port-group',
                f'ip4.dst == \\${ns_addr_set} && '
                f'inport == @{self.pg[az].name}',
                'allow-related',
            )

    @ovn_stats.timeit
    def allow_sub_namespace(self, src, dst, family, az=0):
        self.nbctl[az].acl_add(
            self.pg[az].name,
            'to-lport',
            ACL_NETPOL_ALLOW_PRIO,
            'port-group',
            f'ip{family}.src == \\${self.sub_as[az][src].name} && '
            f'outport == @{self.sub_pg[az][dst].name}',
            'allow-related',
        )
        self.nbctl[az].acl_add(
            self.pg[az].name,
            'to-lport',
            ACL_NETPOL_ALLOW_PRIO,
            'port-group',
            f'ip{family}.dst == \\${self.sub_as[az][dst].name} && '
            f'inport == @{self.sub_pg[az][src].name}',
            'allow-related',
        )

    @ovn_stats.timeit
    def allow_from_external(
        self, external_ips, include_ext_gw=False, family=4, az=0
    ):
        self.enforce()
        # If requested, include the ext-gw of the first port in the namespace
        # so we can check that this rule is enforced.
        if include_ext_gw:
            assert len(self.ports) > 0
            if family == 4 and self.ports[az][0].ext_gw:
                external_ips.append(self.ports[az][0].ext_gw)
            elif family == 6 and self.ports[az][0].ext_gw6:
                external_ips.append(self.ports[az][0].ext_gw6)
        ips = [str(ip) for ip in external_ips]
        self.nbctl[az].acl_add(
            self.pg[az].name,
            'to-lport',
            ACL_NETPOL_ALLOW_PRIO,
            'port-group',
            f'ip.{family} == {{{",".join(ips)}}} && '
            f'outport == @{self.pg[az].name}',
            'allow-related',
        )

    @ovn_stats.timeit
    def check_enforcing_internal(self, az=0):
        # "Random" check that first pod can reach last pod in the namespace.
        if len(self.ports[az]) > 1:
            src = self.ports[az][0]
            dst = self.ports[az][-1]
            worker = src.metadata
            if src.ip:
                worker.ping_port(self.clusters[az], src, dst.ip)
            if src.ip6:
                worker.ping_port(self.clusters[az], src, dst.ip6)

    @ovn_stats.timeit
    def check_enforcing_external(self, az=0):
        if len(self.ports[az]) > 0:
            dst = self.ports[az][0]
            worker = dst.metadata
            worker.ping_external(self.clusters[az], dst)

    @ovn_stats.timeit
    def check_enforcing_cross_ns(self, ns, az=0):
        if len(self.ports[az]) > 0 and len(ns.ports[az]) > 0:
            dst = ns.ports[az][0]
            src = self.ports[az][0]
            worker = src.metadata
            if src.ip and dst.ip:
                worker.ping_port(self.clusters[az], src, dst.ip)
            if src.ip6 and dst.ip6:
                worker.ping_port(self.clusters[az], src, dst.ip6)

    def create_load_balancer(self, az=0):
        self.load_balancer = lb.OvnLoadBalancer(
            f'lb_{self.name}', self.nbctl[az]
        )

    @ovn_stats.timeit
    def provision_vips_to_load_balancers(self, backend_lists, version, az=0):
        vip_ns_subnet = DEFAULT_NS_VIP_SUBNET
        if version == 6:
            vip_ns_subnet = DEFAULT_NS_VIP_SUBNET6
        vip_net = vip_ns_subnet.next(self.clusters[az].n_ns)
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


class Cluster:
    def __init__(self, central_nodes, relay_nodes, cluster_cfg, brex_cfg, az):
        # In clustered mode use the first node for provisioning.
        self.central_nodes = central_nodes
        self.relay_nodes = relay_nodes
        self.worker_nodes = []
        self.cluster_cfg = cluster_cfg
        self.brex_cfg = brex_cfg
        self.nbctl = None
        self.sbctl = None
        self.icnbctl = None
        self.net = cluster_cfg.cluster_net
        self.gw_net = ovn_utils.DualStackSubnet.next(
            cluster_cfg.gw_net,
            az * (cluster_cfg.n_workers // cluster_cfg.n_az),
        )
        self.az = az
        self.router = None
        self.load_balancer = None
        self.load_balancer6 = None
        self.join_switch = None
        self.last_selected_worker = 0
        self.n_ns = 0
        self.ts_switch = None

    def add_workers(self, worker_nodes):
        self.worker_nodes.extend(worker_nodes)

    def start(self):
        for c in self.central_nodes:
            c.start(
                self.cluster_cfg,
                update_election_timeout=(c is self.central_nodes[0]),
            )
        nb_conn = self.get_nb_connection_string()
        inactivity_probe = self.cluster_cfg.db_inactivity_probe // 1000
        self.nbctl = ovn_utils.OvnNbctl(
            self.central_nodes[0], nb_conn, inactivity_probe
        )

        sb_conn = self.get_sb_connection_string()
        self.sbctl = ovn_utils.OvnSbctl(
            self.central_nodes[0], sb_conn, inactivity_probe
        )

        # ovn-ic configuration
        self.icnbctl = ovn_utils.OvnIcNbctl(
            None,
            f'tcp:{self.cluster_cfg.node_net.ip + 2}:6645',
            inactivity_probe,
        )
        self.nbctl.set_global('ic-route-learn', 'true')
        self.nbctl.set_global('ic-route-adv', 'true')

        for r in self.relay_nodes:
            r.start()

        for w in self.worker_nodes:
            w.start(self.cluster_cfg)
            w.configure(self.brex_cfg.physical_net)

        self.nbctl.set_global(
            'use_logical_dp_groups', self.cluster_cfg.logical_dp_groups
        )
        self.nbctl.set_global(
            'northd_probe_interval', self.cluster_cfg.northd_probe_interval
        )
        self.nbctl.set_global_name(f'az{self.az}')
        self.nbctl.set_inactivity_probe(self.cluster_cfg.db_inactivity_probe)
        self.sbctl.set_inactivity_probe(self.cluster_cfg.db_inactivity_probe)

    def get_nb_connection_string(self):
        return ','.join(
            [db.get_connection_string(6641) for db in self.central_nodes]
        )

    def get_sb_connection_string(self):
        return ','.join(
            [db.get_connection_string(6642) for db in self.central_nodes]
        )

    def get_relay_connection_string(self):
        if len(self.relay_nodes) > 0:
            return ','.join(
                [db.get_connection_string(6642) for db in self.relay_nodes]
            )
        return self.get_sb_connection_string()

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
        self.join_switch = self.nbctl.ls_add(sw_name, net_s=self.gw_net)

        self.join_rp = self.nbctl.lr_port_add(
            self.router,
            f'rtr-to-{sw_name}',
            RandMac(),
            self.gw_net.reverse(),
        )
        self.join_ls_rp = self.nbctl.ls_port_add(
            self.join_switch, f'{sw_name}-to-rtr', self.join_rp
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

    def provision_lb_group(self, name='cluster-lb-group'):
        self.lb_group = lb.OvnLoadBalancerGroup(name, self.nbctl)
        for w in self.worker_nodes:
            self.nbctl.ls_add_lbg(w.switch, self.lb_group.lbg)
            self.nbctl.lr_add_lbg(w.gw_router, self.lb_group.lbg)

    def provision_lb(self, lb):
        log.info(f'Creating load balancer {lb.name}')
        self.lb_group.add_lb(lb)
