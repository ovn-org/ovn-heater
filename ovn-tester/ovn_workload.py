import ovn_context
import ovn_exceptions
import ovn_sandbox
import ovn_stats
import ovn_utils
import ovn_load_balancer as lb
import time
import netaddr
import random
import string
import copy
from collections import namedtuple
from collections import defaultdict
from randmac import RandMac
from datetime import datetime


ClusterConfig = namedtuple('ClusterConfig',
                           ['cluster_cmd_path',
                            'monitor_all',
                            'logical_dp_groups',
                            'clustered_db',
                            'raft_election_to',
                            'db_inactivity_probe',
                            'node_net',
                            'node_remote',
                            'node_timeout_s',
                            'internal_net',
                            'external_net',
                            'gw_net',
                            'cluster_net',
                            'n_workers',
                            'vips',
                            'vip_subnet',
                            'static_vips'])


BrExConfig = namedtuple('BrExConfig', ['physical_net'])


class Node(ovn_sandbox.Sandbox):
    def __init__(self, phys_node, container, mgmt_net, mgmt_ip):
        super(Node, self).__init__(phys_node, container)
        self.container = container
        self.mgmt_net = mgmt_net
        self.mgmt_ip = mgmt_ip

    def build_cmd(self, cluster_cfg, cmd, *args):
        monitor_all = 'yes' if cluster_cfg.monitor_all else 'no'
        clustered_db = 'yes' if cluster_cfg.clustered_db else 'no'
        cmd = \
            f'cd {cluster_cfg.cluster_cmd_path} && ' \
            f'OVN_MONITOR_ALL={monitor_all} OVN_DB_CLUSTER={clustered_db} ' \
            f'CREATE_FAKE_VMS=no CHASSIS_COUNT=0 GW_COUNT=0 '\
            f'IP_HOST={self.mgmt_net.ip} ' \
            f'IP_CIDR={self.mgmt_net.prefixlen} ' \
            f'IP_START={self.mgmt_ip} ' \
            f'./ovn_cluster.sh {cmd}'
        return cmd + ' ' + ' '.join(args)


class CentralNode(Node):
    def __init__(self, phys_node, db_containers, mgmt_net, mgmt_ip):
        super(CentralNode, self).__init__(phys_node, db_containers[0],
                                          mgmt_net, mgmt_ip)
        self.db_containers = db_containers

    def start(self, cluster_cfg):
        print('***** starting central node *****')
        self.phys_node.run(self.build_cmd(cluster_cfg, 'start'))
        time.sleep(5)
        self.set_raft_election_timeout(cluster_cfg.raft_election_to)
        self.enable_trim_on_compaction()

    def set_raft_election_timeout(self, timeout_s):
        for timeout in range(1000, (timeout_s + 1) * 1000, 1000):
            print(f'***** set RAFT election timeout to {timeout}ms *****')
            self.run(cmd=f'ovs-appctl -t '
                     f'/run/ovn/ovnnb_db.ctl cluster/change-election-timer '
                     f'OVN_Northbound {timeout}')
            self.run(cmd=f'ovs-appctl -t '
                     f'/run/ovn/ovnsb_db.ctl cluster/change-election-timer '
                     f'OVN_Southbound {timeout}')

    def enable_trim_on_compaction(self):
        print(f'***** set DB trim-on-compaction *****')
        for db_container in self.db_containers:
            self.phys_node.run(f'docker exec {db_container} ovs-appctl -t '
                               f'/run/ovn/ovnnb_db.ctl '
                               f'ovsdb-server/memory-trim-on-compaction on')
            self.phys_node.run(f'docker exec {db_container} ovs-appctl -t '
                               f'/run/ovn/ovnsb_db.ctl '
                               f'ovsdb-server/memory-trim-on-compaction on')


class WorkerNode(Node):
    def __init__(self, phys_node, container, mgmt_net, mgmt_ip,
                 int_net, ext_net, gw_net, unique_id):
        super(WorkerNode, self).__init__(phys_node, container,
                                         mgmt_net, mgmt_ip)
        self.int_net = int_net
        self.ext_net = ext_net
        self.gw_net = gw_net
        self.id = unique_id
        self.switch = None
        self.gw_router = None
        self.ext_switch = None
        self.lports = []
        self.next_lport_index = 0

    def start(self, cluster_cfg):
        print(f'***** starting worker {self.container} *****')
        self.phys_node.run(self.build_cmd(cluster_cfg, 'add-chassis',
                                          self.container, 'tcp:0.0.0.1:6642'))

    @ovn_stats.timeit
    def connect(self, cluster_cfg):
        print(f'***** connecting worker {self.container} *****')
        self.phys_node.run(self.build_cmd(cluster_cfg,
                                          'set-chassis-ovn-remote',
                                          self.container,
                                          cluster_cfg.node_remote))

    def configure_localnet(self, physical_net):
        print(f'***** creating localnet on {self.container} *****')
        self.run(cmd=f'ovs-vsctl -- set open_vswitch . '
                 f'external-ids:ovn-bridge-mappings={physical_net}:br-ex')

    def configure_external_host(self):
        print(f'***** add external host on {self.container} *****')
        gw_ip = netaddr.IPAddress(self.ext_net.last - 1)
        host_ip = netaddr.IPAddress(self.ext_net.last - 2)

        self.run(cmd='ip link add veth0 type veth peer name veth1')
        self.run(cmd='ip link add veth0 type veth peer name veth1')
        self.run(cmd='ip netns add ext-ns')
        self.run(cmd='ip link set netns ext-ns dev veth0')
        self.run(cmd='ip netns exec ext-ns ip link set dev veth0 up')
        self.run(cmd=f'ip netns exec ext-ns '
                 f'ip addr add {host_ip}/{self.ext_net.prefixlen} '
                 f'dev veth0')
        self.run(cmd=f'ip netns exec ext-ns ip route add default via {gw_ip}')
        self.run(cmd='ip link set dev veth1 up')
        self.run(cmd='ovs-vsctl add-port br-ex veth1')

    def configure(self, physical_net):
        self.configure_localnet(physical_net)
        self.configure_external_host()

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
        self.switch = cluster.nbctl.ls_add(f'lswitch-{self.container}',
                                           cidr=self.int_net)
        lrp_name = f'rtr-to-node-{self.container}'
        ls_rp_name = f'node-to-rtr-{self.container}'
        lrp_ip = netaddr.IPAddress(self.int_net.last - 1)
        self.rp = cluster.nbctl.lr_port_add(
            cluster.router, lrp_name, RandMac(), lrp_ip,
            self.int_net.prefixlen
        )
        self.ls_rp = cluster.nbctl.ls_port_add(
            self.switch, ls_rp_name, self.rp
        )

        # Create a gw router and connect it to the cluster join switch.
        self.gw_router = cluster.nbctl.lr_add(f'gwrouter-{self.container}')
        cluster.nbctl.run(f'set Logical_Router {self.gw_router.name} '
                          f'options:chassis={self.container}')
        join_grp_name = f'gw-to-join-{self.container}'
        join_ls_grp_name = f'join-to-gw-{self.container}'
        gr_gw = netaddr.IPAddress(self.gw_net.last - 2 - self.id)
        self.gw_rp = cluster.nbctl.lr_port_add(
            self.gw_router, join_grp_name, RandMac(), gr_gw,
            self.gw_net.prefixlen
        )
        self.join_gw_rp = cluster.nbctl.ls_port_add(
            cluster.join_switch, join_ls_grp_name, self.gw_rp
        )

        # Create an external switch connecting the gateway router to the
        # physnet.
        self.ext_switch = cluster.nbctl.ls_add(f'ext-{self.container}',
                                               cidr=self.ext_net)
        ext_lrp_name = f'gw-to-ext-{self.container}'
        ext_ls_rp_name = f'ext-to-gw-{self.container}'
        lrp_ip = netaddr.IPAddress(self.ext_net.last - 1)
        self.ext_rp = cluster.nbctl.lr_port_add(
            self.gw_router, ext_lrp_name, RandMac(), lrp_ip,
            self.ext_net.prefixlen
        )
        self.ext_gw_rp = cluster.nbctl.ls_port_add(
            self.ext_switch, ext_ls_rp_name, self.ext_rp
        )

        # Configure physnet.
        self.physnet_port = cluster.nbctl.ls_port_add(
            self.ext_switch, f'provnet-{self.container}', ip="unknown"
        )
        cluster.nbctl.ls_port_set_set_type(self.physnet_port, 'localnet')
        cluster.nbctl.ls_port_set_set_options(
            self.physnet_port,
            f'network_name={cluster.brex_cfg.physical_net}'
        )

        # Route for traffic entering the cluster.
        rp_gw = netaddr.IPAddress(self.gw_net.last - 1)
        cluster.nbctl.route_add(self.gw_router, cluster.net, str(rp_gw))

        # Default route to get out of cluster via physnet.
        gr_def_gw = netaddr.IPAddress(self.ext_net.last - 2)
        cluster.nbctl.route_add(self.gw_router, gw=str(gr_def_gw))

        # Force return traffic to return on the same node.
        cluster.nbctl.run(f'set Logical_Router {self.gw_router} '
                          f'options:lb_force_snat_ip={gr_gw}')

        # Route for traffic that needs to exit the cluster
        # (via gw router).
        cluster.nbctl.route_add(cluster.router, str(self.int_net),
                                str(gr_gw), policy="src-ip")

        # SNAT traffic leaving the cluster.
        cluster.nbctl.nat_add(self.gw_router, external_ip=str(gr_gw),
                              logical_ip=cluster.net)

    @ovn_stats.timeit
    def provision_port(self, cluster, passive=False):
        name = f'lp-{self.id}-{self.next_lport_index}'
        ip = netaddr.IPAddress(self.int_net.first + self.next_lport_index + 1)
        plen = self.int_net.prefixlen
        gw = netaddr.IPAddress(self.int_net.last - 1)
        ext_gw = netaddr.IPAddress(self.ext_net.last - 2)

        print(f'***** creating lport {name} *****')
        lport = cluster.nbctl.ls_port_add(self.switch, name,
                                          mac=str(RandMac()), ip=ip, plen=plen,
                                          gw=gw, ext_gw=ext_gw, metadata=self,
                                          passive=passive, security=True)
        self.lports.append(lport)
        self.next_lport_index += 1
        return lport

    @ovn_stats.timeit
    def unprovision_port(self, cluster, port):
        cluster.nbctl.ls_port_del(port)
        self.unbind_port(port)
        self.lports.remove(port)

    @ovn_stats.timeit
    def provision_load_balancers(self, cluster, ports):
        # Add one port IP as a backend to the cluster load balancer.
        port_ips = (str(port.ip) for port in ports if port.ip is not None)
        cluster_vips = cluster.cluster_cfg.vips.keys()
        cluster.load_balancer.add_backends_to_vip(port_ips,
                                                  cluster_vips)
        cluster.load_balancer.add_to_switch(self.switch.name)
        cluster.load_balancer.add_to_router(self.gw_router.name)

        # GW Load balancer has no VIPs/backends configured on it, since
        # this load balancer is used for hostnetwork services. We're not
        # using those right now so the load blaancer is empty.
        self.gw_load_balancer = lb.OvnLoadBalancer(
            f'lb-{self.gw_router.name}', cluster.nbctl)
        self.gw_load_balancer.add_to_router(self.gw_router.name)

    @ovn_stats.timeit
    def bind_port(self, port):
        vsctl = ovn_utils.OvsVsctl(self)
        vsctl.add_port(port, 'br-int', internal=True, ifaceid=port.name)
        # Skip creating a netns for "passive" ports, we won't be sending
        # traffic on those.
        if not port.passive:
            vsctl.bind_vm_port(port)

    @ovn_stats.timeit
    def unbind_port(self, port):
        vsctl = ovn_utils.OvsVsctl(self)
        if not port.passive:
            vsctl.unbind_vm_port(port)
        vsctl.del_port(port)

    def provision_ports(self, cluster, n_ports, passive=False):
        ports = [self.provision_port(cluster, passive) for i in range(n_ports)]
        for port in ports:
            self.bind_port(port)
        return ports

    def run_ping(self, cluster, src, dest):
        print(f'***** pinging from {src} to {dest} *****')
        cmd = f'ip netns exec {src} ping -q -c 1 -W 0.1 {dest}'
        start_time = datetime.now()
        while True:
            try:
                self.run(cmd=cmd, raise_on_error=True)
                break
            except ovn_exceptions.SSHError:
                pass

            duration = (datetime.now() - start_time).seconds
            if (duration > cluster.cluster_cfg.node_timeout_s):
                print(f'***** Error: Timeout waiting for {src} '
                      f'to be able to ping {dest} *****')
                raise ovn_exceptions.OvnPingTimeoutException()

    @ovn_stats.timeit
    def ping_port(self, cluster, port, dest=None):
        if not dest:
            dest = port.ext_gw
        self.run_ping(cluster, port.name, dest)

    @ovn_stats.timeit
    def ping_external(self, cluster, port):
        self.run_ping(cluster, 'ext-ns', port.ip)

    def ping_ports(self, cluster, ports):
        for port in ports:
            self.ping_port(cluster, port)


ACL_DEFAULT_DENY_PRIO = 1
ACL_DEFAULT_ALLOW_ARP_PRIO = 2
ACL_NETPOL_ALLOW_PRIO = 3


class Namespace(object):
    def __init__(self, cluster, name):
        self.cluster = cluster
        self.nbctl = cluster.nbctl
        self.ports = []
        self.enforcing = False
        self.pg_def_deny_igr = \
            self.nbctl.port_group_create(f'pg_deny_igr_{name}')
        self.pg_def_deny_egr = \
            self.nbctl.port_group_create(f'pg_deny_egr_{name}')
        self.pg = self.nbctl.port_group_create(f'pg_{name}')
        self.addr_set = self.nbctl.address_set_create(f'as_{name}')

    @ovn_stats.timeit
    def add_ports(self, ports):
        self.ports.extend(ports)
        # Always add port IPs to the address set but not to the PGs.
        # Simulate what OpenShift does, which is: create the port groups
        # when the first network policy is applied.
        self.nbctl.address_set_add_addrs(self.addr_set,
                                         [str(p.ip) for p in ports])
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
        self.nbctl.address_set_del(self.addr_set)

    def enforce(self):
        if self.enforcing:
            return
        self.enforcing = True
        self.nbctl.port_group_add_ports(self.pg_def_deny_igr, self.ports)
        self.nbctl.port_group_add_ports(self.pg_def_deny_egr, self.ports)
        self.nbctl.port_group_add_ports(self.pg, self.ports)

    @ovn_stats.timeit
    def default_deny(self):
        self.enforce()
        self.nbctl.acl_add(
            self.pg_def_deny_igr.name,
            'to-lport', ACL_DEFAULT_DENY_PRIO, 'port-group',
            f'ip4.src == \\${self.addr_set.name} && '
            f'outport == @{self.pg_def_deny_igr.name}',
            'drop')
        self.nbctl.acl_add(
            self.pg_def_deny_egr.name,
            'to-lport', ACL_DEFAULT_DENY_PRIO, 'port-group',
            f'ip4.dst == \\${self.addr_set.name} && '
            f'inport == @{self.pg_def_deny_egr.name}',
            'drop')
        self.nbctl.acl_add(
            self.pg_def_deny_igr.name,
            'to-lport', ACL_DEFAULT_ALLOW_ARP_PRIO, 'port-group',
            f'outport == @{self.pg_def_deny_igr.name} && arp',
            'allow')
        self.nbctl.acl_add(
            self.pg_def_deny_egr.name,
            'to-lport', ACL_DEFAULT_ALLOW_ARP_PRIO, 'port-group',
            f'inport == @{self.pg_def_deny_egr.name} && arp',
            'allow')

    @ovn_stats.timeit
    def allow_within_namespace(self):
        self.enforce()
        self.nbctl.acl_add(
            self.pg.name, 'to-lport', ACL_NETPOL_ALLOW_PRIO, 'port-group',
            f'ip4.src == \\${self.addr_set.name} && '
            f'outport == @{self.pg.name}',
            'allow-related'
        )
        self.nbctl.acl_add(
            self.pg.name, 'to-lport', ACL_NETPOL_ALLOW_PRIO, 'port-group',
            f'ip4.dst == \\${self.addr_set.name} && '
            f'inport == @{self.pg.name}',
            'allow-related'
        )

    @ovn_stats.timeit
    def allow_cross_namespace(self, ns):
        self.enforce()
        self.nbctl.acl_add(
            self.pg.name, 'to-lport', ACL_NETPOL_ALLOW_PRIO, 'port-group',
            f'ip4.src == \\${self.addr_set.name} && '
            f'outport == @{ns.pg.name}',
            'allow-related'
        )
        self.nbctl.acl_add(
            self.pg.name, 'to-lport', ACL_NETPOL_ALLOW_PRIO, 'port-group',
            f'ip4.dst == \\${ns.addr_set.name} && '
            f'inport == @{self.pg.name}',
            'allow-related'
        )

    @ovn_stats.timeit
    def allow_from_external(self, external_ips, include_ext_gw=False):
        self.enforce()
        # If requested, include the ext-gw of the first port in the namespace
        # so we can check that this rule is enforced.
        if include_ext_gw:
            assert(len(self.ports) > 0)
            external_ips.append(self.ports[0].ext_gw)
        ips = [str(ip) for ip in external_ips]
        self.nbctl.acl_add(
            self.pg.name, 'to-lport', ACL_NETPOL_ALLOW_PRIO, 'port-group',
            f'ip4.src == {{{",".join(ips)}}} && outport == @{self.pg.name}',
            'allow-related'
        )

    @ovn_stats.timeit
    def check_enforcing_internal(self):
        # "Random" check that first pod can reach last pod in the namespace.
        if len(self.ports) > 1:
            src = self.ports[0]
            dst = self.ports[-1]
            worker = src.metadata
            worker.ping_port(self.cluster, src, dst.ip)

    @ovn_stats.timeit
    def check_enforcing_external(self):
        if len(self.ports) > 0:
            dst = self.ports[0]
            worker = dst.metadata
            worker.ping_external(self.cluster, dst)


class Cluster(object):
    def __init__(self, central_node, worker_nodes, cluster_cfg, brex_cfg):
        # In clustered mode use the first node for provisioning.
        self.central_node = central_node
        self.worker_nodes = worker_nodes
        self.cluster_cfg = cluster_cfg
        self.brex_cfg = brex_cfg
        self.nbctl = ovn_utils.OvnNbctl(self.central_node)
        self.sbctl = ovn_utils.OvnSbctl(self.central_node)
        self.net = cluster_cfg.cluster_net
        self.router = None
        self.load_balancer = None
        self.join_switch = None
        self.last_selected_worker = 0

    def start(self):
        self.central_node.start(self.cluster_cfg)
        for w in self.worker_nodes:
            w.start(self.cluster_cfg)
            w.configure(self.brex_cfg.physical_net)

        if self.cluster_cfg.clustered_db:
            nb_cluster_ips = [str(self.central_node.mgmt_ip),
                              str(self.central_node.mgmt_ip + 1),
                              str(self.central_node.mgmt_ip + 2)]
        else:
            nb_cluster_ips = [str(self.central_node.mgmt_ip)]
        self.nbctl.start_daemon(nb_cluster_ips)
        self.nbctl.set_global(
            'use_logical_dp_groups',
            self.cluster_cfg.logical_dp_groups
        )
        self.nbctl.set_inactivity_probe(self.cluster_cfg.db_inactivity_probe)
        self.sbctl.set_inactivity_probe(self.cluster_cfg.db_inactivity_probe)

    def create_cluster_router(self, rtr_name):
        self.router = self.nbctl.lr_add(rtr_name)

    def create_cluster_load_balancer(self, lb_name):
        self.load_balancer = lb.OvnLoadBalancer(lb_name, self.nbctl,
                                                self.cluster_cfg.vips)
        self.load_balancer.add_vips(self.cluster_cfg.static_vips)

    def create_cluster_join_switch(self, sw_name):
        self.join_switch = self.nbctl.ls_add(sw_name,
                                             self.cluster_cfg.gw_net)

        lrp_ip = netaddr.IPAddress(self.cluster_cfg.gw_net.last - 1)
        self.join_rp = self.nbctl.lr_port_add(
            self.router, 'rtr-to-join', RandMac(), lrp_ip,
            self.cluster_cfg.gw_net.prefixlen
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
            str(vip_ip + i): [str(p.ip) for p in ports]
            for i, ports in enumerate(backend_lists)
        }
        self.load_balancer.add_vips(vips)

    def unprovision_vips(self):
        self.load_balancer.clear_vips()
        self.load_balancer.add_vips(self.cluster_cfg.static_vips)

    def select_worker_for_port(self):
        self.last_selected_worker += 1
        self.last_selected_worker %= len(self.worker_nodes)
        return self.worker_nodes[self.last_selected_worker]
