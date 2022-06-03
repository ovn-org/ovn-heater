from tests.netpol import NetPol


class NetpolSmall(NetPol):
    def __init__(self, config, central_node, worker_nodes, global_cfg):
        super(NetpolSmall, self).__init__(
                'netpol_small', config, central_node, worker_nodes)

    def run(self, ovn, global_cfg):
        self.init(ovn)
        super(NetpolSmall, self).run(ovn, global_cfg, True)
