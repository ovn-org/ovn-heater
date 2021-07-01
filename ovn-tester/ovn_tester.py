#!/usr/bin/env python

import os
import sys
import ovn_context
import ovn_stats
import netaddr
import time
import yaml

from collections import namedtuple
from ovn_context import Context
from ovn_sandbox import PhysicalNode
from ovn_workload import BrExConfig, ClusterConfig
from ovn_workload import CentralNode, WorkerNode, Cluster, Namespace

DEFAULT_VIP_SUBNET = netaddr.IPNetwork('4.0.0.0/8')
DEFAULT_N_VIPS = 2


def calculate_default_vips():
    vip_gen = DEFAULT_VIP_SUBNET.iter_hosts()
    vip_range = range(0, DEFAULT_N_VIPS)
    return {str(next(vip_gen)): None for _ in vip_range}


DEFAULT_STATIC_VIP_SUBNET = netaddr.IPNetwork('5.0.0.0/8')
DEFAULT_N_STATIC_VIPS = 65
DEFAULT_STATIC_BACKEND_SUBNET = netaddr.IPNetwork('6.0.0.0/8')
DEFAULT_N_STATIC_BACKENDS = 2


def calculate_default_static_vips():
    vip_gen = DEFAULT_STATIC_VIP_SUBNET.iter_hosts()
    vip_range = range(0, DEFAULT_N_STATIC_VIPS)

    backend_gen = DEFAULT_STATIC_BACKEND_SUBNET.iter_hosts()
    backend_range = range(0, DEFAULT_N_STATIC_BACKENDS)
    # This assumes it's OK to use the same backend list for each
    # VIP. If we need to use different backends for each VIP,
    # then this will need to be updated
    backend_list = [str(next(backend_gen)) for _ in backend_range]

    return {str(next(vip_gen)): backend_list for _ in vip_range}

GlobalCfg = namedtuple('GlobalCfg', ['log_cmds', 'cleanup'])

ClusterBringupCfg = namedtuple('ClusterBringupCfg',
                               ['n_pods_per_node'])

DensityCfg = namedtuple('DensityCfg',
                        ['n_pods',
                         'n_startup',
                         'pods_vip_ratio'])

NsRange = namedtuple('NsRange',
                     ['start', 'n_pods'])
NsMultitenantCfg = namedtuple('NsMultitenantCfg',
                              ['n_namespaces',
                               'ranges',
                               'n_external_ips1',
                               'n_external_ips2'])

ClusterDensityCfg = namedtuple('ClusterDensityCfg',
                               ['n_runs',
                                'n_startup'])

NpCrossNsCfg = namedtuple('NpCrossNsCfg',
                          ['n_ns',
                           'pods_ns_ratio'])


def usage(name):
    print(f'''
{name} PHYSICAL_DEPLOYMENT TEST_CONF
where PHYSICAL_DEPLOYMENT is the YAML file defining the deployment.
where TEST_CONF is the YAML file defining the test parameters.
''', file=sys.stderr)


def read_physical_deployment(deployment, global_cfg):
    with open(deployment, 'r') as yaml_file:
        dep = yaml.safe_load(yaml_file)

        central_dep = dep['central-node']
        central_node = PhysicalNode(
            central_dep.get('name', 'localhost'), global_cfg.log_cmds)
        worker_nodes = [
            PhysicalNode(worker, global_cfg.log_cmds)
            for worker in dep['worker-nodes']
        ]
        return central_node, worker_nodes


def read_config(configuration):
    with open(configuration, 'r') as yaml_file:
        config = yaml.safe_load(yaml_file)
        global_args = config.get('global', dict())
        global_cfg = GlobalCfg(
            log_cmds=global_args.get('log_cmds', False),
            cleanup=global_args.get('cleanup', False)
        )

        cluster_args = config.get('cluster', dict())
        cluster_cfg = ClusterConfig(
            cluster_cmd_path=cluster_args.get(
                'cluster_cmd_path',
                '/root/ovn-heater/runtime/ovn-fake-multinode'
            ),
            monitor_all=cluster_args.get('monitor_all', True),
            logical_dp_groups=cluster_args.get('logical_dp_groups', True),
            clustered_db=cluster_args.get('clustered_db', True),
            raft_election_to=cluster_args.get('raft_election_to', 16),
            db_inactivity_probe=cluster_args.get('db_inactivity_probe', 60000),
            node_net=netaddr.IPNetwork(
                cluster_args.get('node_net', '192.16.0.0/16')
            ),
            node_remote=cluster_args.get(
                'node_remote',
                'ssl:192.16.0.1:6642,ssl:192.16.0.2:6642,ssl:192.16.0.3:6642'
            ),
            node_timeout_s=cluster_args.get('node_timeout_s', 20),
            internal_net=netaddr.IPNetwork(
                cluster_args.get('internal_net', '16.0.0.0/16')
            ),
            external_net=netaddr.IPNetwork(
                cluster_args.get('external_net', '3.0.0.0/16')
            ),
            gw_net=netaddr.IPNetwork(
                cluster_args.get('gw_net', '2.0.0.0/16')
            ),
            cluster_net=netaddr.IPNetwork(
                cluster_args.get('cluster_net', '16.0.0.0/4')
            ),
            n_workers=cluster_args.get('n_workers', 2),
            vips=cluster_args.get('vips', calculate_default_vips()),
            vip_subnet=DEFAULT_VIP_SUBNET,
            static_vips=cluster_args.get('static_vips',
                                         calculate_default_static_vips())
        )
        brex_cfg = BrExConfig(
            physical_net=cluster_args.get('physical_net', 'providernet'),
        )

        bringup_args = config.get('base_cluster_bringup', dict())
        bringup_cfg = ClusterBringupCfg(
            n_pods_per_node=bringup_args.get('n_pods_per_node', 10)
        )

        density_light_args = config.get('density_light', dict())
        density_light_cfg = DensityCfg(
            n_pods=density_light_args.get('n_pods', 0),
            n_startup=density_light_args.get('n_startup', 0),
            pods_vip_ratio=0
        )

        density_heavy_args = config.get('density_heavy', dict())
        density_heavy_cfg = DensityCfg(
            n_pods=density_heavy_args.get('n_pods', 0),
            n_startup=density_heavy_args.get('n_startup', 0),
            pods_vip_ratio=density_heavy_args.get('pods_vip_ratio', 1)
        )

        netpol_multitenant_args = config.get('netpol_multitenant', dict())
        ranges = [
            NsRange(
                start=range_args.get('start', 0),
                n_pods=range_args.get('n_pods', 5),
            ) for range_args in netpol_multitenant_args.get('ranges', list())
        ]
        ranges.sort(key=lambda x: x.start, reverse=True)
        netpol_multitenant_cfg = NsMultitenantCfg(
            n_namespaces=netpol_multitenant_args.get('n_namespaces', 0),
            n_external_ips1=netpol_multitenant_args.get('n_external_ips1', 3),
            n_external_ips2=netpol_multitenant_args.get('n_external_ips2', 20),
            ranges=ranges
        )
        cluster_density_args = config.get('cluster_density', dict())
        cluster_density_cfg = ClusterDensityCfg(
            n_runs=cluster_density_args.get('n_runs', 0),
            n_startup=cluster_density_args.get('n_startup', 0)
        )
        np_cross_args = config.get('network_policy_cross', dict())
        np_cross_cfg = NpCrossNsCfg(
            n_ns=np_cross_args.get('n_ns', 0),
            pods_ns_ratio=np_cross_args.get('pods_ns_ratio', 0)
        )
        return global_cfg, cluster_cfg, brex_cfg, bringup_cfg, \
            density_light_cfg, density_heavy_cfg, netpol_multitenant_cfg, \
            cluster_density_cfg, np_cross_cfg


def create_nodes(cluster_config, central, workers):
    mgmt_net = cluster_config.node_net
    mgmt_ip = mgmt_net.ip + 1
    internal_net = cluster_config.internal_net
    external_net = cluster_config.external_net
    gw_net = cluster_config.gw_net
    db_containers = [
        'ovn-central-1', 'ovn-central-2', 'ovn-central-3'
    ] if cluster_config.clustered_db else [
        'ovn-central'
    ]
    central_node = CentralNode(central, db_containers, mgmt_net, mgmt_ip)
    worker_nodes = [
        WorkerNode(workers[i % len(workers)], f'ovn-scale-{i}',
                   mgmt_net, mgmt_ip + i + 1, internal_net.next(i),
                   external_net.next(i), gw_net, i)
        for i in range(cluster_config.n_workers)
    ]
    return central_node, worker_nodes


def prepare_test(central_node, worker_nodes, cluster_cfg, brex_cfg):
    ovn = Cluster(central_node, worker_nodes, cluster_cfg, brex_cfg)
    with Context("prepare_test"):
        ovn.start()
    return ovn


def run_base_cluster_bringup(ovn, bringup_cfg):
    # create ovn topology
    with Context("base_cluster_bringup", len(ovn.worker_nodes)) as ctx:
        ovn.create_cluster_router("lr-cluster")
        ovn.create_cluster_join_switch("ls-join")
        ovn.create_cluster_load_balancer("lb-cluster")
        for i in ctx:
            worker = ovn.worker_nodes[i]
            worker.provision(ovn)
            ports = worker.provision_ports(ovn,
                                           bringup_cfg.n_pods_per_node)
            worker.provision_load_balancers(ovn, ports)
            worker.ping_ports(ovn, ports)


def run_test_density_light(ovn, global_cfg, cfg):
    ns = Namespace(ovn, 'ns_density_light')
    with Context('density_light_startup', 1, brief_report=True) as ctx:
        ports = ovn.provision_ports(cfg.n_startup, passive=True)
        ns.add_ports(ports)

    with Context('density_light', cfg.n_pods - cfg.n_startup) as ctx:
        for _ in ctx:
            ports = ovn.provision_ports(1)
            ns.add_ports(ports[0:1])
            ovn.ping_ports(ports)

    if not global_cfg.cleanup:
        return
    with Context('density_light_cleanup', brief_report=True) as ctx:
        ns.unprovision()


def run_test_density_heavy(ovn, global_cfg, cfg):
    if cfg.pods_vip_ratio == 0:
        return

    ns = Namespace(ovn, 'ns_density_heavy')
    with Context('density_heavy_startup', 1, brief_report=True) as ctx:
        ports = ovn.provision_ports(cfg.n_startup, passive=True)
        ns.add_ports(ports)
        backends = [
            [ports[i]] for i in range(0, cfg.n_startup, cfg.pods_vip_ratio)
        ]
        ovn.provision_vips_to_load_balancers(backends)

    with Context('density_heavy',
                 (cfg.n_pods - cfg.n_startup) / cfg.pods_vip_ratio) as ctx:
        for _ in ctx:
            ports = ovn.provision_ports(cfg.pods_vip_ratio)
            ns.add_ports(ports)
            ovn.provision_vips_to_load_balancers([[ports[0]]])
            ovn.ping_ports(ports)

    if not global_cfg.cleanup:
        return
    with Context('density_heavy_cleanup', brief_report=True) as ctx:
        ovn.unprovision_vips()
        ns.unprovision()


def run_test_netpol_multitenant(ovn, global_cfg, cfg):
    """
    Run a multitenant network policy test, for example:

    for i in range(n_namespaces):
        create address set AS_ns_i
        create port group PG_ns_i
        if i < 200:
            n_pods = 1 # 200 pods
        elif i < 480:
            n_pods = 5 # 1400 pods
        elif i < 495:
            n_pods = 20 # 300 pods
        else:
            n_pods = 100 # 500 pods
        create n_pods
        add n_pods to AS_ns_i
        add n_pods to PG_ns_i
        create acls:

    to-lport, ip.src == $AS_ns_i && outport == @PG_ns_i, allow-related
    to-lport, ip.src == {ip1, ip2, ip3} && outport == @PG_ns_i, allow-related
    to-lport, ip.src == {ip1, ..., ip20} && outport == @PG_ns_i, allow-related
    """
    external_ips1 = [
        netaddr.IPAddress('42.42.42.1') + i for i in range(cfg.n_external_ips1)
    ]
    external_ips2 = [
        netaddr.IPAddress('43.43.43.1') + i for i in range(cfg.n_external_ips2)
    ]

    all_ns = []
    with Context('netpol_multitenant', cfg.n_namespaces) as ctx:
        for i in ctx:
            # Get the number of pods from the "highest" range that includes i.
            n_ports = next((r.n_pods for r in cfg.ranges if i >= r.start), 1)
            ns = Namespace(ovn, f'ns_{i}')
            for _ in range(n_ports):
                for p in ovn.select_worker_for_port().provision_ports(ovn, 1):
                    ns.add_ports([p])
            ns.default_deny()
            ns.allow_within_namespace()
            ns.check_enforcing_internal()
            ns.allow_from_external(external_ips1)
            ns.allow_from_external(external_ips2, include_ext_gw=True)
            ns.check_enforcing_external()
            all_ns.append(ns)

    if not global_cfg.cleanup:
        return
    with Context('netpol_multitenant_cleanup', brief_report=True) as ctx:
        for ns in all_ns:
            ns.unprovision()


DENSITY_N_BUILD_PODS = 6
DENSITY_N_PODS = 4


def run_test_cluster_density(ovn, cfg):
    all_ns = []
    with Context('cluster_density_startup', 1, brief_report=True) as ctx:
        # create 4 legacy pods per iteration.
        ports = ovn.provision_ports(DENSITY_N_PODS * cfg.n_startup,
                                    passive=True)
        for i in range(cfg.n_startup):
            ns = Namespace(ovn, f'NS_{i}')
            ns.add_ports(ports[DENSITY_N_PODS * i:DENSITY_N_PODS * (i + 1)])
            all_ns.append(ns)

        backends = []
        backends.extend([
            ports[i * DENSITY_N_PODS:i * DENSITY_N_PODS + 1]
            for i in range(cfg.n_startup)
        ])
        backends.extend([
            [ports[i * DENSITY_N_PODS + 2]]
            for i in range(cfg.n_startup)
        ])
        backends.extend([
            [ports[i * DENSITY_N_PODS + 3]]
            for i in range(cfg.n_startup)
        ])
        ovn.provision_vips_to_load_balancers(backends)

    with Context('cluster_density', cfg.n_runs - cfg.n_startup) as ctx:
        for i in ctx:
            ns = Namespace(ovn, 'NS_{}'.format(cfg.n_startup + i))
            all_ns.append(ns)

            # create 6 short lived "build" pods
            build_ports = ovn.provision_ports(DENSITY_N_BUILD_PODS)
            ns.add_ports(build_ports)
            ovn.ping_ports(build_ports)
            # create 4 legacy pods
            ports = ovn.provision_ports(DENSITY_N_PODS)
            ns.add_ports(ports)
            # add VIPs and backends to cluster load-balancer
            ovn.provision_vips_to_load_balancers([ports[0:1],
                                                  ports[2:3],
                                                  ports[3:4]])
            ovn.ping_ports(ports)
            ovn.unprovision_ports(build_ports)

    if not global_cfg.cleanup:
        return
    with Context('cluster_density_cleanup', brief_report=True) as ctx:
        for ns in all_ns:
            ns.unprovision()

def run_test_np_cross(ovn, cfg):
    all_ns = []

    with Context('np_cross_startup', brief_report=True) as ctx:
        ports = ovn.provision_ports(cfg.pods_ns_ratio*cfg.n_ns)
        for i in range(cfg.n_ns):
            ns = Namespace(ovn, f'NS_{i}')
            ns.add_ports(ports[i*cfg.pods_ns_ratio :
                               (i + 1) *cfg.pods_ns_ratio - 1])
            all_ns.append(ns)

    with Context('np_cross', cfg.n_ns) as ctx:
        for i in ctx:
            ns = all_ns[i]
            ext_ns = all_ns[(i+1) % cfg.n_ns]
            ns.allow_cross_namespace(ext_ns)

    if not global_cfg.cleanup:
        return
    with Context('np_cross_cleanup', brief_report=True) as ctx:
        for ns in all_ns:
            ns.unprovision()

if __name__ == '__main__':
    if len(sys.argv) != 3:
        usage(sys.argv[0])
        sys.exit(1)

    global_cfg, cluster_cfg, brex_cfg, bringup_cfg, density_light_cfg, \
        density_heavy_cfg, ns_multitenant_cfg, \
        cluster_density_cfg, np_cross_cfg = read_config(sys.argv[2])

    central, workers = read_physical_deployment(sys.argv[1], global_cfg)
    central_node, worker_nodes = create_nodes(cluster_cfg, central, workers)

    ovn = prepare_test(central_node, worker_nodes, cluster_cfg, brex_cfg)
    run_base_cluster_bringup(ovn, bringup_cfg)
    run_test_density_light(ovn, global_cfg, density_light_cfg)
    run_test_density_heavy(ovn, global_cfg, density_heavy_cfg)
    run_test_netpol_multitenant(ovn, global_cfg, ns_multitenant_cfg)
    run_test_cluster_density(ovn, cluster_density_cfg)
    run_test_np_cross(ovn, np_cross_cfg)
    sys.exit(0)
