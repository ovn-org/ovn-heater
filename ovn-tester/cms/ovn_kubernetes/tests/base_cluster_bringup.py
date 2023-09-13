from collections import namedtuple

from ovn_context import Context
from ovn_ext_cmd import ExtCmd

ClusterBringupCfg = namedtuple('ClusterBringupCfg', ['n_pods_per_node'])


class BaseClusterBringup(ExtCmd):
    def __init__(self, config, central_node, worker_nodes, global_cfg):
        super().__init__(config, central_node, worker_nodes)
        test_config = config.get('base_cluster_bringup', dict())
        self.config = ClusterBringupCfg(
            n_pods_per_node=test_config.get('n_pods_per_node', 0),
        )

    def run(self, ovn, global_cfg):
        # create ovn topology
        with Context(
            ovn, 'base_cluster_bringup', len(ovn.worker_nodes)
        ) as ctx:
            ovn.create_cluster_router('lr-cluster')
            ovn.create_cluster_join_switch('ls-join')
            ovn.create_cluster_load_balancer('lb-cluster', global_cfg)
            for i in ctx:
                worker = ovn.worker_nodes[i]
                worker.provision(ovn)
                ports = worker.provision_ports(
                    ovn, self.config.n_pods_per_node
                )
                worker.provision_load_balancers(ovn, ports, global_cfg)
                worker.ping_ports(ovn, ports)
            ovn.provision_lb_group()
