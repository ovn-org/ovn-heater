from collections import namedtuple
from ovn_context import Context
from cms.ovn_kubernetes import Namespace
from ovn_ext_cmd import ExtCmd


DensityCfg = namedtuple(
    'DensityCfg', ['n_pods', 'n_startup', 'pods_vip_ratio']
)


class DensityLight(ExtCmd):
    def __init__(self, config, clusters, global_cfg):
        super().__init__(config, clusters)
        test_config = config.get('density_light', dict())
        self.config = DensityCfg(
            n_pods=test_config.get('n_pods', 0),
            n_startup=test_config.get('n_startup', 0),
            pods_vip_ratio=0,
        )

    def run(self, clusters, global_cfg):
        ns = Namespace(clusters, 'ns_density_light', global_cfg)
        with Context(
            clusters, 'density_light_startup', len(clusters), brief_report=True
        ) as ctx:
            for i in ctx:
                ports = clusters[i].provision_ports(
                    self.config.n_startup, passive=True
                )
                ns.add_ports(ports, i)

        n_iterations = self.config.n_pods - self.config.n_startup
        with Context(
            clusters, 'density_light', n_iterations, test=self
        ) as ctx:
            for i in ctx:
                ovn = clusters[i % len(clusters)]
                ports = ovn.provision_ports(1)
                ns.add_ports(ports[0:1], i % len(clusters))
                ovn.ping_ports(ports)

        if not global_cfg.cleanup:
            return
        with Context(
            clusters, 'density_light_cleanup', brief_report=True
        ) as ctx:
            ns.unprovision()
