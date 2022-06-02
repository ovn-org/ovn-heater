from tests.netpol import NetPol


class NetpolLarge(NetPol):
    def __init__(self, config, central_node, worker_nodes, global_cfg):
        super(NetpolLarge, self).__init__(
            'netpol_large', config, central_node, worker_nodes
        )

    def run(self, ovn, global_cfg):
        self.init(ovn, global_cfg)
        super(NetpolLarge, self).run(ovn, global_cfg)
