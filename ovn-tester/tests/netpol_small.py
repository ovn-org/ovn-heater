from collections import namedtuple
from ovn_context import Context
from ovn_workload import Namespace
import ovn_exceptions

NpSmallCfg = namedtuple('NpSmallCfg',
                        ['n_ns',
                         'n_labels',
                         'pods_ns_ratio'])

class NetpolSmall(object):
    def __init__(self, config):
        self.config = NpSmallCfg(
            n_ns=config.get('n_ns', 0),
            n_labels=config.get('n_labels', 0),
            pods_ns_ratio=config.get('pods_ns_ratio', 0),
        )
        n_ports = self.config.pods_ns_ratio*self.config.n_ns
        if self.config.n_labels >= n_ports or self.config.n_labels <= 2:
            raise ovn_exceptions.OvnInvalidConfigException()

    def run(self, ovn, global_cfg):
        all_labels = dict()
        all_ns = []
        ports = []

        with Context('netpol_small_startup', brief_report=True) as ctx:
            ports = ovn.provision_ports(
                    self.config.pods_ns_ratio*self.config.n_ns)
            for i in range(self.config.pods_ns_ratio*self.config.n_ns):
                all_labels.setdefault(i % self.config.n_labels, []).append(ports[i])

            for i in range(self.config.n_ns):
                ns = Namespace(ovn, f'NS_{i}')
                ns.add_ports(ports[i*self.config.pods_ns_ratio :
                                   (i+1)*self.config.pods_ns_ratio])
                ns.default_deny()
                all_ns.append(ns)

        with Context('netpol_small', self.config.n_ns) as ctx:
            for i in ctx:
                ns = all_ns[i]
                for l in range(self.config.n_labels):
                    label = all_labels[l];
                    sub_ns_src = ns.create_sub_ns(label)

                    ex_label = label
                    n = (l+1)%self.config.n_labels
                    ex_label = ex_label + all_labels[n]
                    nlabel = [p for p in ports if p not in ex_label]
                    sub_ns_dst = ns.create_sub_ns(nlabel)

                    ns.allow_sub_namespace(sub_ns_src, sub_ns_dst)
                    worker = label[0].metadata
                    worker.ping_port(ovn, label[0], nlabel[0].ip)

        if not global_cfg.cleanup:
            return
        with Context('netpol_small_cleanup', brief_report=True) as ctx:
            for ns in all_ns:
                ns.unprovision()

