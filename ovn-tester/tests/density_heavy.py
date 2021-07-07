from collections import namedtuple
from ovn_context import Context
from ovn_workload import Namespace


DensityCfg = namedtuple('DensityCfg',
                        ['n_pods',
                         'n_startup',
                         'pods_vip_ratio'])


class DensityHeavy(object):
    def __init__(self, config):
        self.config = DensityCfg(
            n_pods=config.get('n_pods', 0),
            n_startup=config.get('n_startup', 0),
            pods_vip_ratio=config.get('pods_vip_ratio', 1)
        )

    def run(self, ovn, global_cfg):
        if self.config.pods_vip_ratio == 0:
            return
    
        ns = Namespace(ovn, 'ns_density_heavy')
        with Context('density_heavy_startup', 1, brief_report=True) as ctx:
            ports = ovn.provision_ports(self.config.n_startup, passive=True)
            ns.add_ports(ports)
            backends = [
                [ports[i]] for i in range(0, self.config.n_startup,
                                          self.config.pods_vip_ratio)
            ]
            ovn.provision_vips_to_load_balancers(backends)
    
        with Context('density_heavy',
                     (self.config.n_pods - self.config.n_startup) / self.config.pods_vip_ratio) as ctx:
            for _ in ctx:
                ports = ovn.provision_ports(self.config.pods_vip_ratio)
                ns.add_ports(ports)
                ovn.provision_vips_to_load_balancers([[ports[0]]])
                ovn.ping_ports(ports)
    
        if not global_cfg.cleanup:
            return
        with Context('density_heavy_cleanup', brief_report=True) as ctx:
            ovn.unprovision_vips()
            ns.unprovision()

