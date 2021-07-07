from collections import namedtuple
import netaddr
from ovn_context import Context
from ovn_workload import Namespace


NsRange = namedtuple('NsRange',
                     ['start', 'n_pods'])


NsMultitenantCfg = namedtuple('NsMultitenantCfg',
                              ['n_namespaces',
                               'ranges',
                               'n_external_ips1',
                               'n_external_ips2'])


class NetpolMultitenant(object):
    def __init__(self, config):
        ranges = [
            NsRange(
                start=range_args.get('start', 0),
                n_pods=range_args.get('n_pods', 5),
            ) for range_args in config.get('ranges', list())
        ]
        ranges.sort(key=lambda x: x.start, reverse=True)
        self.config = NsMultitenantCfg(
            n_namespaces=config.get('n_namespaces', 0),
            n_external_ips1=config.get('n_external_ips1', 3),
            n_external_ips2=config.get('n_external_ips2', 20),
            ranges=ranges
        )

    def run(self, ovn, global_cfg):
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
            netaddr.IPAddress('42.42.42.1') + i for i in range(self.config.n_external_ips1)
        ]
        external_ips2 = [
            netaddr.IPAddress('43.43.43.1') + i for i in range(self.config.n_external_ips2)
        ]
    
        all_ns = []
        with Context('netpol_multitenant', self.config.n_namespaces) as ctx:
            for i in ctx:
                # Get the number of pods from the "highest" range that includes i.
                n_ports = next((r.n_pods for r in self.config.ranges if i >= r.start), 1)
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
    
