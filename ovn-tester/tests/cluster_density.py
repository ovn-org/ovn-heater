from collections import namedtuple
from ovn_context import Context, get_current_iteration
from ovn_ext_cmd import ExtCmd
import ovn_exceptions
from ovn_workload import create_namespace


DENSITY_N_BUILD_PODS = 6
DENSITY_N_PODS = 4
DENSITY_N_TOT_PODS = DENSITY_N_BUILD_PODS + DENSITY_N_PODS


ClusterDensityCfg = namedtuple('ClusterDensityCfg',
                               ['n_runs',
                                'batch',
                                'n_startup',
                                'queries_per_second'])


class ClusterDensity(ExtCmd):
    def __init__(self, config, central_node, worker_nodes):
        super(ClusterDensity, self).__init__(
                config, central_node, worker_nodes)
        test_config = config.get('cluster_density', dict())
        self.config = ClusterDensityCfg(
            n_runs=test_config.get('n_runs', 0),
            batch=test_config.get('batch', DENSITY_N_TOT_PODS),
            n_startup=test_config.get('n_startup', 0),
            queries_per_second=test_config.get('queries_per_second', 20),
        )
        if self.config.batch % DENSITY_N_TOT_PODS:
            raise ovn_exceptions.OvnInvalidConfigException()
        self.batch_ratio = self.config.batch // DENSITY_N_TOT_PODS

    async def run(self, ovn, global_cfg):
        all_ns = []
        with Context('cluster_density_startup', brief_report=True) as ctx:
            # create 4 legacy pods per iteration.
            ports = await ovn.provision_ports(
                DENSITY_N_PODS * self.config.n_startup,
                passive=True
            )
            for i in range(self.config.n_startup):
                ns = await create_namespace(ovn, f'NS_density_{i}')
                await ns.add_ports(
                    ports[DENSITY_N_PODS * i:DENSITY_N_PODS * (i + 1)]
                )
                await ns.create_load_balancer()
                await ovn.provision_lb(ns.load_balancer)
                await ns.provision_vips_to_load_balancers(
                            [ports[i * DENSITY_N_PODS:i * DENSITY_N_PODS + 2],
                             [ports[i * DENSITY_N_PODS + 2]],
                             [ports[i * DENSITY_N_PODS + 3]]])
                all_ns.append(ns)

        with Context('cluster_density',
                     (self.config.n_runs - self.config.n_startup) //
                     self.batch_ratio, test=self) as ctx:
            await ctx.qps_test(self.config.queries_per_second,
                               self.tester, ovn, all_ns)

        if not global_cfg.cleanup:
            return
        with Context('cluster_density_cleanup', brief_report=True) as ctx:
            for ns in all_ns:
                await ns.unprovision()

    async def tester(self, ovn, all_ns):
        iteration = get_current_iteration()
        ns_num = self.config.n_startup + iteration.num
        ns = await create_namespace(ovn, f'NS_density_{ns_num}')
        await ns.create_load_balancer()
        await ovn.provision_lb(ns.load_balancer)
        all_ns.append(ns)

        iter_ports = await ovn.provision_ports(self.config.batch)
        for j in range(self.batch_ratio):
            # create 6 short lived "build" pods
            build_ports = iter_ports[
                    j*DENSITY_N_TOT_PODS:
                    j*DENSITY_N_TOT_PODS+DENSITY_N_BUILD_PODS]
            await ns.add_ports(build_ports)
            await ovn.ping_ports(build_ports)
            # create 4 legacy pods
            ports = iter_ports[
                    j*DENSITY_N_TOT_PODS+DENSITY_N_BUILD_PODS:
                    (j+1)*DENSITY_N_TOT_PODS]
            await ns.add_ports(ports)
            # add VIPs and backends to cluster load-balancer
            await ns.provision_vips_to_load_balancers(
                    [ports[0:1], ports[2:3], ports[3:4]])
            await ovn.ping_ports(ports)
            await ns.unprovision_ports(build_ports)
