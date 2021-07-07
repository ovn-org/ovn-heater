from collections import namedtuple
from ovn_context import Context
from ovn_workload import Namespace


DensityCfg = namedtuple('DensityCfg',
                        ['n_pods',
                         'n_startup',
                         'pods_vip_ratio'])


class DensityLight(object):
    def __init__(self, config):
        self.config = DensityCfg(
            n_pods=config.get('n_pods', 0),
            n_startup=config.get('n_startup', 0),
            pods_vip_ratio=0
        )

    def run(self, ovn, global_cfg):
        ns = Namespace(ovn, 'ns_density_light')
        with Context('density_light_startup', 1, brief_report=True) as ctx:
            ports = ovn.provision_ports(self.config.n_startup, passive=True)
            ns.add_ports(ports)
    
        n_iterations = self.config.n_pods - self.config.n_startup
        with Context('density_light', n_iterations) as ctx:
            for _ in ctx:
                ports = ovn.provision_ports(1)
                ns.add_ports(ports[0:1])
                ovn.ping_ports(ports)
    
        if not global_cfg.cleanup:
            return
        with Context('density_light_cleanup', brief_report=True) as ctx:
            ns.unprovision()

