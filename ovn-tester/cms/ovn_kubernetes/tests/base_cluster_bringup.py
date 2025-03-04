from collections import namedtuple

from randmac import RandMac
from ovn_utils import LSwitch, OvnIcNbctl
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
        self.ic_cluster = clusters[0] if len(clusters) > 1 else None

    def create_transit_switch(self):
        if self.ic_cluster is None:
            return

        inactivity_probe = (
            self.ic_cluster.cluster_cfg.db_inactivity_probe // 1000
        )
        ic_remote = f'tcp:{self.ic_cluster.cluster_cfg.node_net.ip + 2}:6645'
        OvnIcNbctl(None, ic_remote, inactivity_probe).ts_add()

    def connect_transit_switch(self, cluster):
        if self.ic_cluster is None:
            return
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
        if self.ic_cluster is None:
            return
        for cluster in clusters:
            if self.ic_cluster == cluster:
                continue
            for w in cluster.worker_nodes:
                port = w.lports[0]
                if port.ip:
                    self.ic_cluster.worker_nodes[0].run_ping(
                        self.ic_cluster,
                        self.ic_cluster.worker_nodes[0].lports[0].name,
                        port.ip,
                    )
                if port.ip6:
                    self.ic_cluster.worker_nodes[0].run_ping(
                        self.ic_cluster,
                        self.ic_cluster.worker_nodes[0].lports[0].name,
                        port.ip6,
                    )

    def run(self, clusters, global_cfg):
        self.create_transit_switch()

        for c, cluster in enumerate(clusters):
            # create ovn topology
            with Context(
                clusters, 'base_cluster_bringup', len(cluster.worker_nodes)
            ) as ctx:
                cluster.create_cluster_router(f'lr-cluster{c + 1}')
                cluster.create_cluster_join_switch(f'ls-join{c + 1}')
                cluster.create_cluster_load_balancer(
                    f'lb-cluster{c + 1}', global_cfg
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
                cluster.provision_lb_group(f'cluster-lb-group{c + 1}')

        # check ic connectivity
        self.check_ic_connectivity(clusters)
