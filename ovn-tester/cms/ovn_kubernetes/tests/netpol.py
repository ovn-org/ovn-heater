from collections import namedtuple
from ovn_context import Context
from cms.ovn_kubernetes import Namespace
from ovn_ext_cmd import ExtCmd
from itertools import chain
import ovn_exceptions

NpCfg = namedtuple('NpCfg', ['n_ns', 'n_labels', 'pods_ns_ratio'])


class NetPol(ExtCmd):
    def __init__(self, name, config, clusters):
        super().__init__(config, clusters)
        test_config = config.get(name, dict())
        self.config = NpCfg(
            n_ns=test_config.get('n_ns', 0),
            n_labels=test_config.get('n_labels', 0),
            pods_ns_ratio=test_config.get('pods_ns_ratio', 0),
        )
        n_ports = self.config.pods_ns_ratio * self.config.n_ns
        if self.config.n_labels >= n_ports or self.config.n_labels <= 2:
            raise ovn_exceptions.OvnInvalidConfigException()

        self.name = name
        self.all_labels = dict()
        self.all_ns = []
        self.ports = [[] for _ in range(self.config.n_ns)]

    def init(self, clusters, global_cfg):
        with Context(clusters, f'{self.name}_startup', brief_report=True) as _:
            for i in range(self.config.n_ns):
                az_index = i % len(clusters)
                ovn = clusters[az_index]
                self.ports[i] = ovn.provision_ports(self.config.pods_ns_ratio)
                ns = Namespace(clusters, f'NS_{self.name}_{i}', global_cfg)
                ns.add_ports(self.ports[i], az_index)
                ns.default_deny(4, az_index)
                self.all_ns.append(ns)
                for i, port in enumerate(
                    list(chain.from_iterable(self.ports))
                ):
                    self.all_labels.setdefault(
                        i % self.config.n_labels, []
                    ).append(port)

    def run(self, clusters, global_cfg, exclude=False):
        with Context(clusters, self.name, self.config.n_ns, test=self) as ctx:
            for i in ctx:
                ns = self.all_ns[i]
                for lbl in range(self.config.n_labels):
                    label = self.all_labels[lbl]
                    sub_ns_src = ns.create_sub_ns(label, global_cfg)

                    n = (lbl + 1) % self.config.n_labels
                    if exclude:
                        ex_label = label + self.all_labels[n]
                        nlabel = [
                            p for p in self.ports[i] if p not in ex_label
                        ]
                    else:
                        nlabel = self.all_labels[n]
                    sub_ns_dst = ns.create_sub_ns(nlabel, global_cfg)

                    if global_cfg.run_ipv4:
                        ns.allow_sub_namespace(sub_ns_src, sub_ns_dst, 4)
                    if global_cfg.run_ipv6:
                        ns.allow_sub_namespace(sub_ns_src, sub_ns_dst, 6)
                    worker = label[0].metadata
                    if label[0].ip and nlabel[0].ip:
                        worker.ping_port(clusters[0], label[0], nlabel[0].ip)
                    if label[0].ip6 and nlabel[0].ip6:
                        worker.ping_port(clusters[0], label[0], nlabel[0].ip6)

        if not global_cfg.cleanup:
            return
        with Context(
            clusters, f'{self.name}_cleanup', brief_report=True
        ) as ctx:
            for ns in self.all_ns:
                ns.unprovision()
