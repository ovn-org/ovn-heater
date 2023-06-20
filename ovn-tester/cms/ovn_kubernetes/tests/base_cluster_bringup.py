from collections import namedtuple

from ovn_context import Context
from ovn_ext_cmd import ExtCmd

ClusterBringupCfg = namedtuple('ClusterBringupCfg', ['n_pods_per_node'])


class BaseClusterBringup(ExtCmd):
    def __init__(self, config, clusters, global_cfg):
        super().__init__(config, clusters)
        test_config = config.get('base_cluster_bringup', dict())
        self.config = ClusterBringupCfg(
            n_pods_per_node=test_config.get('n_pods_per_node', 0),
        )

    def run(self, clusters, global_cfg):
        for c, cluster in enumerate(clusters):
            # create ovn topology
            with Context(
                cluster, 'base_cluster_bringup', len(cluster.worker_nodes)
            ) as ctx:
                cluster.create_cluster_router(f'lr-cluster{c+1}')
                cluster.create_cluster_join_switch(f'ls-join{c+1}')
                cluster.create_cluster_load_balancer(
                    f'lb-cluster{c+1}', global_cfg
                )
                for i in ctx:
                    worker = cluster.worker_nodes[i]
                    worker.provision(cluster)
                    ports = worker.provision_ports(
                        cluster, self.config.n_pods_per_node
                    )
                    worker.provision_load_balancers(cluster, ports, global_cfg)
                    worker.ping_ports(cluster, ports)
                cluster.provision_lb_group(f'cluster-lb-group{c+1}')
