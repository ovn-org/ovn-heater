import logging
import ovn_exceptions
import ovn_sandbox
import ovn_stats
import ovn_utils
import time
import netaddr
from collections import namedtuple
from collections import defaultdict
from datetime import datetime
from typing import List, Optional

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
    def bind_port(self, port, mtu_request: Optional[int] = None):
        log.info(f'Binding lport {port.name} on {self.container}')
        self.vsctl.add_port(
            port,
            'br-int',
            internal=True,
            ifaceid=port.name,
            mtu_request=mtu_request,
        )
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


class Cluster:
    def __init__(self, cluster_cfg, central, brex_cfg, az):
        # In clustered mode use the first node for provisioning.
        self.worker_nodes = []
        self.cluster_cfg = cluster_cfg
        self.brex_cfg = brex_cfg
        self.nbctl: Optional[ovn_utils.OvnNbctl] = None
        self.sbctl: Optional[ovn_utils.OvnSbctl] = None
        self.icnbctl: Optional[ovn_utils.OvnIcNbctl] = None
        self.az = az

        protocol = "ssl" if cluster_cfg.enable_ssl else "tcp"
        db_containers = (
            [
                f'ovn-central-az{self.az+1}-1',
                f'ovn-central-az{self.az+1}-2',
                f'ovn-central-az{self.az+1}-3',
            ]
            if cluster_cfg.clustered_db
            else [f'ovn-central-az{self.az+1}-1']
        )

        mgmt_ip = cluster_cfg.node_net.ip + 2 + self.az * len(db_containers)
        self.central_nodes = [
            CentralNode(central, c, mgmt_ip + i, protocol)
            for i, c in enumerate(db_containers)
        ]

        mgmt_ip = (
            cluster_cfg.node_net.ip
            + 2
            + cluster_cfg.n_az * len(self.central_nodes)
            + self.az * cluster_cfg.n_relays
        )
        self.relay_nodes = [
            RelayNode(
                central,
                f'ovn-relay-az{self.az+1}-{i+1}',
                mgmt_ip + i,
                protocol,
            )
            for i in range(cluster_cfg.n_relays)
        ]

    def add_cluster_worker_nodes(self, workers):
        raise NotImplementedError

    def add_workers(self, worker_nodes):
        self.worker_nodes.extend(worker_nodes)

    def prepare_test(self):
        self.start()

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

    def mesh_ping_ports(self, ports: List[ovn_utils.LSPort]) -> None:
        """Perform full-mesh ping test between ports."""
        all_ips = [port.ip for port in ports]

        for port in ports:
            chassis: Optional[ChassisNode] = port.metadata
            if chassis is None:
                log.error(
                    f"Port {port.name} is missing 'metadata' attribute. "
                    f"Can't perform ping."
                )
                continue

            for dest_ip in all_ips:
                if dest_ip == port.ip:
                    continue
                chassis.ping_port(self, port, dest_ip)

    def select_worker_for_port(self):
        self.last_selected_worker += 1
        self.last_selected_worker %= len(self.worker_nodes)
        return self.worker_nodes[self.last_selected_worker]
