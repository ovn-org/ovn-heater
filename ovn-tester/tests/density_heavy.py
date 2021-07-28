from collections import namedtuple
from ovn_context import Context
from ovn_ext_cmd import ExtCmd
from ovn_load_balancer import create_load_balancer
import ovn_exceptions
import netaddr
from ovn_workload import create_namespace


# By default simulate a deployment with 2 pods, one of which is the
# backend of a service.
DENSITY_PODS_VIP_RATIO = 2


DensityCfg = namedtuple('DensityCfg',
                        ['n_pods',
                         'n_startup',
                         'batch',
                         'pods_vip_ratio'])

DEFAULT_VIP_SUBNET = netaddr.IPNetwork('100.0.0.0/16')
DEFAULT_VIP_PORT = 80
DEFAULT_BACKEND_PORT = 8080


class DensityHeavy(ExtCmd):
    def __init__(self, config, central_node, worker_nodes):
        super(DensityHeavy, self).__init__(
                config, central_node, worker_nodes)
        test_config = config.get('density_heavy', dict())
        pods_vip_ratio = test_config.get('pods_vip_ratio',
                                         DENSITY_PODS_VIP_RATIO)
        self.config = DensityCfg(
            n_pods=test_config.get('n_pods', 0),
            n_startup=test_config.get('n_startup', 0),
            batch=test_config.get('batch', pods_vip_ratio),
            pods_vip_ratio=pods_vip_ratio
        )
        if self.config.pods_vip_ratio > self.config.batch or \
           self.config.batch % self.config.pods_vip_ratio or \
           self.config.n_pods % self.config.batch:
            raise ovn_exceptions.OvnInvalidConfigException()
        self.lb_list = []

    async def create_lb(self, cluster, name, backend_lists):
        load_balancer = await create_load_balancer(f'lb_{name}', cluster.nbctl)
        await cluster.provision_lb(load_balancer)

        vip_net = DEFAULT_VIP_SUBNET.next(len(self.lb_list))
        vip_ip = vip_net.ip.__add__(1)

        vips = {
            f'{vip_ip + i}:{DEFAULT_VIP_PORT}':
                [f'{p.ip}:{DEFAULT_BACKEND_PORT}' for p in ports]
            for i, ports in enumerate(backend_lists)
        }
        await load_balancer.add_vips(vips)
        self.lb_list.append(load_balancer)

    async def run(self, ovn, global_cfg):
        if self.config.pods_vip_ratio == 0:
            return

        ns = await create_namespace(ovn, 'ns_density_heavy')
        await ns.create_load_balancer()
        await ovn.provision_lb(ns.load_balancer)
        with Context('density_heavy_startup', brief_report=True) as ctx:
            ports = await ovn.provision_ports(self.config.n_startup,
                                              passive=True)
            await ns.add_ports(ports)
            for i in range(0, self.config.n_startup,
                           self.config.pods_vip_ratio):
                await self.create_lb(ovn, 'density_heavy_' + str(i),
                                     [[ports[i]]])

        with Context('density_heavy',
                     (self.config.n_pods - self.config.n_startup) /
                     self.config.batch, test=self) as ctx:
            for i in ctx:
                ports = await ovn.provision_ports(self.config.batch)
                await ns.add_ports(ports)
                for j in range(0, self.config.batch,
                               self.config.pods_vip_ratio):
                    name = 'density_heavy_' + \
                        str(self.config.n_startup + i * self.config.batch + j)
                    self.create_lb(ovn, name, [[ports[j]]])
                await ovn.ping_ports(ports)

        if not global_cfg.cleanup:
            return
        with Context('density_heavy_cleanup', brief_report=True) as ctx:
            await ovn.unprovision_vips()
            await ns.unprovision()
