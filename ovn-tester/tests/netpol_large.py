from tests.netpol import NetPol


class NetpolLarge(NetPol):
    def __init__(self, config, central_node, worker_nodes):
        super(NetpolLarge, self).__init__(
                'netpol_large', config, central_node, worker_nodes)

    async def run(self, ovn, global_cfg):
        await self.init(ovn)
        await super(NetpolLarge, self).run(ovn, global_cfg)
