from collections import namedtuple
from ovn_context import Context
from ovn_workload import Namespace
from ovn_ext_cmd import ExtCmd


DENSITY_N_BUILD_PODS = 6
DENSITY_N_PODS = 4


ClusterDensityCfg = namedtuple('ClusterDensityCfg',
                               ['n_runs',
                                'n_startup'])


class ClusterDensity(ExtCmd):
    def __init__(self, config, central_node, worker_nodes):
        super(ClusterDensity, self).__init__(
                config, central_node, worker_nodes)
        test_config = config.get('cluster_density', dict())
        self.config = ClusterDensityCfg(
            n_runs=test_config.get('n_runs', 0),
            n_startup=test_config.get('n_startup', 0)
        )

    def run(self, ovn, global_cfg):
        all_ns = []
        with Context('cluster_density_startup', 1, brief_report=True) as ctx:
            # create 4 legacy pods per iteration.
            ports = ovn.provision_ports(DENSITY_N_PODS * self.config.n_startup,
                                        passive=True)
            for i in range(self.config.n_startup):
                ns = Namespace(ovn, f'NS_{i}')
                ns.add_ports(
                    ports[DENSITY_N_PODS * i:DENSITY_N_PODS * (i + 1)]
                )
                all_ns.append(ns)

            backends = []
            backends.extend([
                ports[i * DENSITY_N_PODS:i * DENSITY_N_PODS + 1]
                for i in range(self.config.n_startup)
            ])
            backends.extend([
                [ports[i * DENSITY_N_PODS + 2]]
                for i in range(self.config.n_startup)
            ])
            backends.extend([
                [ports[i * DENSITY_N_PODS + 3]]
                for i in range(self.config.n_startup)
            ])
            ovn.provision_vips_to_load_balancers(backends)

        with Context('cluster_density',
                     self.config.n_runs - self.config.n_startup) as ctx:
            for i in ctx:
                # exec external cmd
                self.exec_cmd(i, 'cluster_density')

                ns = Namespace(ovn, 'NS_{}'.format(self.config.n_startup + i))
                all_ns.append(ns)

                # create 6 short lived "build" pods
                build_ports = ovn.provision_ports(DENSITY_N_BUILD_PODS)
                ns.add_ports(build_ports)
                ovn.ping_ports(build_ports)
                # create 4 legacy pods
                ports = ovn.provision_ports(DENSITY_N_PODS)
                ns.add_ports(ports)
                # add VIPs and backends to cluster load-balancer
                ovn.provision_vips_to_load_balancers([ports[0:1],
                                                      ports[2:3],
                                                      ports[3:4]])
                ovn.ping_ports(ports)
                ovn.unprovision_ports(build_ports)

        if not global_cfg.cleanup:
            return
        with Context('cluster_density_cleanup', brief_report=True) as ctx:
            for ns in all_ns:
                ns.unprovision()
