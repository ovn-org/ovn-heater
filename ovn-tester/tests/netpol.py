from collections import namedtuple
from ovn_context import Context
from ovn_ext_cmd import ExtCmd
from ovn_workload import create_namespace
import ovn_exceptions

NpCfg = namedtuple('NpCfg',
                   ['n_ns',
                    'n_labels',
                    'pods_ns_ratio'])


class NetPol(ExtCmd):
    def __init__(self, name, config, central_node, worker_nodes):
        super(NetPol, self).__init__(
                config, central_node, worker_nodes)
        test_config = config.get(name, dict())
        self.config = NpCfg(
            n_ns=test_config.get('n_ns', 0),
            n_labels=test_config.get('n_labels', 0),
            pods_ns_ratio=test_config.get('pods_ns_ratio', 0),
        )
        n_ports = self.config.pods_ns_ratio*self.config.n_ns
        if self.config.n_labels >= n_ports or self.config.n_labels <= 2:
            raise ovn_exceptions.OvnInvalidConfigException()

        self.name = name
        self.all_labels = dict()
        self.all_ns = []
        self.ports = []

    async def init(self, ovn):
        with Context(f'{self.name}_startup', brief_report=True) as _:
            self.ports = await ovn.provision_ports(
                    self.config.pods_ns_ratio*self.config.n_ns)
            for i in range(self.config.pods_ns_ratio*self.config.n_ns):
                self.all_labels.setdefault(
                        i % self.config.n_labels, []).append(self.ports[i])

            for i in range(self.config.n_ns):
                ns = await create_namespace(ovn, f'NS_{self.name}_{i}')
                await ns.add_ports(
                        self.ports[i * self.config.pods_ns_ratio:
                                   (i + 1)*self.config.pods_ns_ratio])
                await ns.default_deny()
                self.all_ns.append(ns)

    async def run(self, ovn, global_cfg, exclude=False):
        with Context(self.name, self.config.n_ns, test=self) as ctx:
            for i in ctx:
                ns = self.all_ns[i]
                for lbl in range(self.config.n_labels):
                    label = self.all_labels[lbl]
                    sub_ns_src = await ns.create_sub_ns(label)

                    n = (lbl + 1) % self.config.n_labels
                    if exclude:
                        ex_label = label + self.all_labels[n]
                        nlabel = [p for p in self.ports if p not in ex_label]
                    else:
                        nlabel = self.all_labels[n]
                    sub_ns_dst = await ns.create_sub_ns(nlabel)

                    await ns.allow_sub_namespace(sub_ns_src, sub_ns_dst)
                    worker = label[0].metadata
                    await worker.ping_port(ovn, label[0], nlabel[0].ip)

        if not global_cfg.cleanup:
            return
        with Context(f'{self.name}_cleanup', brief_report=True) as ctx:
            for ns in self.all_ns:
                await ns.unprovision()
