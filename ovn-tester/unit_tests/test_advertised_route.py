import contextlib
import threading
import unittest
import uuid
from types import SimpleNamespace
from unittest import mock

import netaddr

import ovn_exceptions
from cms.ovn_kubernetes.tests.advertised_route import AdvertisedRoute, Route
from ovn_utils import LoadBalancer, OvnNbctl, OvnSbctl


class AdvertisedRouteTest(unittest.TestCase):
    def make_test(self, **overrides):
        test_config = {
            'n_advertisers': 2,
            'n_lbs': 5,
            'n_backends': 3,
            'n_sparse_lbs': 2,
            'n_sparse_iterations': 2,
        }
        test_config.update(overrides)
        config = {
            'advertised_route': test_config,
        }
        global_cfg = SimpleNamespace(run_ipv4=True, run_ipv6=False)
        return AdvertisedRoute(config, [], global_cfg)

    def test_expected_row_counts_and_monitor_states(self):
        test = self.make_test()

        self.assertEqual(test.expected_counts(), (30, 15))
        self.assertEqual(test.monitor_state(True), {'online': 15})
        self.assertEqual(
            test.monitor_state(True, sparse=True),
            {'online': 6, 'offline': 9},
        )
        self.assertEqual(test.monitor_state(False), {'offline': 15})
        self.assertEqual(test.expected_counts(n_distributed=3), (22, 15))
        self.assertEqual(
            test.expected_kernel_routes(False, n_distributed=3), (2, 0)
        )

    def test_centralized_row_counts_and_route_state(self):
        test = self.make_test(distributed=False)

        self.assertEqual(test.expected_counts(), (10, 15))
        self.assertEqual(test.expected_kernel_routes(False), (5, 0))
        self.assertEqual(
            test.expected_kernel_routes(True, sparse=True), (5, 0)
        )

        cluster = mock.sentinel.cluster
        advertiser = mock.sentinel.advertiser
        test.backends[cluster] = [
            SimpleNamespace(metadata=advertiser),
            SimpleNamespace(metadata=mock.sentinel.other_advertiser),
        ]
        self.assertEqual(test.kernel_route_priorities(cluster, advertiser), 2)

    def test_feature_opt_out_has_no_routes(self):
        test = self.make_test(distributed=False, advertise=False)

        self.assertEqual(test.expected_counts(), (0, 15))
        self.assertEqual(test.expected_kernel_routes(False), (0, 0))
        self.assertEqual(test.expected_kernel_routes(True), (0, 0))

    def test_feature_opt_out_has_no_lb_options(self):
        self.assertEqual(
            AdvertisedRoute.lb_options(False, advertise=False), {}
        )

    def test_distributed_feature_opt_out_is_rejected(self):
        with self.assertRaises(ovn_exceptions.OvnInvalidConfigException):
            self.make_test(distributed=True, advertise=False)

    def test_feature_opt_out_does_not_configure_dynamic_routing(self):
        test = self.make_test(distributed=False, advertise=False)
        cluster = mock.MagicMock()
        cluster.worker_nodes = [mock.sentinel.worker_0, mock.sentinel.worker_1]

        test.configure_advertisers(cluster)

        self.assertEqual(test.advertisers[cluster], cluster.worker_nodes)
        cluster.nbctl.lr_set_options.assert_not_called()
        cluster.nbctl.lr_port_set_options.assert_not_called()

    def test_route_identity_check_detects_row_replacement(self):
        test = self.make_test()
        cluster = mock.MagicMock()
        original = frozenset({mock.sentinel.original})
        cluster.sbctl.advertised_lb_routes.return_value = [
            (mock.sentinel.replacement, '198.18.0.1/32')
        ]

        with self.assertRaises(ovn_exceptions.OvnTestException):
            test.check_route_ids(cluster, original)

    def test_sparse_transition_requires_unaffected_routes(self):
        test = self.make_test(
            n_advertisers=1,
            n_lbs=3,
            n_backends=1,
            n_sparse_lbs=1,
        )
        cluster = mock.MagicMock()
        sparse_id = uuid.uuid4()
        unchanged_id = uuid.uuid4()
        replaced_id = uuid.uuid4()
        cluster.sbctl.advertised_lb_routes.return_value = [
            (sparse_id, '198.18.0.1/32'),
            (unchanged_id, '198.18.0.2/32'),
            (replaced_id, '198.18.0.3/32'),
        ]
        before = test.unaffected_route_ids(cluster)
        cluster.sbctl.advertised_lb_routes.return_value = [
            (uuid.uuid4(), '198.18.0.1/32'),
            (unchanged_id, '198.18.0.2/32'),
            (uuid.uuid4(), '198.18.0.3/32'),
        ]

        with self.assertRaises(ovn_exceptions.OvnTestException):
            test.check_unaffected_route_ids(cluster, before)

    def test_sparse_transition_allows_sparse_route_replacement(self):
        test = self.make_test(
            n_advertisers=1,
            n_lbs=2,
            n_backends=1,
            n_sparse_lbs=1,
        )
        cluster = mock.MagicMock()
        unchanged_id = uuid.uuid4()
        cluster.sbctl.advertised_lb_routes.return_value = [
            (uuid.uuid4(), '198.18.0.1/32'),
            (unchanged_id, '198.18.0.2/32'),
        ]
        before = test.unaffected_route_ids(cluster)
        cluster.sbctl.advertised_lb_routes.return_value = [
            (uuid.uuid4(), '198.18.0.1/32'),
            (unchanged_id, '198.18.0.2/32'),
        ]

        test.check_unaffected_route_ids(cluster, before)

    def test_run_uses_only_regression_phases(self):
        test = self.make_test()
        phases = [
            'run_setup',
            'run_distributed_transition',
            'run_sparse_distributed_flaps',
            'run_monitor_transition',
            'run_sparse_monitor_flaps',
            'run_forced_recompute',
            'run_sb_reconnect',
            'run_cleanup',
        ]
        for phase in phases:
            setattr(test, phase, mock.Mock())

        clusters = [mock.sentinel.cluster]
        test.run(clusters, SimpleNamespace(cleanup=True))

        test.run_setup.assert_called_once_with(clusters)
        test.run_distributed_transition.assert_called_once_with(clusters)
        test.run_sparse_distributed_flaps.assert_called_once_with(clusters)
        self.assertEqual(
            test.run_monitor_transition.call_args_list,
            [
                mock.call(clusters, online=True),
                mock.call(clusters, online=False),
            ],
        )
        test.run_sparse_monitor_flaps.assert_called_once_with(clusters)
        test.run_forced_recompute.assert_called_once_with(clusters)
        test.run_sb_reconnect.assert_called_once_with(clusters)
        test.run_cleanup.assert_called_once_with(clusters)

    def test_centralized_run_skips_distributed_transition(self):
        test = self.make_test(distributed=False)
        phases = [
            'run_setup',
            'run_distributed_transition',
            'run_sparse_distributed_flaps',
            'run_monitor_transition',
            'run_sparse_monitor_flaps',
            'run_forced_recompute',
            'run_sb_reconnect',
            'run_cleanup',
        ]
        for phase in phases:
            setattr(test, phase, mock.Mock())

        clusters = [mock.sentinel.cluster]
        test.run(clusters, SimpleNamespace(cleanup=False))

        test.run_distributed_transition.assert_not_called()
        test.run_sparse_distributed_flaps.assert_not_called()
        test.run_sb_reconnect.assert_called_once_with(clusters)
        test.run_cleanup.assert_not_called()

    @mock.patch(
        'cms.ovn_kubernetes.tests.advertised_route.ovn_stats.measure',
        side_effect=lambda _: contextlib.nullcontext(),
    )
    @mock.patch('cms.ovn_kubernetes.tests.advertised_route.Context')
    def test_sb_reconnect_waits_for_controller_and_route_state(
        self, context, _measure
    ):
        test = self.make_test()
        cluster = mock.MagicMock()
        advertisers = [
            SimpleNamespace(container='worker-0'),
            SimpleNamespace(container='worker-1'),
        ]
        test.advertisers[cluster] = advertisers
        route_id = mock.sentinel.route_id
        cluster.sbctl.advertised_lb_routes.return_value = [
            (route_id, '198.18.0.1/32')
        ]
        context.return_value.__enter__.return_value.__iter__.return_value = [0]
        before = {mock.sentinel.advertiser: 10}
        test.engine_runs = mock.Mock(return_value=before)
        test.wait_for_engine_runs = mock.Mock()
        test.wait_for_state = mock.Mock()

        test.run_sb_reconnect([cluster])

        cluster.reconnect_sb.assert_called_once_with()
        test.wait_for_engine_runs.assert_called_once_with(before)
        self.assertEqual(
            cluster.sbctl.advertised_lb_routes.call_args_list,
            [
                mock.call(netaddr.IPNetwork('198.18.0.0/15')),
                mock.call(netaddr.IPNetwork('198.18.0.0/15')),
            ],
        )
        test.wait_for_state.assert_called_once_with(
            cluster,
            test.expected_kernel_routes(False),
            test.monitor_state(False),
        )

    def test_set_load_balancers_distributed_uses_requested_subset(self):
        test = self.make_test()
        cluster = mock.Mock()
        lb_rows = [mock.sentinel.lb_0, mock.sentinel.lb_1]
        test.load_balancers[cluster] = [
            SimpleNamespace(row=lb_row) for lb_row in lb_rows
        ]

        test.set_load_balancers_distributed(cluster, False, n_lbs=1)

        cluster.nbctl.lb_set_options_batch.assert_called_once_with(
            [mock.sentinel.lb_0],
            {
                'distributed': 'false',
                'dynamic-routing-advertise': 'true',
            },
            test.config.batch_size,
        )

    def test_listener_uses_tracked_background_process(self):
        test = self.make_test()
        cluster = mock.sentinel.cluster
        node = mock.Mock()
        test.backends[cluster] = [SimpleNamespace(name='lp-0', metadata=node)]

        test.start_backend_listeners(cluster, sparse=True)

        node.start_background_process.assert_called_once_with(
            'lp-0',
            'ip netns exec lp-0 python3 /tmp/tcp-listener.py 10000 10001',
        )


class AdvertisedRouteResourceTest(unittest.TestCase):
    @mock.patch('cms.ovn_kubernetes.tests.advertised_route.lb.OvnLoadBalancer')
    def test_creates_load_balancer_route(self, load_balancer):
        nbctl = mock.Mock()
        ovn_lb = load_balancer.return_value
        ovn_lb.lbs = [mock.sentinel.lb_row]
        backend = SimpleNamespace(
            ip='10.0.0.2',
            name='backend-0',
            metadata=SimpleNamespace(
                rp=SimpleNamespace(ip=SimpleNamespace(ip4='192.0.2.2'))
            ),
        )

        route = Route(
            'route-0',
            nbctl,
            '198.18.0.1',
            80,
            [backend],
            10000,
        )
        route.set_advertisement(True)
        route.add_to_routers(['router-0'])

        load_balancer.assert_called_once_with(
            'route-0', nbctl, protocols=['tcp']
        )
        ovn_lb.add_vip.assert_called_once_with(
            '198.18.0.1', 80, [backend], 10000, 4
        )
        nbctl.lb_add_ip_port_mapping.assert_called_once_with(
            mock.sentinel.lb_row,
            '10.0.0.2',
            'backend-0',
            '192.0.2.2',
        )
        nbctl.lb_set_options.assert_called_once_with(
            mock.sentinel.lb_row,
            {
                'distributed': 'true',
                'dynamic-routing-advertise': 'true',
            },
        )
        ovn_lb.add_to_routers.assert_called_once_with(['router-0'])
        self.assertEqual(
            route.health_check,
            (mock.sentinel.lb_row, '198.18.0.1:80'),
        )


class OvnNbctlBatchTest(unittest.TestCase):
    def test_lb_set_options_batch_replaces_options_in_bounded_transactions(
        self,
    ):
        nbctl = OvnNbctl.__new__(OvnNbctl)
        nbctl.idl = mock.MagicMock()
        transaction = nbctl.idl.transaction.return_value.__enter__.return_value
        commands = [mock.sentinel.command_0, mock.sentinel.command_1]
        nbctl.idl.db_set.side_effect = commands
        lbs = [
            LoadBalancer('lb-0', mock.sentinel.uuid_0),
            LoadBalancer('lb-1', mock.sentinel.uuid_1),
        ]

        nbctl.lb_set_options_batch(
            lbs,
            {'distributed': False, 'dynamic-routing-advertise': 'true'},
            batch_size=1,
        )

        self.assertEqual(nbctl.idl.transaction.call_count, 2)
        self.assertEqual(
            nbctl.idl.db_set.call_args_list,
            [
                mock.call(
                    'Load_Balancer',
                    mock.sentinel.uuid_0,
                    (
                        'options',
                        {
                            'distributed': 'False',
                            'dynamic-routing-advertise': 'true',
                        },
                    ),
                ),
                mock.call(
                    'Load_Balancer',
                    mock.sentinel.uuid_1,
                    (
                        'options',
                        {
                            'distributed': 'False',
                            'dynamic-routing-advertise': 'true',
                        },
                    ),
                ),
            ],
        )
        self.assertEqual(
            transaction.add.call_args_list,
            [
                mock.call(mock.sentinel.command_0),
                mock.call(mock.sentinel.command_1),
            ],
        )


class AdvertisedRouteSnapshotTest(unittest.TestCase):
    @staticmethod
    def table(*rows):
        return SimpleNamespace(rows={row.uuid: row for row in rows})

    def test_route_and_monitor_snapshots_filter_rows(self):
        route_uuid = uuid.uuid4()
        other_route_uuid = uuid.uuid4()
        route = SimpleNamespace(
            uuid=route_uuid,
            ip_prefix='198.18.0.1/32',
            external_ids={'source': 'lb'},
        )
        other_route = SimpleNamespace(
            uuid=other_route_uuid,
            ip_prefix='203.0.113.1/32',
            external_ids={'source': 'lb'},
        )
        offline_monitor = SimpleNamespace(
            uuid=uuid.uuid4(), type=['load-balancer'], status='offline'
        )
        pending_monitor = SimpleNamespace(
            uuid=uuid.uuid4(), type=['load-balancer'], status=[]
        )

        sbctl = OvnSbctl.__new__(OvnSbctl)
        sbctl.idl = SimpleNamespace(
            tables={
                'Advertised_Route': self.table(route, other_route),
                'Service_Monitor': self.table(
                    offline_monitor, pending_monitor
                ),
            },
            ovsdb_connection=SimpleNamespace(lock=threading.Lock()),
        )

        self.assertEqual(
            sbctl.advertised_lb_routes(netaddr.IPNetwork('198.18.0.0/15')),
            [(route_uuid, '198.18.0.1/32')],
        )
        self.assertEqual(sbctl.service_monitor_count(), 2)
        self.assertEqual(
            sbctl.service_monitor_summary(),
            (2, {'offline': 1, None: 1}),
        )

    def test_route_snapshot_requires_lb_source(self):
        no_source = SimpleNamespace(
            uuid=uuid.uuid4(),
            ip_prefix='198.18.0.1/32',
            external_ids={},
        )
        nat_route = SimpleNamespace(
            uuid=uuid.uuid4(),
            ip_prefix='198.18.0.2/32',
            external_ids={'source': 'nat'},
        )
        sbctl = OvnSbctl.__new__(OvnSbctl)
        sbctl.idl = SimpleNamespace(
            tables={'Advertised_Route': self.table(no_source, nat_route)},
            ovsdb_connection=SimpleNamespace(lock=threading.Lock()),
        )

        self.assertEqual(
            sbctl.advertised_lb_routes(netaddr.IPNetwork('198.18.0.0/15')),
            [],
        )
        self.assertEqual(sbctl.advertised_lb_routes(), [])

    def test_wait_checks_route_and_monitor_state(self):
        sbctl = OvnSbctl.__new__(OvnSbctl)
        sbctl.advertised_lb_route_count = mock.Mock(return_value=2)
        sbctl.service_monitor_summary = mock.Mock(
            return_value=(1, {'offline': 1})
        )
        sbctl.wait_for_advertised_route_state(
            expected_routes=2,
            expected_monitors=1,
            expected_monitor_states={'offline': 1},
            timeout_s=1,
            route_subnet=netaddr.IPNetwork('198.18.0.0/15'),
        )

        sbctl.advertised_lb_route_count.assert_called_once_with(
            netaddr.IPNetwork('198.18.0.0/15')
        )
        sbctl.service_monitor_summary.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
