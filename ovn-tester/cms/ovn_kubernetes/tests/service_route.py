from collections import namedtuple
from ovn_context import Context
from cms.ovn_kubernetes import Namespace
from ovn_ext_cmd import ExtCmd

import netaddr
import ovn_load_balancer as lb

ServiceRouteCfg = namedtuple('ServiceRouteCfg', ['n_lb', 'n_backends'])


DEFAULT_VIP_SUBNET = netaddr.IPNetwork('90.0.0.0/8')
DEFAULT_VIP_SUBNET6 = netaddr.IPNetwork('9::/32')
DEFAULT_VIP_PORT = 80
DEFAULT_BACKEND_PORT = 8080


class ServiceRoute(ExtCmd):
    def __init__(self, config, clusters, global_cfg):
        super().__init__(config, clusters)
        test_config = config.get('service_route', dict())
        self.config = ServiceRouteCfg(
            n_lb=test_config.get('n_lb', 16),
            n_backends=test_config.get('n_backends', 4),
        )
        self.vips = DEFAULT_VIP_SUBNET.iter_hosts()
        self.vips6 = DEFAULT_VIP_SUBNET6.iter_hosts()

    def provide_cluster_lb(self, name, cluster, vip, backends, version):
        load_balancer = lb.OvnLoadBalancer(name, cluster.nbctl)
        cluster.provision_lb(load_balancer)

        load_balancer.add_vip(
            vip,
            DEFAULT_VIP_PORT,
            backends,
            DEFAULT_BACKEND_PORT,
            version,
        )

    def provide_node_load_balancer(
        self, name, cluster, node, backends, version
    ):
        load_balancer = lb.OvnLoadBalancer(name, cluster.nbctl)
        vip = node.ext_rp.ip.ip6 if version == 6 else node.ext_rp.ip.ip4
        load_balancer.add_vip(
            vip, DEFAULT_VIP_PORT, backends, DEFAULT_BACKEND_PORT, version
        )
        load_balancer.add_to_routers([node.gw_router.name])
        load_balancer.add_to_switches([node.switch.name])

    def run(self, clusters, global_cfg):
        ns = Namespace(clusters, 'ns_service_route', global_cfg)
        with Context(
            clusters, 'service_route', self.config.n_lb, test=self
        ) as ctx:
            for i in ctx:
                ovn = clusters[i % len(clusters)]
                ports = ovn.provision_ports(self.config.n_backends + 1)
                ns.add_ports(ports, i % len(clusters))

                if ports[1].ip:
                    self.provide_cluster_lb(
                        f'slb-cluster-{i}',
                        ovn,
                        next(self.vips),
                        ports[1:],
                        4,
                    )
                if ports[1].ip6:
                    self.provide_cluster_lb(
                        f'slb6-cluster-{i}',
                        ovn,
                        next(self.vips6),
                        ports[1:],
                        6,
                    )

                for w in ovn.worker_nodes:
                    index = i * self.config.n_lb + w.id
                    if ports[1].ip:
                        self.provide_node_load_balancer(
                            f'slb-{index}', ovn, w, ports[1:], 4
                        )
                    if ports[1].ip6:
                        self.provide_node_load_balancer(
                            f'slb6-{index}', ovn, w, ports[1:], 6
                        )

        if not global_cfg.cleanup:
            return
        with Context(
            clusters, 'service_route_cleanup', brief_report=True
        ) as ctx:
            ns.unprovision()
