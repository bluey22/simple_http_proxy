#!/usr/bin/env python3
from proxy import ProxyServer

# -------------------------- Driver Code ---------------------------------
if __name__ == '__main__':
    server = ProxyServer(backend_config_path="servers.conf")
    try:
        server.setup_server()
        server.run()
    except Exception as e:
        print(f"Error occurred: {e}")