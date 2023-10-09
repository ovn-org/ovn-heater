from ovn_context import Context
from ovn_workload import WorkerNode
from ovn_utils import DualStackSubnet


OVN_HEATER_CMS_PLUGIN = 'OVNKubernetes'


class OVNKubernetes:
    @staticmethod
    def add_cluster_worker_nodes(cluster, workers, az):
        cluster_cfg = cluster.cluster_cfg

        # Allocate worker IPs after central and relay IPs.
        mgmt_ip = (
            cluster_cfg.node_net.ip
            + 2
            + cluster_cfg.n_az
            * (len(cluster.central_nodes) + len(cluster.relay_nodes))
        )

        protocol = "ssl" if cluster_cfg.enable_ssl else "tcp"
        internal_net = cluster_cfg.internal_net
        external_net = cluster_cfg.external_net
        # Number of workers for each az
        n_az_workers = cluster_cfg.n_workers // cluster_cfg.n_az
        cluster.add_workers(
            [
                WorkerNode(
                    workers[i % len(workers)],
                    f'ovn-scale-{i}',
                    mgmt_ip + i,
                    protocol,
                    DualStackSubnet.next(internal_net, i),
                    DualStackSubnet.next(external_net, i),
                    cluster.gw_net,
                    i,
                )
                for i in range(az * n_az_workers, (az + 1) * n_az_workers)
            ]
        )

    @staticmethod
    def prepare_test(clusters):
        with Context(clusters, 'prepare_test clusters'):
            for c in clusters:
                c.start()
