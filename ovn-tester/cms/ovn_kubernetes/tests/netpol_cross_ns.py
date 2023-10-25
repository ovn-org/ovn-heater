from collections import namedtuple
from ovn_context import Context
from cms.ovn_kubernetes import Namespace
from ovn_ext_cmd import ExtCmd

NpCrossNsCfg = namedtuple('NpCrossNsCfg', ['n_ns', 'pods_ns_ratio'])


class NetpolCrossNs(ExtCmd):
    def __init__(self, config, clusters, global_cfg):
        super().__init__(config, clusters)
        test_config = config.get('netpol_cross', dict())
        self.config = NpCrossNsCfg(
            n_ns=test_config.get('n_ns', 0),
            pods_ns_ratio=test_config.get('pods_ns_ratio', 0),
        )

    def run(self, clusters, global_cfg):
        all_ns = []

        with Context(
            clusters, 'netpol_cross_ns_startup', brief_report=True
        ) as ctx:
            for i in range(self.config.n_ns):
                az_index = i % len(clusters)
                ovn = clusters[az_index]
                ports = ovn.provision_ports(self.config.pods_ns_ratio)
                ns = Namespace(
                    clusters,
                    f'NS_netpol_cross_ns_startup_{i}',
                    global_cfg,
                    az_index,
                )
                ns.add_ports(ports[0 : self.config.pods_ns_ratio], az_index)
                if global_cfg.run_ipv4:
                    ns.default_deny(4, az_index)
                if global_cfg.run_ipv6:
                    ns.default_deny(6, az_index)
                all_ns.append(ns)

        with Context(
            clusters, 'netpol_cross_ns', self.config.n_ns, test=self
        ) as ctx:
            for i in ctx:
                ns = all_ns[i]
                ext_ns = all_ns[(i + 1) % self.config.n_ns]
                if global_cfg.run_ipv4:
                    ns.allow_cross_namespace(ext_ns, 4)
                if global_cfg.run_ipv6:
                    ns.allow_cross_namespace(ext_ns, 6)
                ns.check_enforcing_cross_ns(ext_ns)

        if not global_cfg.cleanup:
            return
        with Context(
            clusters, 'netpol_cross_ns_cleanup', brief_report=True
        ) as ctx:
            for ns in all_ns:
                ns.unprovision()
