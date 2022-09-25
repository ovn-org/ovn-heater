#!/usr/bin/env python3

import logging
import sys
import netaddr
import yaml
import importlib
import ovn_exceptions
import gc
import time

from collections import namedtuple
from ovn_context import Context
from ovn_sandbox import PhysicalNode
from ovn_workload import BrExConfig, ClusterConfig
from ovn_workload import CentralNode, WorkerNode, Cluster
from ovn_utils import DualStackSubnet
from ovs.stream import Stream


GlobalCfg = namedtuple(
    'GlobalCfg', ['log_cmds', 'cleanup', 'run_ipv4', 'run_ipv6']
)

ClusterBringupCfg = namedtuple('ClusterBringupCfg', ['n_pods_per_node'])


def usage(name):
    print(
        f'''
{name} PHYSICAL_DEPLOYMENT TEST_CONF
where PHYSICAL_DEPLOYMENT is the YAML file defining the deployment.
where TEST_CONF is the YAML file defining the test parameters.
''',
        file=sys.stderr,
    )


def read_physical_deployment(deployment, global_cfg):
    with open(deployment, 'r') as yaml_file:
        dep = yaml.safe_load(yaml_file)

        central_dep = dep['central-node']
        central_node = PhysicalNode(
            central_dep.get('name', 'localhost'), global_cfg.log_cmds
        )
        worker_nodes = [
            PhysicalNode(worker, global_cfg.log_cmds)
            for worker in dep['worker-nodes']
        ]
        return central_node, worker_nodes


# SSL files are installed by ovn-fake-multinode in these locations.
SSL_KEY_FILE = "/opt/ovn/ovn-privkey.pem"
SSL_CERT_FILE = "/opt/ovn/ovn-cert.pem"
SSL_CACERT_FILE = "/opt/ovn/pki/switchca/cacert.pem"


def read_config(config):
    global_args = config.get('global', dict())
    global_cfg = GlobalCfg(**global_args)

    cluster_args = config.get('cluster')
    cluster_cfg = ClusterConfig(
        cluster_cmd_path=cluster_args['cluster_cmd_path'],
        monitor_all=cluster_args['monitor_all'],
        logical_dp_groups=cluster_args['logical_dp_groups'],
        clustered_db=cluster_args['clustered_db'],
        datapath_type=cluster_args['datapath_type'],
        raft_election_to=cluster_args['raft_election_to'],
        node_net=netaddr.IPNetwork(cluster_args['node_net']),
        n_relays=cluster_args['n_relays'],
        enable_ssl=cluster_args['enable_ssl'],
        node_remote=cluster_args['node_remote'],
        northd_probe_interval=cluster_args['northd_probe_interval'],
        db_inactivity_probe=cluster_args['db_inactivity_probe'],
        node_timeout_s=cluster_args['node_timeout_s'],
        internal_net=DualStackSubnet(
            netaddr.IPNetwork(cluster_args['internal_net'])
            if global_cfg.run_ipv4
            else None,
            netaddr.IPNetwork(cluster_args['internal_net6'])
            if global_cfg.run_ipv6
            else None,
        ),
        external_net=DualStackSubnet(
            netaddr.IPNetwork(cluster_args['external_net'])
            if global_cfg.run_ipv4
            else None,
            netaddr.IPNetwork(cluster_args['external_net6'])
            if global_cfg.run_ipv6
            else None,
        ),
        gw_net=DualStackSubnet(
            netaddr.IPNetwork(cluster_args['gw_net'])
            if global_cfg.run_ipv4
            else None,
            netaddr.IPNetwork(cluster_args['gw_net6'])
            if global_cfg.run_ipv6
            else None,
        ),
        cluster_net=DualStackSubnet(
            netaddr.IPNetwork(cluster_args['cluster_net'])
            if global_cfg.run_ipv4
            else None,
            netaddr.IPNetwork(cluster_args['cluster_net6'])
            if global_cfg.run_ipv6
            else None,
        ),
        n_workers=cluster_args['n_workers'],
        vips=cluster_args['vips'],
        vips6=cluster_args['vips6'],
        vip_subnet=cluster_args['vip_subnet'],
        static_vips=cluster_args['static_vips'],
        static_vips6=cluster_args['static_vips6'],
        use_ovsdb_etcd=cluster_args['use_ovsdb_etcd'],
        northd_threads=cluster_args['northd_threads'],
        ssl_private_key=SSL_KEY_FILE,
        ssl_cert=SSL_CERT_FILE,
        ssl_cacert=SSL_CACERT_FILE,
    )

    brex_cfg = BrExConfig(
        physical_net=cluster_args.get('physical_net', 'providernet'),
    )

    bringup_args = config.get('base_cluster_bringup', dict())
    bringup_cfg = ClusterBringupCfg(
        n_pods_per_node=bringup_args.get('n_pods_per_node', 10)
    )
    return global_cfg, cluster_cfg, brex_cfg, bringup_cfg


def setup_logging(global_cfg):
    FORMAT = '%(asctime)s | %(name)-12s |%(levelname)s| %(message)s'
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format=FORMAT)
    logging.Formatter.converter = time.gmtime

    if gc.isenabled():
        # If the garbage collector is enabled, it runs from time to time, and
        # interrupts ovn-tester to do so. If we are timing an operation, then
        # the gc can distort the amount of time something actually takes to
        # complete, resulting in graphs with spikes.
        #
        # Disabling the garbage collector runs the theoretical risk of leaking
        # a lot of memory, but in practical tests, this has not been a
        # problem. If gigantic-scale tests end up introducing memory issues,
        # then we may want to manually run the garbage collector between test
        # iterations or between test runs.
        gc.disable()
        gc.set_threshold(0)

    if not global_cfg.log_cmds:
        return

    modules = [
        "ovsdbapp.backend.ovs_idl.transaction",
    ]
    for module_name in modules:
        logging.getLogger(module_name).setLevel(logging.DEBUG)


RESERVED = [
    'global',
    'cluster',
    'base_cluster_bringup',
    'ext_cmd',
]


def configure_tests(yaml, central_node, worker_nodes, global_cfg):
    tests = []
    for section, cfg in yaml.items():
        if section in RESERVED:
            continue

        mod = importlib.import_module(f'tests.{section}')
        class_name = ''.join(s.title() for s in section.split('_'))
        cls = getattr(mod, class_name)
        tests.append(cls(yaml, central_node, worker_nodes, global_cfg))
    return tests


def create_nodes(cluster_config, central, workers):
    mgmt_net = cluster_config.node_net
    mgmt_ip = mgmt_net.ip + 2
    internal_net = cluster_config.internal_net
    external_net = cluster_config.external_net
    gw_net = cluster_config.gw_net
    db_containers = (
        ['ovn-central-1', 'ovn-central-2', 'ovn-central-3']
        if cluster_config.clustered_db
        else ['ovn-central']
    )
    relay_containers = [
        f'ovn-relay-{i + 1}' for i in range(cluster_config.n_relays)
    ]
    central_node = CentralNode(
        central, db_containers, relay_containers, mgmt_net, mgmt_ip
    )
    worker_nodes = [
        WorkerNode(
            workers[i % len(workers)],
            f'ovn-scale-{i}',
            mgmt_net,
            mgmt_ip + i,
            DualStackSubnet.next(internal_net, i),
            DualStackSubnet.next(external_net, i),
            gw_net,
            i,
        )
        for i in range(cluster_config.n_workers)
    ]
    return central_node, worker_nodes


def set_ssl_keys(cluster_cfg):
    Stream.ssl_set_private_key_file(cluster_cfg.ssl_private_key)
    Stream.ssl_set_certificate_file(cluster_cfg.ssl_cert)
    Stream.ssl_set_ca_cert_file(cluster_cfg.ssl_cacert)


def prepare_test(central_node, worker_nodes, cluster_cfg, brex_cfg):
    if cluster_cfg.enable_ssl:
        set_ssl_keys(cluster_cfg)
    ovn = Cluster(central_node, worker_nodes, cluster_cfg, brex_cfg)
    with Context(ovn, "prepare_test"):
        ovn.start()
    return ovn


def run_base_cluster_bringup(ovn, bringup_cfg, global_cfg):
    # create ovn topology
    with Context(ovn, "base_cluster_bringup", len(ovn.worker_nodes)) as ctx:
        ovn.create_cluster_router("lr-cluster")
        ovn.create_cluster_join_switch("ls-join")
        ovn.create_cluster_load_balancer("lb-cluster", global_cfg)
        for i in ctx:
            worker = ovn.worker_nodes[i]
            worker.provision(ovn)
            ports = worker.provision_ports(ovn, bringup_cfg.n_pods_per_node)
            worker.provision_load_balancers(ovn, ports, global_cfg)
            worker.ping_ports(ovn, ports)
        ovn.provision_lb_group()


if __name__ == '__main__':
    if len(sys.argv) != 3:
        usage(sys.argv[0])
        sys.exit(1)

    with open(sys.argv[2], 'r') as yaml_file:
        config = yaml.safe_load(yaml_file)

    global_cfg, cluster_cfg, brex_cfg, bringup_cfg = read_config(config)

    setup_logging(global_cfg)

    if not global_cfg.run_ipv4 and not global_cfg.run_ipv6:
        raise ovn_exceptions.OvnInvalidConfigException()

    central, workers = read_physical_deployment(sys.argv[1], global_cfg)
    central_node, worker_nodes = create_nodes(cluster_cfg, central, workers)
    tests = configure_tests(config, central_node, worker_nodes, global_cfg)

    ovn = prepare_test(central_node, worker_nodes, cluster_cfg, brex_cfg)
    run_base_cluster_bringup(ovn, bringup_cfg, global_cfg)
    for test in tests:
        test.run(ovn, global_cfg)
    sys.exit(0)
