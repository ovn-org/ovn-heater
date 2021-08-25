from collections import namedtuple
from ovn_context import Context
from ovn_workload import Namespace
from ovn_ext_cmd import ExtCmd
import ovn_exceptions


DensityCfg = namedtuple('DensityCfg',
                        ['n_pods',
                         'n_startup',
                         'batch',
                         'pods_vip_ratio'])


class DensityHeavy(ExtCmd):
    def __init__(self, config, central_node, worker_nodes):
        super(DensityHeavy, self).__init__(
                config, central_node, worker_nodes)
        test_config = config.get('density_heavy', dict())
        self.config = DensityCfg(
            n_pods=test_config.get('n_pods', 0),
            n_startup=test_config.get('n_startup', 0),
            batch=test_config.get('batch', 1),
            pods_vip_ratio=test_config.get('pods_vip_ratio', 1)
        )
        if self.config.pods_vip_ratio > self.config.batch or \
           self.config.batch % self.config.pods_vip_ratio or \
           self.config.n_pods % self.config.batch:
            raise ovn_exceptions.OvnInvalidConfigException()

    def run(self, ovn, global_cfg):
        if self.config.pods_vip_ratio == 0:
            return

        ns = Namespace(ovn, 'ns_density_heavy')
        ns.create_load_balancer('lb_density_heavy')
        ovn.provision_lb(ns.load_balancer)

        with Context('density_heavy_startup', brief_report=True) as ctx:
            ports = ovn.provision_ports(self.config.n_startup, passive=True)
            ns.add_ports(ports)
            backends = [
                [ports[i]] for i in range(0, self.config.n_startup,
                                          self.config.pods_vip_ratio)
            ]
            ns.provision_vips_to_load_balancers(backends)

        with Context('density_heavy',
                     (self.config.n_pods - self.config.n_startup) /
                     self.config.batch, test=self) as ctx:
            for i in ctx:
                ports = ovn.provision_ports(self.config.batch)
                ns.add_ports(ports)
                backends = [
                        [ports[i]] for i in range(0, self.config.batch,
                                                  self.config.pods_vip_ratio)
                ]
                ns.provision_vips_to_load_balancers(backends)
                ovn.ping_ports(ports)

        if not global_cfg.cleanup:
            return
        with Context('density_heavy_cleanup', brief_report=True) as ctx:
            ovn.unprovision_vips()
            ns.unprovision()
