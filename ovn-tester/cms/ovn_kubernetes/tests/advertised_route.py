import json
import re
import shlex
from collections import namedtuple
from itertools import islice

import netaddr

import ovn_exceptions
import ovn_load_balancer as lb
import ovn_stats
import ovn_utils
from ovn_context import Context
from ovn_ext_cmd import ExtCmd


DEFAULT_VIP_SUBNET = netaddr.IPNetwork('198.18.0.0/15')
DEFAULT_VIP_PORT = 80
DEFAULT_BACKEND_PORT = 10000
MAX_BACKEND_PORT = 65535
HEALTH_CHECK_OPTIONS = {
    'timeout': '1',
    'success_count': '1',
    'failure_count': '1',
}


class Route:
    def __init__(
        self,
        name,
        nbctl,
        vip,
        vip_port,
        backends,
        backend_port,
        ip_version=4,
        protocol='tcp',
    ):
        self.nbctl = nbctl
        self.vip = vip
        self.vip_port = vip_port
        self.ip_version = ip_version
        self.load_balancer = lb.OvnLoadBalancer(
            name,
            nbctl,
            protocols=[protocol],
        )
        self.load_balancer.add_vip(
            vip,
            vip_port,
            backends,
            backend_port,
            ip_version,
        )
        for backend in backends:
            endpoint_ip = backend.ip6 if ip_version == 6 else backend.ip
            source_ip = (
                backend.metadata.rp.ip.ip6
                if ip_version == 6
                else backend.metadata.rp.ip.ip4
            )
            self.nbctl.lb_add_ip_port_mapping(
                self.row,
                endpoint_ip,
                backend.name,
                source_ip,
            )

    @property
    def row(self):
        return self.load_balancer.lbs[0]

    @property
    def health_check(self):
        vip = f'[{self.vip}]' if self.ip_version == 6 else self.vip
        return self.row, f'{vip}:{self.vip_port}'

    def set_advertisement(self, distributed):
        self.nbctl.lb_set_options(
            self.row,
            {
                'distributed': str(distributed).lower(),
                'dynamic-routing-advertise': 'true',
            },
        )

    def add_to_routers(self, routers):
        self.load_balancer.add_to_routers(routers)

    def destroy(self):
        self.load_balancer.destroy()


AdvertisedRouteCfg = namedtuple(
    'AdvertisedRouteCfg',
    [
        'n_advertisers',
        'n_lbs',
        'n_backends',
        'batch_size',
        'timeout_s',
        'n_sparse_lbs',
        'n_sparse_iterations',
        'health_check_interval_s',
        'distributed',
        'advertise',
    ],
)


class AdvertisedRoute(ExtCmd):
    def __init__(self, config, clusters, global_cfg):
        test_config = config.get('advertised_route', {})
        workload_config = AdvertisedRouteCfg(
            n_advertisers=test_config.get('n_advertisers', 1),
            n_lbs=test_config.get('n_lbs', 100),
            n_backends=test_config.get('n_backends', 4),
            batch_size=test_config.get('batch_size', 500),
            timeout_s=test_config.get('timeout_s', 600),
            n_sparse_lbs=test_config.get('n_sparse_lbs', 10),
            n_sparse_iterations=test_config.get('n_sparse_iterations', 3),
            health_check_interval_s=test_config.get(
                'health_check_interval_s', 1
            ),
            distributed=test_config.get('distributed', True),
            advertise=test_config.get('advertise', True),
        )
        super().__init__(config, clusters)
        self.config = workload_config
        if (
            self.config.n_advertisers < 1
            or self.config.n_lbs < 1
            or self.config.n_backends < 1
            or self.config.batch_size < 1
            or self.config.n_sparse_lbs < 1
            or self.config.n_sparse_lbs > self.config.n_lbs
            or self.config.n_sparse_iterations < 1
            or self.config.health_check_interval_s < 1
            or DEFAULT_BACKEND_PORT + self.config.n_lbs > MAX_BACKEND_PORT
            or (2 * self.config.n_lbs + 2) >= DEFAULT_VIP_SUBNET.size
            or (self.config.distributed and not self.config.advertise)
            or global_cfg.run_ipv6
            or not global_cfg.run_ipv4
        ):
            raise ovn_exceptions.OvnInvalidConfigException()

        self.load_balancers = {}
        self.health_checks = {}
        self.backends = {}
        self.advertisers = {}

    def health_check_options(self):
        return {
            **HEALTH_CHECK_OPTIONS,
            'interval': str(self.config.health_check_interval_s),
        }

    def configure_advertisers(self, cluster):
        if self.config.n_advertisers > len(cluster.worker_nodes):
            raise ovn_exceptions.OvnInvalidConfigException()

        advertisers = cluster.worker_nodes[: self.config.n_advertisers]
        self.advertisers[cluster] = advertisers
        if not self.config.advertise:
            return

        for advertiser in advertisers:
            cluster.nbctl.lr_set_options(
                advertiser.gw_router,
                {
                    'dynamic-routing': 'true',
                    'dynamic-routing-vrf-id': 1000 + advertiser.id,
                },
            )
            cluster.nbctl.lr_port_set_options(
                advertiser.gw_rp,
                {
                    'dynamic-routing-redistribute': 'lb',
                    'dynamic-routing-maintain-vrf': 'true',
                },
            )

    @staticmethod
    def lb_options(distributed, advertise=True):
        if not advertise:
            return {}
        return {
            'distributed': str(distributed).lower(),
            'dynamic-routing-advertise': 'true',
        }

    def create_load_balancers(self, cluster, distributed=False):
        backends = cluster.provision_ports(
            self.config.n_backends, passive=False
        )
        self.backends[cluster] = backends
        self.load_balancers[cluster] = []
        self.health_checks[cluster] = []
        vips = DEFAULT_VIP_SUBNET.iter_hosts()

        for i in range(self.config.n_lbs):
            route = Route(
                f'advertised-route-{i}',
                cluster.nbctl,
                str(next(vips)),
                DEFAULT_VIP_PORT,
                backends,
                DEFAULT_BACKEND_PORT + i,
            )
            if self.config.advertise:
                route.set_advertisement(distributed)
            route.add_to_routers([cluster.router.name])
            self.load_balancers[cluster].append(route)
            self.health_checks[cluster].append(route.health_check)

        cluster.nbctl.lb_add_health_checks(
            self.health_checks[cluster],
            self.health_check_options(),
            self.config.batch_size,
        )

    def set_load_balancers_distributed(self, cluster, distributed, n_lbs=None):
        n_lbs = self.config.n_lbs if n_lbs is None else n_lbs
        lb_rows = [route.row for route in self.load_balancers[cluster][:n_lbs]]
        cluster.nbctl.lb_set_options_batch(
            lb_rows,
            self.lb_options(distributed, self.config.advertise),
            self.config.batch_size,
        )

    def monitor_rows(self, n_lbs=None):
        n_lbs = self.config.n_lbs if n_lbs is None else n_lbs
        return n_lbs * self.config.n_backends

    def listener_ports(self, sparse):
        n_lbs = self.config.n_sparse_lbs if sparse else self.config.n_lbs
        return range(DEFAULT_BACKEND_PORT, DEFAULT_BACKEND_PORT + n_lbs)

    def start_backend_listeners(self, cluster, sparse):
        ports = ' '.join(str(port) for port in self.listener_ports(sparse))
        for backend in self.backends[cluster]:
            command = (
                f'ip netns exec {shlex.quote(backend.name)} '
                f'python3 /tmp/tcp-listener.py {ports}'
            )
            backend.metadata.start_background_process(backend.name, command)

    def stop_backend_listeners(self, cluster):
        for backend in self.backends[cluster]:
            backend.metadata.stop_background_processes(backend.name)

    def destroy_topology(self, cluster):
        for route in self.load_balancers[cluster]:
            route.destroy()
        cluster.unprovision_ports(self.backends[cluster])

    def distributed_lbs(self):
        return self.config.n_lbs if self.config.distributed else 0

    def route_rows(self, n_distributed=None):
        if not self.config.advertise:
            return 0
        n_distributed = (
            self.distributed_lbs() if n_distributed is None else n_distributed
        )
        n_centralized = self.config.n_lbs - n_distributed
        return self.config.n_advertisers * (
            n_centralized + n_distributed * self.config.n_backends
        )

    def expected_counts(self, n_distributed=None):
        return self.route_rows(n_distributed), self.monitor_rows()

    def monitor_state(self, online, sparse=False):
        online_rows = self.monitor_rows(
            self.config.n_sparse_lbs if sparse and online else 0
        )
        if online and not sparse:
            online_rows = self.monitor_rows()
        states = {}
        if online_rows:
            states['online'] = online_rows
        offline_rows = self.monitor_rows() - online_rows
        if offline_rows:
            states['offline'] = offline_rows
        return states

    def expected_kernel_routes(self, online, sparse=False, n_distributed=None):
        if not self.config.advertise:
            return 0, 0
        n_distributed = (
            self.distributed_lbs() if n_distributed is None else n_distributed
        )
        n_centralized = self.config.n_lbs - n_distributed
        if not online:
            return n_centralized, 0
        n_active = self.config.n_sparse_lbs if sparse else n_distributed
        return n_centralized, min(n_active, n_distributed)

    def controller_engine_runs(self, advertiser):
        output = advertiser.run_output(
            'ovn-appctl -t ovn-controller inc-engine/show-stats',
            raise_on_error=True,
        )
        match = re.search(
            r'Node: route_exchange\s+'
            r'- recompute:\s+(\d+)\s+'
            r'- compute:\s+(\d+)',
            output,
        )
        if not match:
            raise ovn_exceptions.OvnTestException(
                f'No route_exchange statistics on {advertiser.container}'
            )
        return int(match.group(1)) + int(match.group(2))

    def engine_runs(self, cluster):
        advertisers = self.advertisers.get(
            cluster,
            cluster.worker_nodes[: self.config.n_advertisers],
        )
        return {
            advertiser: self.controller_engine_runs(advertiser)
            for advertiser in advertisers
        }

    def route_ids(self, cluster):
        return frozenset(
            uuid
            for uuid, _ in cluster.sbctl.advertised_lb_routes(
                DEFAULT_VIP_SUBNET
            )
        )

    def unaffected_route_ids(self, cluster):
        sparse_vips = frozenset(
            islice(
                DEFAULT_VIP_SUBNET.iter_hosts(),
                self.config.n_sparse_lbs,
            )
        )
        return frozenset(
            uuid
            for uuid, ip_prefix in cluster.sbctl.advertised_lb_routes(
                DEFAULT_VIP_SUBNET
            )
            if netaddr.IPNetwork(ip_prefix).ip not in sparse_vips
        )

    def check_route_ids(self, cluster, expected):
        if self.route_ids(cluster) != expected:
            raise ovn_exceptions.OvnTestException(
                'Advertised_Route row identities changed while the routes '
                'were stable'
            )

    def check_unaffected_route_ids(self, cluster, before):
        if self.unaffected_route_ids(cluster) != before:
            raise ovn_exceptions.OvnTestException(
                'Unaffected Advertised_Route rows were replaced'
            )

    def wait_for_engine_runs(self, before):
        expected = {
            advertiser.container: old_runs
            for advertiser, old_runs in before.items()
        }

        def get_runs():
            return {
                advertiser.container: self.controller_engine_runs(advertiser)
                for advertiser in before
            }

        ovn_utils.wait_for_value(
            get_runs,
            lambda observed: all(
                observed[name] > old_runs
                for name, old_runs in expected.items()
            ),
            self.config.timeout_s,
            f'route_exchange to advance from {expected}',
        )

    def kernel_route_count(self, advertiser):
        output = advertiser.run_output(
            'ip -j -4 route show table all proto 84',
            raise_on_error=True,
        )
        table = str(1000 + advertiser.id)
        routes = [
            route
            for route in json.loads(output or '[]')
            if str(route.get('table')) == table
        ]
        return sum(
            1
            for route in routes
            if route.get('dst')
            and netaddr.IPNetwork(route['dst']).ip in DEFAULT_VIP_SUBNET
        )

    def kernel_route_priorities(self, cluster, advertiser):
        return int(
            any(
                backend.metadata == advertiser
                for backend in self.backends[cluster]
            )
        ) + int(
            any(
                backend.metadata != advertiser
                for backend in self.backends[cluster]
            )
        )

    def wait_for_kernel_routes(self, cluster, expected_vips):
        centralized_vips, distributed_vips = expected_vips
        expected = {}
        for advertiser in self.advertisers[cluster]:
            expected[advertiser.container] = (
                centralized_vips
                + distributed_vips
                * self.kernel_route_priorities(cluster, advertiser)
            )

        def get_route_counts():
            return {
                advertiser.container: self.kernel_route_count(advertiser)
                for advertiser in self.advertisers[cluster]
            }

        ovn_utils.wait_for_value(
            get_route_counts,
            lambda observed: observed == expected,
            self.config.timeout_s,
            f'advertised kernel routes {expected}',
        )

    def wait_for_state(
        self,
        cluster,
        expected_kernel_routes,
        expected_monitor_states,
        engine_runs_before=None,
        n_distributed=None,
    ):
        expected_routes, expected_monitors = self.expected_counts(
            n_distributed
        )
        duration = cluster.sbctl.wait_for_advertised_route_state(
            expected_routes,
            expected_monitors,
            expected_monitor_states=expected_monitor_states,
            timeout_s=self.config.timeout_s,
            route_subnet=DEFAULT_VIP_SUBNET,
        )
        if engine_runs_before is not None:
            self.wait_for_engine_runs(engine_runs_before)
        self.wait_for_kernel_routes(cluster, expected_kernel_routes)
        return duration

    def run_setup(self, clusters):
        with Context(clusters, 'advertised_route_setup', test=self) as ctx:
            for _ in ctx:
                for cluster in clusters:
                    with ovn_stats.measure('Topology mutation'):
                        self.configure_advertisers(cluster)
                        self.create_load_balancers(cluster, distributed=False)
                    with ovn_stats.measure('Initial convergence'):
                        self.wait_for_state(
                            cluster,
                            self.expected_kernel_routes(
                                False, n_distributed=0
                            ),
                            self.monitor_state(False),
                            n_distributed=0,
                        )

    def run_distributed_transition(self, clusters):
        with Context(
            clusters,
            'advertised_route_distributed_transition',
            test=self,
        ) as ctx:
            for _ in ctx:
                for cluster in clusters:
                    engine_runs = self.engine_runs(cluster)
                    with ovn_stats.measure('Distributed enable mutation'):
                        self.set_load_balancers_distributed(cluster, True)
                    with ovn_stats.measure('Distributed enable convergence'):
                        self.wait_for_state(
                            cluster,
                            self.expected_kernel_routes(False),
                            self.monitor_state(False),
                            engine_runs_before=engine_runs,
                        )

    def transition_sparse_distributed(self, cluster, distributed):
        action = 'restore' if distributed else 'disable'
        n_distributed = (
            self.config.n_lbs
            if distributed
            else self.config.n_lbs - self.config.n_sparse_lbs
        )
        engine_runs = self.engine_runs(cluster)
        route_ids = self.unaffected_route_ids(cluster)
        with ovn_stats.measure(f'Sparse distributed {action} mutation'):
            self.set_load_balancers_distributed(
                cluster, distributed, self.config.n_sparse_lbs
            )
        with ovn_stats.measure(f'Sparse distributed {action} convergence'):
            self.wait_for_state(
                cluster,
                self.expected_kernel_routes(
                    False, n_distributed=n_distributed
                ),
                self.monitor_state(False),
                engine_runs_before=engine_runs,
                n_distributed=n_distributed,
            )
        self.check_unaffected_route_ids(cluster, route_ids)

    def run_sparse_distributed_flaps(self, clusters):
        with Context(
            clusters,
            'advertised_route_sparse_distributed_flap',
            max_iterations=self.config.n_sparse_iterations,
            test=self,
        ) as ctx:
            for _ in ctx:
                for cluster in clusters:
                    self.transition_sparse_distributed(cluster, False)
                    self.transition_sparse_distributed(cluster, True)

    def transition_monitor(self, cluster, online, sparse):
        scope = 'Sparse' if sparse else 'Bulk'
        state = 'online' if online else 'offline'
        engine_runs = (
            self.engine_runs(cluster) if self.config.distributed else None
        )
        route_ids = self.route_ids(cluster)
        with ovn_stats.measure(f'{scope} {state} monitor mutation'):
            if online:
                self.start_backend_listeners(cluster, sparse)
            else:
                self.stop_backend_listeners(cluster)

        with ovn_stats.measure(f'{scope} {state} monitor convergence'):
            self.wait_for_state(
                cluster,
                self.expected_kernel_routes(online, sparse),
                self.monitor_state(online, sparse),
                engine_runs_before=engine_runs,
            )
        self.check_route_ids(cluster, route_ids)

    def run_monitor_transition(self, clusters, online):
        state = 'online' if online else 'offline'
        with Context(
            clusters,
            f'advertised_route_bulk_{state}',
            test=self,
        ) as ctx:
            for _ in ctx:
                for cluster in clusters:
                    self.transition_monitor(cluster, online, sparse=False)

    def run_sparse_monitor_flaps(self, clusters):
        with Context(
            clusters,
            'advertised_route_sparse_flap',
            max_iterations=self.config.n_sparse_iterations,
            test=self,
        ) as ctx:
            for _ in ctx:
                for cluster in clusters:
                    self.transition_monitor(cluster, True, sparse=True)
                    self.transition_monitor(cluster, False, sparse=True)

    def run_forced_recompute(self, clusters):
        with Context(
            clusters,
            'advertised_route_forced_recompute',
            test=self,
        ) as ctx:
            for _ in ctx:
                for cluster in clusters:
                    before = self.engine_runs(cluster)
                    route_ids = self.route_ids(cluster)
                    with ovn_stats.measure('Unchanged full recompute'):
                        for advertiser in self.advertisers[cluster]:
                            advertiser.run_output(
                                'ovn-appctl -t ovn-controller '
                                'inc-engine/recompute',
                                raise_on_error=True,
                            )
                        self.wait_for_engine_runs(before)
                        self.wait_for_state(
                            cluster,
                            self.expected_kernel_routes(False),
                            self.monitor_state(False),
                        )
                    self.check_route_ids(cluster, route_ids)

    def run_sb_reconnect(self, clusters):
        with Context(
            clusters,
            'advertised_route_sb_reconnect',
            test=self,
        ) as ctx:
            for _ in ctx:
                for cluster in clusters:
                    before = self.engine_runs(cluster)
                    route_ids = self.route_ids(cluster)
                    with ovn_stats.measure('Southbound reconnect mutation'):
                        cluster.reconnect_sb()
                    with ovn_stats.measure('Southbound reconnect convergence'):
                        self.wait_for_engine_runs(before)
                        self.wait_for_state(
                            cluster,
                            self.expected_kernel_routes(False),
                            self.monitor_state(False),
                        )
                    self.check_route_ids(cluster, route_ids)

    def run_cleanup(self, clusters):
        with Context(
            clusters,
            'advertised_route_cleanup',
            brief_report=True,
            test=self,
        ) as ctx:
            for _ in ctx:
                for cluster in clusters:
                    engine_runs = (
                        self.engine_runs(cluster)
                        if self.config.advertise
                        else None
                    )
                    with ovn_stats.measure('Topology cleanup mutation'):
                        self.destroy_topology(cluster)
                    with ovn_stats.measure('Route cleanup convergence'):
                        cluster.sbctl.wait_for_advertised_route_state(
                            0,
                            0,
                            expected_monitor_states={},
                            timeout_s=self.config.timeout_s,
                            route_subnet=DEFAULT_VIP_SUBNET,
                        )
                        if engine_runs is not None:
                            self.wait_for_engine_runs(engine_runs)
                        self.wait_for_kernel_routes(cluster, (0, 0))

    def run(self, clusters, global_cfg):
        self.run_setup(clusters)
        if self.config.distributed:
            self.run_distributed_transition(clusters)
            self.run_sparse_distributed_flaps(clusters)
        self.run_monitor_transition(clusters, online=True)
        self.run_monitor_transition(clusters, online=False)
        self.run_sparse_monitor_flaps(clusters)
        self.run_forced_recompute(clusters)
        self.run_sb_reconnect(clusters)
        if global_cfg.cleanup:
            self.run_cleanup(clusters)
