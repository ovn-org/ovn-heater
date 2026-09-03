import unittest
from types import SimpleNamespace
from unittest import mock

import netaddr

from ovn_workload import Cluster


class ClusterTest(unittest.TestCase):
    def make_cluster(self, clustered_db):
        config = SimpleNamespace(
            clustered_db=clustered_db,
            enable_ssl=False,
            n_relays=0,
            node_net=netaddr.IPNetwork('192.0.2.0/24'),
        )
        return Cluster(config, SimpleNamespace(), SimpleNamespace(), az=0)

    def test_standalone_central_uses_runtime_container_name(self):
        cluster = self.make_cluster(clustered_db=False)

        self.assertEqual(cluster.central_nodes[0].container, 'ovn-central-az0')

    def test_clustered_central_uses_numbered_container_names(self):
        cluster = self.make_cluster(clustered_db=True)

        self.assertEqual(
            [node.container for node in cluster.central_nodes],
            [
                'ovn-central-az0-1',
                'ovn-central-az0-2',
                'ovn-central-az0-3',
            ],
        )

    def test_reconnect_sb_reconnects_every_central_node(self):
        cluster = self.make_cluster(clustered_db=True)
        cluster.central_nodes = [mock.Mock(), mock.Mock()]

        cluster.reconnect_sb()

        command = (
            'ovs-appctl -t /run/ovn/ovnsb_db.ctl ' 'ovsdb-server/reconnect'
        )
        for central in cluster.central_nodes:
            central.run_output.assert_called_once_with(
                command, raise_on_error=True
            )


if __name__ == '__main__':
    unittest.main()
