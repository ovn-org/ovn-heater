from collections import namedtuple

from randmac import RandMac
from ovn_utils import LSwitch
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

    def create_transit_switch(self, cluster):
        cluster.icnbctl.ts_add()

    def connect_transit_switch(self, cluster):
        uuid = cluster.nbctl.ls_get_uuid('ts', 10)
        cluster.ts_switch = LSwitch(
            name='ts',
            cidr=cluster.cluster_cfg.ts_net.n4,
            cidr6=cluster.cluster_cfg.ts_net.n6,
            uuid=uuid,
        )
        rp = cluster.nbctl.lr_port_add(
            cluster.router,
            f'lr-cluster{cluster.az}-to-ts',
            RandMac(),
            cluster.cluster_cfg.ts_net.forward(cluster.az),
        )
        cluster.nbctl.ls_port_add(
            cluster.ts_switch, f'ts-to-lr-cluster{cluster.az}', rp
        )
        cluster.nbctl.lr_port_set_gw_chassis(
            rp, cluster.worker_nodes[0].container
        )
        cluster.worker_nodes[0].vsctl.set_global_external_id(
            'ovn-is-interconn', 'true'
        )

    def check_ic_connectivity(self, clusters):
        ic_cluster = clusters[0]
        for cluster in clusters:
            if ic_cluster == cluster:
                continue
            for w in cluster.worker_nodes:
                port = w.lports[0]
                if port.ip:
                    ic_cluster.worker_nodes[0].run_ping(
                        ic_cluster,
                        ic_cluster.worker_nodes[0].lports[0].name,
                        port.ip,
                    )
                if port.ip6:
                    ic_cluster.worker_nodes[0].run_ping(
                        ic_cluster,
                        ic_cluster.worker_nodes[0].lports[0].name,
                        port.ip6,
                    )

    def run(self, clusters, global_cfg):
        self.create_transit_switch(clusters[0])

        for c, cluster in enumerate(clusters):
            # create ovn topology
            with Context(
                clusters, 'base_cluster_bringup', len(cluster.worker_nodes)
            ) as ctx:
                cluster.create_cluster_router(f'lr-cluster{c+1}')
                cluster.create_cluster_join_switch(f'ls-join{c+1}')
                cluster.create_cluster_load_balancer(
                    f'lb-cluster{c+1}', global_cfg
                )
                self.connect_transit_switch(cluster)
                for i in ctx:
                    worker = cluster.worker_nodes[i]
                    worker.provision(cluster)
                    ports = worker.provision_ports(
                        cluster, self.config.n_pods_per_node
                    )
                    worker.provision_load_balancers(cluster, ports, global_cfg)
                    worker.ping_ports(cluster, ports)
                cluster.provision_lb_group(f'cluster-lb-group{c+1}')

        # check ic connectivity
        self.check_ic_connectivity(clusters)
