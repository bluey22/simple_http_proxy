# proxy.py
import sys
import json
import socket
import select
import uuid
import logging
from typing import Dict
from message_builder_http import MessageBuilderHTTP
from connection import SocketContext
from utils import SocketType

# --------------------------------- Constants --------------------------------------
LISTENER_ADDRESS = "127.0.0.1"  # or '0.0.0.0', available on all network interfaces
LISTENER_PORT = 9000
BUF_SIZE = 4096

BITERRS = [25, 24, 8, 16, 9, 17, 26, 10, 18]
BUF_SIZE = 4096

READ_ONLY = select.EPOLLIN | select.EPOLLPRI | select.EPOLLHUP | select.EPOLLERR
READ_WRITE = READ_ONLY | select.EPOLLOUT
# EPOLLIN: Triggered when data is available to read.
# EPOLLPRI: High-priority data is available to read.
# EPOLLHUP: Remote socket hang up (closed by client).
# EPOLLERR: Error occurred.
# EPOLLOUT: Triggered when a socket is ready to send data without blocking

# ------------------------------- Service Logging ------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [proxy] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

# -------------------------------- Server Class --------------------------------------
class ProxyServer:
    """
    An HTTP/1.1 Proxy Server that support pipelining and non-blocking I/O with epoll
    """
    def __init__(
        self, backend_config_path, proxy_host=LISTENER_ADDRESS, proxy_port=LISTENER_PORT
    ):
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port

        # 1) Load backend servers from JSON Config
        self.backend_servers = self.load_backend_servers(backend_config_path)
        self.num_backends = len(self.backend_servers)
        self.backend_index = 0  # for round-robin

        # 2) Main Socket Instance and Epoll (these will be set up in setup_server())
        self.server_socket: socket.socket = None
        self.epoll: select.epoll = None

        # 3) Data Stores
        # Maps File descriptor to Socket, SocketContext
        self.fd_to_socket: Dict[int, socket.socket] = {}    # { key=file_no (any socket) : val=socket.socket}
        self.fd_to_socket_context: Dict[int, SocketContext] = {}  # { key=file_no (any socket) : val=SocketHTTP object}
        
        # Maps x-request-id to client-fd
        self.req_to_client: Dict[str, int] = {}  # { key=X-Request-ID : val=file_no (client)}

        # Maps address to fd: Map round robin address to exising connection from proxy
        self.address_to_fd: Dict[str, int] = {} # { key=IP-Address : val=file_no (backend)}

    def setup_server(self):
        """
        Initailizes the server listener socket and epoll for non-blocking IO
        """
        try:
            # 1. Create socket listening (frontend)
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # creates a new TCP/IP socke
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # socket option: allows immediate reuse of the same port
            self.server_socket.bind((self.proxy_host, self.proxy_port))
            self.server_socket.listen(5)  # allow up to 5 queued connections
            self.server_socket.setblocking(False)  # non-blocking (accept(), recv(), and send() return immediately)
            self.fd_to_socket[self.server_socket.fileno()] = (self.server_socket)  # register file descriptor to socket object
            # Note: No register with self.fd_to_socket_context since this is purely the frontend socket for the proxy

            # 2. Set up epoll for non-blocking IO
            self.epoll = select.epoll()
            self.epoll.register(self.server_socket.fileno(), READ_ONLY)  # register file descriptor as read only
            logging.info(f"Proxy Server started on {self.proxy_host}:{self.proxy_port}")
        except socket.error as err:
            logging.error(f"Socket creation failed with error: {err}")
            exit(1)

    def run(self):
        """Main loop for the ProxyServer and listens for events with epoll"""
        try:
            while True:
                events = self.epoll.poll(timeout=1)

                for file_no, event in events:
                    if file_no == self.server_socket.fileno():
                        # Case 1: New client is connecting
                        self.handle_new_connection()

                    elif event & (select.EPOLLIN | select.EPOLLPRI):
                        # Case 2: Data from client/backend (new data or connection closed packet)
                        self.handle_read_event(file_no)

                    elif event & select.EPOLLOUT:
                        # Case 3: Socket is ready to send data
                        self.handle_write_event(file_no)

                    elif event in BITERRS:
                        # Case 4: error handling
                        self.close_connection(file_no)
        finally:
            self.shutdown()

    # ---------------------------- Event Methods --------------------------------
    def handle_new_connection(self):
        """
        Accept and create a new connection socket
        """
        conn, addr = self.server_socket.accept()
        conn.setblocking(False)
        conn_file_no = conn.fileno()

        # Register the socket file descriptor
        self.fd_to_socket[conn_file_no] = conn
        self.epoll.register(conn_file_no, READ_ONLY)

        # Register the context object for this socket (SocketHTTP)
        conn_context = SocketContext(conn)
        conn_context.socket_type = SocketType.CLIENT_TO_PROXY
        conn_context.current_request = MessageBuilderHTTP()
        conn_context.current_response = MessageBuilderHTTP()
        self.fd_to_socket_context[conn_file_no] = conn_context
        print(f"New connection from {addr}")

    def handle_read_event(self, file_no):
        #TODO
        pass

    def handle_write_event(self, file_no):
        #TODO
        pass

    # -------------------------- Helper Methods ---------------------------------
    def load_backend_servers(self, config_path):
        """Load backend servers list from a JSON config file"""
        with open(config_path, "r") as f:
            data = json.load(f)
        return data["backend_servers"]

    def close_connection(self, file_no):
        """Closes connection socket and deletes relevant data"""
        if file_no in self.fd_to_socket:
            self.epoll.unregister(file_no)  # untrack from epoll
            self.fd_to_socket[file_no].close()  # close socket
            del (self.fd_to_socket[file_no])
            if file_no in self.fd_to_socket_context:
                del (self.fd_to_socket_context[file_no])
            print(f"Closed connection {file_no}")

    def shutdown(self):
        """Shuts down the ProxyServer instance"""
        print("Shutting down server...")
        if self.epoll:
            self.epoll.unregister(
                self.server_socket.fileno()
            )  # untrack server socket from epoll
            self.epoll.close()  # close epoll instance
        if self.server_socket:
            self.server_socket.close()  # close server socket
        print("Server shut down.")
