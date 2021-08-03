from collections import namedtuple
import netaddr
from ovn_context import Context, get_current_iteration
from ovn_ext_cmd import ExtCmd
from ovn_workload import create_namespace


NsRange = namedtuple('NsRange',
                     ['start', 'n_pods'])


NsMultitenantCfg = namedtuple('NsMultitenantCfg',
                              ['n_namespaces',
                               'ranges',
                               'n_external_ips1',
                               'n_external_ips2',
                               'queries_per_second'])


class NetpolMultitenant(ExtCmd):
    def __init__(self, config, central_node, worker_nodes):
        super(NetpolMultitenant, self).__init__(
                config, central_node, worker_nodes)
        test_config = config.get('netpol_multitenant', dict())
        ranges = [
            NsRange(
                start=range_args.get('start', 0),
                n_pods=range_args.get('n_pods', 5),
            ) for range_args in test_config.get('ranges', list())
        ]
        ranges.sort(key=lambda x: x.start, reverse=True)
        self.config = NsMultitenantCfg(
            n_namespaces=test_config.get('n_namespaces', 0),
            n_external_ips1=test_config.get('n_external_ips1', 3),
            n_external_ips2=test_config.get('n_external_ips2', 20),
            ranges=ranges,
            queries_per_second=test_config.get('queries_per_second', 20),
        )

    async def run(self, ovn, global_cfg):
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

        to-lport, ip.src == $AS_ns_i && outport == @PG_ns_i,
                  allow-related
        to-lport, ip.src == {ip1, ip2, ip3} && outport == @PG_ns_i,
                  allow-related
        to-lport, ip.src == {ip1, ..., ip20} && outport == @PG_ns_i,
                  allow-related
        """
        external_ips1 = [
            netaddr.IPAddress('42.42.42.1') + i
            for i in range(self.config.n_external_ips1)
        ]
        external_ips2 = [
            netaddr.IPAddress('43.43.43.1') + i
            for i in range(self.config.n_external_ips2)
        ]

        all_ns = []
        with Context('netpol_multitenant', self.config.n_namespaces,
                     test=self) as ctx:
            await ctx.qps_test(self.config.queries_per_second,
                               self.tester, ovn, external_ips1, external_ips2,
                               all_ns)

        if not global_cfg.cleanup:
            return
        with Context('netpol_multitenant_cleanup', brief_report=True) as ctx:
            for ns in all_ns:
                await ns.unprovision()

    async def tester(self, ovn, external_ips1, external_ips2, all_ns):
        # Get the number of pods from the "highest" range that
        # includes iteration.
        iter_num = get_current_iteration().num
        ranges = self.config.ranges
        n_ports = next((r.n_pods for r in ranges if iter_num >= r.start), 1)
        ns = await create_namespace(ovn, f'ns_netpol_multitenant_{iter_num}')
        for _ in range(n_ports):
            worker = ovn.select_worker_for_port()
            for p in await worker.provision_ports(ovn, 1):
                await ns.add_ports([p])
        await ns.default_deny()
        await ns.allow_within_namespace()
        await ns.check_enforcing_internal()
        await ns.allow_from_external(external_ips1)
        await ns.allow_from_external(external_ips2, include_ext_gw=True)
        await ns.check_enforcing_external()
        all_ns.append(ns)
