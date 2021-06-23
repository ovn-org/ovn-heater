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
from ovn_utils import PhysicalNode
from ovn_workload import BrExConfig, ClusterConfig
from ovn_workload import CentralNode, WorkerNode, Cluster

DEFAULT_VIP_SUBNET = netaddr.IPNetwork('4.0.0.0/8')
DEFAULT_N_VIPS = 2


def calculate_default_vips():
    vip_gen = DEFAULT_VIP_SUBNET.iter_hosts()
    vip_range = range(0, DEFAULT_N_VIPS)
    return {str(next(vip_gen)):None for _ in vip_range}


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

    return {str(next(vip_gen)):backend_list for _ in vip_range}


ClusterBringupCfg = namedtuple('ClusterBringupCfg',
                               ['n_pods_per_node'])


def usage(name):
    print("""
{} PHYSICAL_DEPLOYMENT TEST_CONF
where PHYSICAL_DEPLOYMENT is the YAML file defining the deployment.
where TEST_CONF is the YAML file defining the test parameters.
""".format(name), file=sys.stderr)


def read_physical_deployment(deployment, log_cmds):
    with open(deployment, 'r') as yaml_file:
        dep = yaml.safe_load(yaml_file)

        central_dep = dep['central-node']
        central_node = PhysicalNode(
            central_dep.get('name', 'localhost'), log_cmds)
        worker_nodes = [
            PhysicalNode(worker, log_cmds)
            for worker in dep['worker-nodes']
        ]
        return central_node, worker_nodes


def read_config(configuration):
    with open(configuration, 'r') as yaml_file:
        config = yaml.safe_load(yaml_file)
        global_args = config.get('global', dict())
        log_cmds = global_args.get('log_cmds', False)

        cluster_args = config.get('cluster', dict())
        cluster_cfg = ClusterConfig(
            cluster_cmd_path=cluster_args.get(
                'cluster_cmd_path',
                '/root/ovn-heater/runtime/ovn-fake-multinode'
            ),
            monitor_all=cluster_args.get('monitor_all', True),
            logical_dp_groups=cluster_args.get('logical_dp_groups', True),
            clustered_db=cluster_args.get('clustered_db', True),
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
        return log_cmds, cluster_cfg, brex_cfg, bringup_cfg


def create_nodes(cluster_config, central, workers):
    mgmt_net = cluster_config.node_net
    mgmt_ip = mgmt_net.ip + 1
    internal_net = cluster_config.internal_net
    external_net = cluster_config.external_net
    gw_net = cluster_config.gw_net
    central_name = \
        'ovn-central-1' if cluster_config.clustered_db else 'ovn-central'
    central_node = CentralNode(central, central_name, mgmt_net, mgmt_ip)
    worker_nodes = [
        WorkerNode(workers[i % len(workers)], 'ovn-scale-{}'.format(i),
                   mgmt_net, mgmt_ip + i + 1, internal_net.next(i),
                   external_net.next(i), gw_net.next(i), i)
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
        ovn.create_cluster_load_balancer("lb-cluster")
        for i in ctx:
            worker = ovn.worker_nodes[i]
            worker.provision(ovn)
            ports = worker.provision_ports(ovn,
                                           bringup_cfg.n_pods_per_node)
            worker.ping_ports(ovn, ports)

# FIXME: This needs to be reworked.
# def run_test_network_policy(ovn):
#     with Context("create_routed_lport", run_args['n_lports']) as ctx:
#         for _ in ctx:
#             ovn.create_routed_lport(lport_create_args, lport_bind_args)

if __name__ == '__main__':
    if len(sys.argv) != 3:
        usage(sys.argv[0])
        sys.exit(1)

    log_cmds, cluster_cfg, brex_cfg, bringup_cfg = read_config(sys.argv[2])

    central, workers = read_physical_deployment(sys.argv[1], log_cmds)
    central_node, worker_nodes = create_nodes(cluster_cfg, central, workers)

    ovn = prepare_test(central_node, worker_nodes, cluster_cfg, brex_cfg)
    run_base_cluster_bringup(ovn, bringup_cfg)
    sys.exit(0)
