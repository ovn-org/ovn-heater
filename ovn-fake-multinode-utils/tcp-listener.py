#!/usr/bin/env python3

import argparse
import selectors
import socket


def serve(address, ports):
    with selectors.DefaultSelector() as selector:
        for port in ports:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((address, port))
            listener.listen()
            selector.register(listener, selectors.EVENT_READ)

        while True:
            for key, _ in selector.select():
                connection, _ = key.fileobj.accept()
                connection.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='TCP connection listener')
    parser.add_argument('ports', nargs='+', type=int)
    parser.add_argument(
        '-a', '--address', default='0.0.0.0', help='Address to listen on'
    )
    args = parser.parse_args()

    serve(args.address, args.ports)
