from cms.ovn_kubernetes.tests.netpol import NetPol


class NetpolLarge(NetPol):
    def __init__(self, config, central_node, worker_nodes, global_cfg):
        super().__init__('netpol_large', config, central_node, worker_nodes)

    def run(self, ovn, global_cfg):
        self.init(ovn, global_cfg)
        super().run(ovn, global_cfg)
