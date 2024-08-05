#!/usr/bin/env python3

from pathlib import Path
from dataclasses import dataclass
from typing import Dict
import yaml
import netaddr
import sys


def load_yaml(orig_yaml_file_name):
    with open(orig_yaml_file_name, "r") as orig_yaml_file:
        return yaml.safe_load(orig_yaml_file)


@dataclass
class GlobalConfig:
    """This contains all "global" level configuration options and their
    default values. If you want to add a new global level option then it needs
    to be listed here and have its type and default value specified."""

    log_cmds: bool = False
    cleanup: bool = False
    run_ipv4: bool = True
    run_ipv6: bool = False
    cms_name: str = ''


DEFAULT_N_VIPS = 2
DEFAULT_VIP_PORT = 80


def calculate_vips(subnet: str) -> Dict:
    vip_subnet = netaddr.IPNetwork(subnet)
    vip_gen = vip_subnet.iter_hosts()
    vip_range = range(0, DEFAULT_N_VIPS)
    prefix = '[' if vip_subnet.version == 6 else ''
    suffix = ']' if vip_subnet.version == 6 else ''
    return {
        f'{prefix}{next(vip_gen)}{suffix}:{DEFAULT_VIP_PORT}': None
        for _ in vip_range
    }


DEFAULT_N_STATIC_VIPS = 65
DEFAULT_N_STATIC_BACKENDS = 2
DEFAULT_STATIC_BACKEND_SUBNET = netaddr.IPNetwork('6.0.0.0/8')
DEFAULT_STATIC_BACKEND_SUBNET6 = netaddr.IPNetwork('6::/32')
DEFAULT_BACKEND_PORT = 8080


def calculate_static_vips(vip_subnet: str) -> Dict:
    vip_subnet = netaddr.IPNetwork(vip_subnet)
    if vip_subnet.version == 6:
        backend_subnet = DEFAULT_STATIC_BACKEND_SUBNET6
    else:
        backend_subnet = DEFAULT_STATIC_BACKEND_SUBNET

    vip_gen = vip_subnet.iter_hosts()
    vip_range = range(0, DEFAULT_N_STATIC_VIPS)

    backend_gen = backend_subnet.iter_hosts()
    backend_range = range(0, DEFAULT_N_STATIC_BACKENDS)

    prefix = '[' if vip_subnet.version == 6 else ''
    suffix = ']' if vip_subnet.version == 6 else ''

    # This assumes it's OK to use the same backend list for each
    # VIP. If we need to use different backends for each VIP,
    # then this will need to be updated
    backend_list = [
        f'{prefix}{next(backend_gen)}{suffix}:{DEFAULT_BACKEND_PORT}'
        for _ in backend_range
    ]

    return {
        f'{prefix}{next(vip_gen)}{suffix}:{DEFAULT_VIP_PORT}': backend_list
        for _ in vip_range
    }


@dataclass
class ClusterConfig:
    """This contains all "cluster" level configuration options and their
    default values. If you want to add a new cluster level option then it needs
    to be listed here and have its type and default value specified.

    Fields with "None" as their default are calculated in the __post_init__
    method."""

    monitor_all: bool = True
    logical_dp_groups: bool = True
    clustered_db: bool = True
    log_txns_db: bool = False
    datapath_type: str = "system"
    raft_election_to: int = 16
    northd_probe_interval: int = 16000
    northd_threads: int = 4
    db_inactivity_probe: int = 60000
    node_net: str = "192.16.0.0/16"
    enable_ssl: bool = True
    node_timeout_s: int = 20
    internal_net: str = "16.0.0.0/16"
    internal_net6: str = "16::/64"
    external_net: str = "20.0.0.0/16"
    external_net6: str = "20::/64"
    gw_net: str = "30.0.0.0/16"
    gw_net6: str = "30::/64"
    ts_net: str = "40.0.0.0/16"
    ts_net6: str = "40::/64"
    cluster_net: str = "16.0.0.0/4"
    cluster_net6: str = "16::/32"
    n_workers: int = 2
    n_relays: int = 0
    n_az: int = 1
    vips: Dict = None
    vips6: Dict = None
    vip_subnet: str = "4.0.0.0/8"
    vip_subnet6: str = "4::/32"
    static_vips: Dict = None
    static_vips6: Dict = None
    use_ovsdb_etcd: bool = False

    def __post_init__(self, **kwargs):
        # Some defaults have to be calculated
        if self.vips is None:
            self.vips = calculate_vips(self.vip_subnet)

        if self.vips6 is None:
            self.vips6 = calculate_vips(self.vip_subnet6)

        if self.static_vips is None:
            self.static_vips = calculate_static_vips(self.vip_subnet)

        if self.static_vips6 is None:
            self.static_vips6 = calculate_static_vips(self.vip_subnet6)


def translate_yaml(orig_yaml):
    global_cfg = GlobalConfig(**orig_yaml["global"])
    cluster_cfg = ClusterConfig(**orig_yaml["cluster"])

    dest_yaml = dict()
    dest_yaml["global"] = vars(global_cfg)
    dest_yaml["cluster"] = vars(cluster_cfg)

    for section, values in orig_yaml.items():
        if section != "global" and section != "cluster":
            dest_yaml[section] = values

    return dest_yaml


def write_yaml(dest_yaml, dest_yaml_file_name):
    with open(dest_yaml_file_name, "w") as dest_yaml_file:
        yaml.dump(dest_yaml, dest_yaml_file)


def main():
    orig_yaml_file_name = Path(sys.argv[1])
    dest_yaml_file_name = Path(sys.argv[2])

    orig_yaml = load_yaml(orig_yaml_file_name)
    dest_yaml = translate_yaml(orig_yaml)
    write_yaml(dest_yaml, dest_yaml_file_name)

    return 0


if __name__ == "__main__":
    main()
