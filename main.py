#!/usr/bin/env python3
from proxy import ProxyServer

# -------------------------- Driver Code ---------------------------------
if __name__ == '__main__':
    server = ProxyServer()
    server.setup_server()
    server.run()