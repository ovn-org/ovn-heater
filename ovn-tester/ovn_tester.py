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
from ovn_workload import (
    BrExConfig,
    ClusterConfig,
)
from ovn_utils import DualStackSubnet
from ovs.stream import Stream


GlobalCfg = namedtuple(
    'GlobalCfg', ['log_cmds', 'cleanup', 'run_ipv4', 'run_ipv6', 'cms_name']
)


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
        monitor_all=cluster_args['monitor_all'],
        logical_dp_groups=cluster_args['logical_dp_groups'],
        clustered_db=cluster_args['clustered_db'],
        log_txns_db=cluster_args['log_txns_db'],
        datapath_type=cluster_args['datapath_type'],
        raft_election_to=cluster_args['raft_election_to'],
        node_net=netaddr.IPNetwork(cluster_args['node_net']),
        n_relays=cluster_args['n_relays'],
        n_az=cluster_args['n_az'],
        enable_ssl=cluster_args['enable_ssl'],
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
        ts_net=DualStackSubnet(
            netaddr.IPNetwork(cluster_args['ts_net'])
            if global_cfg.run_ipv4
            else None,
            netaddr.IPNetwork(cluster_args['ts_net6'])
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

    return global_cfg, cluster_cfg, brex_cfg


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
    'ext_cmd',
]


def load_cms(cms_name):
    mod = importlib.import_module(f'cms.{cms_name}')
    class_name = getattr(mod, 'OVN_HEATER_CMS_PLUGIN')
    cls = getattr(mod, class_name)
    return cls


def configure_tests(yaml, clusters, global_cfg):
    tests = []
    for section, cfg in yaml.items():
        if section in RESERVED:
            continue

        mod = importlib.import_module(
            f'cms.{global_cfg.cms_name}.tests.{section}'
        )
        class_name = ''.join(s.title() for s in section.split('_'))
        cls = getattr(mod, class_name)
        tests.append(cls(yaml, clusters, global_cfg))
    return tests


def set_ssl_keys(cluster_cfg):
    Stream.ssl_set_private_key_file(cluster_cfg.ssl_private_key)
    Stream.ssl_set_certificate_file(cluster_cfg.ssl_cert)
    Stream.ssl_set_ca_cert_file(cluster_cfg.ssl_cacert)


if __name__ == '__main__':
    if len(sys.argv) != 3:
        usage(sys.argv[0])
        sys.exit(1)

    with open(sys.argv[2], 'r') as yaml_file:
        config = yaml.safe_load(yaml_file)

    global_cfg, cluster_cfg, brex_cfg = read_config(config)

    setup_logging(global_cfg)

    if not global_cfg.cms_name or (
        not global_cfg.run_ipv4 and not global_cfg.run_ipv6
    ):
        raise ovn_exceptions.OvnInvalidConfigException()

    cms_cls = load_cms(global_cfg.cms_name)
    central, workers = read_physical_deployment(sys.argv[1], global_cfg)
    clusters = [
        cms_cls(cluster_cfg, central, brex_cfg, i)
        for i in range(cluster_cfg.n_az)
    ]
    for c in clusters:
        c.add_cluster_worker_nodes(workers)

    tests = configure_tests(config, clusters, global_cfg)

    if cluster_cfg.enable_ssl:
        set_ssl_keys(cluster_cfg)

    with Context(clusters, 'prepare_test clusters'):
        for c in clusters:
            c.prepare_test()
    for test in tests:
        test.run(clusters, global_cfg)
    sys.exit(0)
