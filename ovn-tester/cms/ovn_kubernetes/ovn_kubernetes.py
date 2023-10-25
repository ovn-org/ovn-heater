import logging
from collections import namedtuple

import netaddr

from randmac import RandMac

import ovn_load_balancer as lb
import ovn_utils
import ovn_stats

from ovn_utils import DualStackSubnet
from ovn_workload import (
    ChassisNode,
    Cluster,
    DEFAULT_BACKEND_PORT,
    DEFAULT_VIP_PORT,
)

log = logging.getLogger(__name__)
ClusterBringupCfg = namedtuple('ClusterBringupCfg', ['n_pods_per_node'])
OVN_HEATER_CMS_PLUGIN = 'OVNKubernetesCluster'


class OVNKubernetesCluster(Cluster):
    def __init__(self, cluster_cfg, central, brex_cfg, az):
        super().__init__(cluster_cfg, central, brex_cfg, az)
        self.net = cluster_cfg.cluster_net
        self.gw_net = ovn_utils.DualStackSubnet.next(
            cluster_cfg.gw_net,
            az * (cluster_cfg.n_workers // cluster_cfg.n_az),
        )
        self.router = None
        self.load_balancer = None
        self.load_balancer6 = None
        self.join_switch = None
        self.last_selected_worker = 0
        self.n_ns = 0
        self.ts_switch = None

    def add_cluster_worker_nodes(self, workers):
        cluster_cfg = self.cluster_cfg

        # Allocate worker IPs after central and relay IPs.
        mgmt_ip = (
            cluster_cfg.node_net.ip
            + 2
            + cluster_cfg.n_az
            * (len(self.central_nodes) + len(self.relay_nodes))
        )

        protocol = "ssl" if cluster_cfg.enable_ssl else "tcp"
        internal_net = cluster_cfg.internal_net
        external_net = cluster_cfg.external_net
        # Number of workers for each az
        n_az_workers = cluster_cfg.n_workers // cluster_cfg.n_az
        self.add_workers(
            [
                WorkerNode(
                    workers[i % len(workers)],
                    f'ovn-scale-{i}',
                    mgmt_ip + i,
                    protocol,
                    DualStackSubnet.next(internal_net, i),
                    DualStackSubnet.next(external_net, i),
                    self.gw_net,
                    i,
                )
                for i in range(
                    self.az * n_az_workers, (self.az + 1) * n_az_workers
                )
            ]
        )

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

    def provision_lb_group(self, name='cluster-lb-group'):
        self.lb_group = lb.OvnLoadBalancerGroup(name, self.nbctl)
        for w in self.worker_nodes:
            self.nbctl.ls_add_lbg(w.switch, self.lb_group.lbg)
            self.nbctl.lr_add_lbg(w.gw_router, self.lb_group.lbg)

    def provision_lb(self, lb):
        log.info(f'Creating load balancer {lb.name}')
        self.lb_group.add_lb(lb)


class WorkerNode(ChassisNode):
    def __init__(
        self,
        phys_node,
        container,
        mgmt_ip,
        protocol,
        int_net,
        ext_net,
        gw_net,
        unique_id,
    ):
        super().__init__(phys_node, container, mgmt_ip, protocol)
        self.int_net = int_net
        self.ext_net = ext_net
        self.gw_net = gw_net
        self.id = unique_id

    def configure(self, physical_net):
        self.configure_localnet(physical_net)
        phys_ctl = ovn_utils.PhysCtl(self)
        phys_ctl.external_host_provision(
            ip=self.ext_net.reverse(2), gw=self.ext_net.reverse()
        )

    @ovn_stats.timeit
    def provision(self, cluster):
        self.connect(cluster.get_relay_connection_string())
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
    def ping_external(self, cluster, port):
        if port.ip:
            self.run_ping(cluster, 'ext-ns', port.ip)
        if port.ip6:
            self.run_ping(cluster, 'ext-ns', port.ip6)
