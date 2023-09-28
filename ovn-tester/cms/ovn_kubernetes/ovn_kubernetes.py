from ovn_context import Context
from ovn_workload import WorkerNode, Cluster
from ovn_utils import DualStackSubnet


OVN_HEATER_CMS_PLUGIN = 'OVNKubernetes'


class OVNKubernetes:
    @staticmethod
    def create_nodes(cluster_cfg, workers):
        mgmt_ip = cluster_cfg.node_net.ip + 2
        internal_net = cluster_cfg.internal_net
        external_net = cluster_cfg.external_net
        gw_net = cluster_cfg.gw_net
        worker_nodes = [
            WorkerNode(
                workers[i % len(workers)],
                f'ovn-scale-{i}',
                mgmt_ip + i,
                DualStackSubnet.next(internal_net, i),
                DualStackSubnet.next(external_net, i),
                gw_net,
                i,
            )
            for i in range(cluster_cfg.n_workers)
        ]
        return worker_nodes

    @staticmethod
    def prepare_test(central_node, worker_nodes, cluster_cfg, brex_cfg):
        ovn = Cluster(central_node, worker_nodes, cluster_cfg, brex_cfg)
        with Context(ovn, 'prepare_test'):
            ovn.start()
        return ovn
