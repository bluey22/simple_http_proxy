# proxy.py
import sys
import json
import socket
import select
import uuid
import logging
from typing import Dict, Optional
from collections import deque as queue
from message_builder_http import MessageBuilderHTTP

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
        self.backend_servers = self.load_servers_config(backend_config_path)
        self.num_backends = len(self.backend_servers)
        self.backend_index = 0  # for round-robin

        # 2) Main Socket Instance and Epoll (these will be set up in setup_server())
        self.server_socket: socket.socket = None
        self.epoll: select.epoll = None

        # ----------------------------------------------------------------
        # Data Stores: Connections, Buffers, and Queues
        # ----------------------------------------------------------------

        # Connections: File descriptor to Master Map, Contains All Sockets
        self.fd_to_socket: Dict[int, socket.socket] = (
            {}
        )  # { key=file_no (any socket) : val=socket object}

        # Connections: Maps x-request-id to client-fd
        self.req_to_client: Dict[str, int] = (
            {}
        )  # { key=X-Request-ID : val=file_no (client)}

        # Connections: File descriptor to File descriptor
        self.client_to_backend: Dict[int, int] = (
            {}
        )  # { key=file_no (client) : val=file_no (backend_server)}

        self.backend_to_client: Dict[int, int] = (
            {}
        )  # { key=file_no (backend_server) : val=file_no (client)}

        # Buffers: Client to Proxy, Proxy to Backend
        #     client_buffers[client_fd]["read_buffer"][x_req_id] = b'full request'
        #     backend_buffers[client_fd]["write_buffer"][x_req_id] = b'data to send to proxy'
        self.client_buffers: Dict[int, Dict[str, Dict[str, bytearray]]] = (
            {}
        )  # { key=file_no : val={ "read_buffer" : { x-request-id: b'' }, "write_buffer" : { x-request-id: b'' } } }
        
        self.backend_buffers: Dict[int, Dict[str, Dict[str, bytearray]]] = (
            {}
        )  # { key=file_no : val={ "read_buffer" : { x-request-id: b'' }, "write_buffer" : { x-request-id: b'' } } }

        # Buffers: Ephemeral states Each fd may contain a "MessageBuilderHTTP" on a partial parse
        self.parse_state_request: Dict[int, MessageBuilderHTTP] = {}  # for clients
        self.parse_state_response: Dict[int, MessageBuilderHTTP] = {}  # for backends

        # Queues: Per-client queue of x-request-ids (for pipelining order)
        self.conn_to_http_requests_order: Dict[int, queue[str]] = (
            {}
        )  # { key=file_no : val=queue(x-request-ids) (client)}

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

        self.fd_to_socket[conn_file_no] = conn
        self.epoll.register(conn_file_no, READ_ONLY)

        # Initialize an empty ephemeral parse state for the socket (buffer)
        self.parse_state_request[conn_file_no] = MessageBuilderHTTP()

        # Initialize read and write buffers for the socket
        self.client_buffers[conn_file_no] = {
                "read_buffer": {},
                "write_buffer": {}
            }
        
        self.connections[conn.fileno()] = conn
        self.responses[conn.fileno()] = b""
        print(f"New connection from {addr}")

    def receive_data(self, file_no):
        """
        Check file descriptor for data and store it. Close connection if data is empty
        """
        try:
            recv_data = self.connections[file_no].recv(BUF_SIZE)
            if not recv_data:
                # Will we prematurely close the connection in receive_data()? A: No, because we got here due to an epoll event trigger
                self.close_connection(file_no)
                return
            self.responses[file_no] += recv_data  # store the received data in a buffer
            self.epoll.modify(
                file_no, READ_WRITE
            )  # immediately prepare socket for an EPOLLOUT event to flush our send data back (ECHO SERVER ARTIFACT - TODO:  Wait to ensure we receive the end of the HTTP REQ!)

        except (ConnectionResetError, BrokenPipeError) as e:
            print(f"Error in receive_data for fd={file_no}: {e}")
            self.close_connection(file_no)

    def send_data(self, file_no):
        """
        Write data in the file descriptor to the Client
        If the buffer to store data to send to the client is empty,
        modify file descriptor to only listen to new data
        """
        try:
            bytes_written = self.connections[file_no].send(
                self.responses[file_no]
            )  # retrieve socket for file_no, and send all the response data stored (ECHO SERVER ARTIFACT - TODO:  Wait to ensure we receive the end of the HTTP REQ!)

            # B/c send does not gurantee a full send: we wrote as much as we could
            # (OS moves data into the send buffer to then send when available)
            # We shorten our response data by removing the front bytes that were sent
            self.responses[file_no] = self.responses[file_no][bytes_written:]

            # NOTE: EPOLLOUT remains enabled due to us using the level-triggered, so we can retry this operation again
            # NOTE: In Edge-Triggered, we have to read and write completely, this is why we add a while true and check for BlockingIOError
            #           Because we need a way not to block if there's no data, but also can't say data = b'' because that would be a fin packet
            #               to close the connection (which we don't want)

            if (
                len(self.responses[file_no]) == 0
            ):  # if we wrote all the data, we can turn the socket back to read only
                self.epoll.modify(file_no, READ_ONLY)

        except (
            ConnectionResetError,
            BrokenPipeError,
        ) as e:  # client has already forcefully closed the connection
            print(f"Error in send_data for fd={file_no}: {e}")
            self.close_connection(file_no)

    # -------------------------- Helper Methods ---------------------------------
    def load_servers_config(self, config_path):
        """Load backend servers list from a JSON config file"""
        with open(config_path, "r") as f:
            data = json.load(f)
        return data["backend_servers"]

    def close_connection(self, file_no):
        """Closes connection socket and deletes relevant data"""
        if file_no in self.connections:
            self.epoll.unregister(file_no)  # untrack from epoll
            self.connections[file_no].close()  # close socket
            del (
                self.connections[file_no],
                self.responses[file_no],
            )  # delete from tracking dictionaries
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

    def load_backend_servers(self, config_file_path):
        # TODO:
        pass
