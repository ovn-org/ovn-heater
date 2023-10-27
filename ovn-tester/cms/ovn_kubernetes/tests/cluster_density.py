from collections import namedtuple
from ovn_context import Context
from cms.ovn_kubernetes import Namespace
from ovn_ext_cmd import ExtCmd
import ovn_exceptions


DENSITY_N_BUILD_PODS = 6
DENSITY_N_PODS = 4
DENSITY_N_TOT_PODS = DENSITY_N_BUILD_PODS + DENSITY_N_PODS

# In ClusterDensity.run_iteration() we assume at least 4 different pods
# can be used as backends.
assert DENSITY_N_PODS >= 4

ClusterDensityCfg = namedtuple('ClusterDensityCfg', ['n_runs', 'n_startup'])


class ClusterDensity(ExtCmd):
    def __init__(self, config, clusters, global_cfg):
        super().__init__(config, clusters)
        test_config = config.get('cluster_density', dict())
        self.config = ClusterDensityCfg(
            n_runs=test_config.get('n_runs', 0),
            n_startup=test_config.get('n_startup', 0),
        )
        if self.config.n_startup > self.config.n_runs:
            raise ovn_exceptions.OvnInvalidConfigException()

    def run_iteration(self, clusters, index, global_cfg, passive):
        ns = Namespace(clusters, f'NS_density_{index}', global_cfg)
        az_index = index % len(clusters)
        ovn = clusters[az_index]
        # Create DENSITY_N_BUILD_PODS short lived "build" pods.
        if not passive:
            build_ports = ovn.provision_ports(DENSITY_N_BUILD_PODS, passive)
            ns.add_ports(build_ports, az_index)
            ovn.ping_ports(build_ports)

        # Add DENSITY_N_PODS test pods and provision them as backends
        # to the namespace load balancer.
        ports = ovn.provision_ports(DENSITY_N_PODS, passive)
        ns.add_ports(ports, az_index)
        ns.create_load_balancer(az_index)
        ovn.provision_lb(ns.load_balancer)
        if global_cfg.run_ipv4:
            ns.provision_vips_to_load_balancers(
                [ports[0:2], ports[2:3], ports[3:4]],
                4,
                az_index,
            )
        if global_cfg.run_ipv6:
            ns.provision_vips_to_load_balancers(
                [ports[0:2], ports[2:3], ports[3:4]],
                6,
                az_index,
            )

        # Ping the test pods and remove the short lived ones.
        if not passive:
            ovn.ping_ports(ports)
            ns.unprovision_ports(build_ports, az_index)
        return ns

    def run(self, clusters, global_cfg):
        all_ns = []
        with Context(
            clusters, 'cluster_density_startup', brief_report=True
        ) as ctx:
            for index in range(self.config.n_startup):
                all_ns.append(
                    self.run_iteration(
                        clusters, index, global_cfg, passive=True
                    )
                )

        with Context(
            clusters,
            'cluster_density',
            self.config.n_runs - self.config.n_startup,
            test=self,
        ) as ctx:
            for i in ctx:
                index = self.config.n_startup + i
                all_ns.append(
                    self.run_iteration(
                        clusters, index, global_cfg, passive=False
                    )
                )

        if not global_cfg.cleanup:
            return
        with Context(
            clusters, 'cluster_density_cleanup', brief_report=True
        ) as ctx:
            for ns in all_ns:
                ns.unprovision()
