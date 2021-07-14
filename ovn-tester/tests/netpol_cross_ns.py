from collections import namedtuple
from ovn_context import Context
from ovn_workload import Namespace

NpCrossNsCfg = namedtuple('NpCrossNsCfg',
                          ['n_ns',
                           'pods_ns_ratio'])

class NetpolCrossNs(object):
    def __init__(self, config):
        self.config = NpCrossNsCfg(
            n_ns=config.get('n_ns', 0),
            pods_ns_ratio=config.get('pods_ns_ratio', 0),
        )

    def run(self, ovn, global_cfg):
        all_ns = []

        with Context('netpol_cross_ns_startup', brief_report=True) as ctx:
            ports = ovn.provision_ports(
                    self.config.pods_ns_ratio*self.config.n_ns)
            for i in range(self.config.n_ns):
                ns = Namespace(ovn, f'NS_{i}')
                ns.add_ports(ports[i*self.config.pods_ns_ratio :
                                   (i + 1) *self.config.pods_ns_ratio])
                ns.default_deny()
                all_ns.append(ns)

        with Context('netpol_cross_ns', self.config.n_ns) as ctx:
            for i in ctx:
                ns = all_ns[i]
                ext_ns = all_ns[(i+1) % self.config.n_ns]
                ns.allow_cross_namespace(ext_ns)
                ns.check_enforcing_cross_ns(ext_ns)

        if not global_cfg.cleanup:
            return
        with Context('netpol_cross_ns_cleanup', brief_report=True) as ctx:
            for ns in all_ns:
                ns.unprovision()
