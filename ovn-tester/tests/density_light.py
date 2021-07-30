from collections import namedtuple
from ovn_context import Context
from ovn_ext_cmd import ExtCmd
from ovn_workload import create_namespace


DensityCfg = namedtuple('DensityCfg',
                        ['n_pods',
                         'n_startup',
                         'pods_vip_ratio',
                         'queries_per_second'])


class DensityLight(ExtCmd):
    def __init__(self, config, central_node, worker_nodes):
        super(DensityLight, self).__init__(
                config, central_node, worker_nodes)
        test_config = config.get('density_light', dict())
        self.config = DensityCfg(
            n_pods=test_config.get('n_pods', 0),
            n_startup=test_config.get('n_startup', 0),
            pods_vip_ratio=0,
            queries_per_second=config.get('queries_per_second', 20),
        )

    async def run(self, ovn, global_cfg):
        ns = await create_namespace(ovn, 'ns_density_light')
        with Context('density_light_startup', brief_report=True) as ctx:
            ports = await ovn.provision_ports(self.config.n_startup,
                                              passive=True)
            await ns.add_ports(ports)

        n_iterations = self.config.n_pods - self.config.n_startup
        with Context('density_light', n_iterations, test=self) as ctx:
            await ctx.qps_test(self.config.queries_per_second,
                               self.provisioner, ns, ovn)

        if not global_cfg.cleanup:
            return
        with Context('density_light_cleanup', brief_report=True) as ctx:
            await ns.unprovision()

    async def provisioner(self, ns, ovn):
        ports = await ovn.provision_ports(1)
        await ns.add_ports(ports[0:1])
        await ovn.ping_ports(ports)
