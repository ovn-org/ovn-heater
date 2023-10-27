from collections import namedtuple
from ovn_context import Context
from cms.ovn_kubernetes import Namespace
from ovn_ext_cmd import ExtCmd
import ovn_load_balancer as lb
import ovn_exceptions
import netaddr


# By default simulate a deployment with 2 pods, one of which is the
# backend of a service.
DENSITY_PODS_VIP_RATIO = 2


DensityCfg = namedtuple(
    'DensityCfg', ['n_pods', 'n_startup', 'pods_vip_ratio']
)

DEFAULT_VIP_SUBNET = netaddr.IPNetwork('100.0.0.0/8')
DEFAULT_VIP_SUBNET6 = netaddr.IPNetwork('100::/32')
DEFAULT_VIP_PORT = 80
DEFAULT_BACKEND_PORT = 8080


class DensityHeavy(ExtCmd):
    def __init__(self, config, clusters, global_cfg):
        super().__init__(config, clusters)
        test_config = config.get('density_heavy', dict())
        pods_vip_ratio = test_config.get(
            'pods_vip_ratio', DENSITY_PODS_VIP_RATIO
        )
        self.config = DensityCfg(
            n_pods=test_config.get('n_pods', 0),
            n_startup=test_config.get('n_startup', 0),
            pods_vip_ratio=pods_vip_ratio,
        )
        if self.config.n_startup > self.config.n_pods:
            raise ovn_exceptions.OvnInvalidConfigException()
        self.lb_list = []
        self.vips = DEFAULT_VIP_SUBNET.iter_hosts()
        self.vips6 = DEFAULT_VIP_SUBNET6.iter_hosts()

    def create_lb(self, cluster, name, vip, backends, version):
        load_balancer = lb.OvnLoadBalancer(f'lb_{name}', cluster.nbctl)
        cluster.provision_lb(load_balancer)

        load_balancer.add_vip(
            vip,
            DEFAULT_VIP_PORT,
            backends,
            DEFAULT_BACKEND_PORT,
            version,
        )
        self.lb_list.append(load_balancer)

    def run_iteration(self, clusters, ns, index, global_cfg, passive):
        ovn = clusters[index % len(clusters)]
        ports = ovn.provision_ports(self.config.pods_vip_ratio, passive)
        ns.add_ports(ports, index % len(clusters))
        backends = ports[0:1]
        if global_cfg.run_ipv4:
            name = f'density_heavy_{index}'
            self.create_lb(ovn, name, next(self.vips), backends, 4)
        if global_cfg.run_ipv6:
            name = f'density_heavy6_{index}'
            self.create_lb(ovn, name, next(self.vips6), backends, 6)
        if not passive:
            ovn.ping_ports(ports)

    def run(self, clusters, global_cfg):
        if self.config.pods_vip_ratio == 0:
            return

        ns = Namespace(clusters, 'ns_density_heavy', global_cfg)
        with Context(
            clusters, 'density_heavy_startup', brief_report=True
        ) as ctx:
            for i in range(
                0, self.config.n_startup, self.config.pods_vip_ratio
            ):
                self.run_iteration(clusters, ns, i, global_cfg, passive=True)

        with Context(
            clusters,
            'density_heavy',
            (self.config.n_pods - self.config.n_startup)
            / self.config.pods_vip_ratio,
            test=self,
        ) as ctx:
            for i in ctx:
                index = i * self.config.pods_vip_ratio + self.config.n_startup
                self.run_iteration(
                    clusters, ns, index, global_cfg, passive=False
                )

        if not global_cfg.cleanup:
            return
        with Context(
            clusters, 'density_heavy_cleanup', len(clusters), brief_report=True
        ) as ctx:
            for i in ctx:
                clusters[i].unprovision_vips()
            ns.unprovision()
