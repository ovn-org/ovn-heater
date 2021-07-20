from tests.netpol import NetPol


class NetpolLarge(NetPol):
    def __init__(self, config):
        super(NetpolLarge, self).__init__('netpol_large', config)

    def run(self, ovn, global_cfg):
        self.init(ovn)
        super(NetpolLarge, self).run(ovn, global_cfg)
