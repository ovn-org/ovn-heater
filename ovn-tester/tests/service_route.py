from collections import namedtuple
from ovn_context import Context
from ovn_workload import Namespace
from ovn_ext_cmd import ExtCmd

import netaddr
import ovn_load_balancer as lb

ServiceRouteCfg = namedtuple('ServiceRouteCfg', ['n_lb', 'n_backends'])


DEFAULT_VIP_SUBNET = netaddr.IPNetwork('90.0.0.0/8')
DEFAULT_VIP_SUBNET6 = netaddr.IPNetwork('9::/32')
DEFAULT_VIP_PORT = 80
DEFAULT_BACKEND_PORT = 8080


class ServiceRoute(ExtCmd):
    def __init__(self, config, central_node, worker_nodes, global_cfg):
        super(ServiceRoute, self).__init__(config, central_node, worker_nodes)
        test_config = config.get('service_route', dict())
        self.config = ServiceRouteCfg(
            n_lb=test_config.get('n_lb', 16),
            n_backends=test_config.get('n_backends', 4),
        )

    def get_vip_map(self, vip, backend_lists, version):
        if version == 6:
            return {
                f'[{vip}]:{DEFAULT_VIP_PORT}': [
                    f'[{b.ip6}]:{DEFAULT_BACKEND_PORT}' for b in backend_lists
                ]
            }
        else:
            return {
                f'{vip}:{DEFAULT_VIP_PORT}': [
                    f'{b.ip}:{DEFAULT_BACKEND_PORT}' for b in backend_lists
                ]
            }

    def provide_cluster_lb(self, name, cluster, vip_subnet, backends, index):
        vip_net = vip_subnet.next(index)
        vip_ip = vip_net.ip.__add__(1)
        vip = self.get_vip_map(vip_ip, backends, vip_subnet.version)
        load_balancer = lb.OvnLoadBalancer(name, cluster.nbctl)
        load_balancer.add_vips(vip)
        cluster.provision_lb(load_balancer)

    def provide_node_load_balancer(
        self, name, cluster, node, backends, version
    ):
        load_balancer = lb.OvnLoadBalancer(name, cluster.nbctl)
        vip = node.ext_rp.ip.ip6 if version == 6 else node.ext_rp.ip.ip4
        load_balancer.add_vips(self.get_vip_map(vip, backends, version))
        load_balancer.add_to_routers([node.gw_router.name])
        load_balancer.add_to_switches([node.switch.name])

    def run(self, ovn, global_cfg):
        ns = Namespace(ovn, 'ns_service_route', global_cfg)
        with Context(ovn, 'service_route', self.config.n_lb, test=self) as ctx:
            for i in ctx:
                ports = ovn.provision_ports(self.config.n_backends + 1)
                ns.add_ports(ports)

                if ports[1].ip:
                    self.provide_cluster_lb(
                        f'slb-cluster-{i}',
                        ovn,
                        DEFAULT_VIP_SUBNET,
                        ports[1:],
                        i,
                    )
                if ports[1].ip6:
                    self.provide_cluster_lb(
                        f'slb6-cluster-{i}',
                        ovn,
                        DEFAULT_VIP_SUBNET6,
                        ports[1:],
                        i,
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
        with Context(ovn, 'service_route_cleanup', brief_report=True) as ctx:
            ns.unprovision()
