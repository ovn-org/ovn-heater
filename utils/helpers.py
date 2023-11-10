try:
    from collections.abc import Mapping
except ImportError:
    from collections import Mapping

import os
from typing import Dict, Tuple


def get_node_config(config: Dict) -> Tuple[str, Dict]:
    mappings: Dict = {}
    if isinstance(config, Mapping):
        host = list(config.keys())[0]
        if config[host]:
            mappings = config[host]
    else:
        host = config
    return host, mappings


def get_prefix_suffix(hosts: str) -> Tuple[str, str]:
    prefix = os.path.commonprefix(hosts)
    rev = [x[::-1] for x in hosts]
    suffix = os.path.commonprefix(rev)[::-1]
    return prefix, suffix


def get_shortname(host: str, prefix: str, suffix: str) -> str:
    return host[len(prefix) : len(host) - len(suffix)]
