import logging
import ovn_exceptions
import ovn_sandbox
import ovn_stats
import ovn_utils
import time
from collections import namedtuple
from collections import defaultdict
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
        super().__init__(phys_node, container)
        self.container = container
        self.mgmt_net = mgmt_net
        self.mgmt_ip = mgmt_ip


class CentralNode(Node):
    def __init__(
        self, phys_node, db_containers, relay_containers, mgmt_net, mgmt_ip
    ):
        super().__init__(phys_node, db_containers[0], mgmt_net, mgmt_ip)
        self.db_containers = db_containers
        self.relay_containers = relay_containers

    def start(self, cluster_cfg):
        log.info('Configuring central node')
        self.set_raft_election_timeout(cluster_cfg.raft_election_to)
        self.enable_trim_on_compaction()
        self.set_northd_threads(cluster_cfg.northd_threads)
        if cluster_cfg.log_txns_db:
            self.enable_txns_db_logging()

    def set_northd_threads(self, n_threads):
        log.info(f'Configuring northd to use {n_threads} threads')
        for container in self.db_containers:
            self.phys_node.run(
                f'podman exec {container} ovn-appctl -t '
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
                f'podman exec {db_container} ovs-appctl -t '
                f'/run/ovn/ovnnb_db.ctl '
                f'ovsdb-server/memory-trim-on-compaction on'
            )
            self.phys_node.run(
                f'podman exec {db_container} ovs-appctl -t '
                f'/run/ovn/ovnsb_db.ctl '
                f'ovsdb-server/memory-trim-on-compaction on'
            )
        for relay_container in self.relay_containers:
            self.phys_node.run(
                f'podman exec {relay_container} ovs-appctl -t '
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

    def get_connection_string(self, cluster_cfg, port):
        protocol = "ssl" if cluster_cfg.enable_ssl else "tcp"
        ip = self.mgmt_ip
        num_conns = 3 if cluster_cfg.clustered_db else 1
        conns = [f"{protocol}:{ip + idx}:{port}" for idx in range(num_conns)]
        return ",".join(conns)

    def central_containers(self):
        return self.db_containers


class ChassisNode(Node):
    def __init__(
        self,
        phys_node,
        container,
        mgmt_net,
        mgmt_ip,
    ):
        super().__init__(phys_node, container, mgmt_net, mgmt_ip)
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

    def get_connection_string(self, cluster_cfg, port):
        protocol = "ssl" if cluster_cfg.enable_ssl else "tcp"
        offset = 0
        offset += 3 if cluster_cfg.clustered_db else 1
        offset += cluster_cfg.n_relays
        return f"{protocol}:{self.mgmt_ip + offset}:{port}"

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


class Cluster:
    def __init__(self, central_node, worker_nodes, cluster_cfg, brex_cfg):
        # In clustered mode use the first node for provisioning.
        self.central_node = central_node
        self.worker_nodes = worker_nodes
        self.cluster_cfg = cluster_cfg
        self.brex_cfg = brex_cfg
        self.nbctl = None
        self.sbctl = None
        self.last_selected_worker = 0

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

    def select_worker_for_port(self):
        self.last_selected_worker += 1
        self.last_selected_worker %= len(self.worker_nodes)
        return self.worker_nodes[self.last_selected_worker]
