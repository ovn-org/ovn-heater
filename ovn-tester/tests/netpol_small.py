from tests.netpol import NetPol


class NetpolSmall(NetPol):
    def __init__(self, config, central_node, worker_nodes, global_cfg):
        super().__init__('netpol_small', config, central_node, worker_nodes)

    def run(self, ovn, global_cfg):
        self.init(ovn, global_cfg)
        super().run(ovn, global_cfg, True)
