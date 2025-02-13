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
    def __init__(self, backend_config_path, proxy_host=LISTENER_ADDRESS, proxy_port=LISTENER_PORT):
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


    # ---------------------------- Main Loop --------------------------------
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
        conn_context.address = addr
        conn_context.file_descriptor = conn_file_no
        self.fd_to_socket_context[conn_file_no] = conn_context
        logging.info(f"Created new connection from {addr}")


    def handle_read_event(self, file_no):
        """
        Handle incoming data from either a client or backend server
        """
        try:
            # 1) Get socket context and socket
            socket_context = self.fd_to_socket_context[file_no]
            sock = self.fd_to_socket[file_no]

            # 2) Read data into recv_buffer (if > BUF_SIZE, will be)
            data = sock.recv(BUF_SIZE)

            if not data:  # Connection closed by peer
                self.close_connection(file_no)
                return

            # Append new data to existing recv buffer
            socket_context.recv_buffer.extend(data)

            # Split buffer by potential message boundaries
            messages = self._find_message_boundaries(socket_context.recv_buffer)

            # Sanity check
            if not messages:
                logging.warning(f"No messages found, but there was data in the socket {data.decode("utf-8", errors="ignore")}")
                return

            # Process all messages
            for i, message in enumerate(messages):

                # First or only message may be a partial message
                if socket_context.socket_type == SocketType.CLIENT_TO_PROXY:
                    builder = socket_context.current_request if i == 0 else MessageBuilderHTTP()

                else:
                    builder = socket_context.current_response if i == 0 else MessageBuilderHTTP()

                # Try to parse the message
                bytes_consumed = builder.parse_data(message)
                
                if bytes_consumed > 0:
                    if builder.is_complete():
                        # Handle complete message for Client Request
                        if socket_context.socket_type == SocketType.CLIENT_TO_PROXY:
                            self._handle_complete_client_request(socket_context, builder)

                            if i == 0:  # Reset builder if it was the first message
                                socket_context.current_request = MessageBuilderHTTP()

                        # Handle complete message for Backend Response
                        else:
                            self._handle_complete_backend_response(socket_context, builder)
                            
                            if i == 0:  # Reset builder if it was the first message
                                socket_context.current_response = MessageBuilderHTTP()
                    else:
                        # Partial message - store in context
                        if i == len(messages) - 1:  # Last message
                            if socket_context.socket_type == SocketType.CLIENT_TO_PROXY:
                                socket_context.current_request = builder

                            else:
                                socket_context.current_response = builder
                            socket_context.recv_buffer = message[bytes_consumed:]

                        else:
                            # Unexpected partial message in middle - something's wrong
                            logging.error(f"Partial message in middle of sequence")
                            self.close_connection(file_no)
                            return
                else:
                    # Hopefully this never runs
                    logging.warning(f"builder.parse_data on {message.decode("utf-8", errors="ignore")} consumed 0 bytes")
                    
                    # Couldn't parse anything - store remaining data
                    if i == len(messages) - 1:  # Last message
                        socket_context.recv_buffer = message
                    else:
                        # Couldn't parse non-last message - something's wrong
                        logging.error(f"Could not parse message in sequence")
                        self.close_connection(file_no)
                        return

            # Update epoll if we have data to send (little efficiency bump, otherwise we run the same as SELECT)
            # NOTE: Other sockets will populate this socket's send_buffer with _handle_complete_backend_response() 
            #       and _handle_complete_client_request() (LOOK FOR SEND_BUFFER.EXTEND())
            if len(socket_context.send_buffer) > 0:
                self.epoll.modify(file_no, READ_WRITE)

        except (ConnectionError, socket.error) as e:
            logging.error(f"Error handling read event: {e}")
            self.close_connection(file_no)


    def handle_write_event(self, file_no):
        """
        Handle writing data to either a client or backend server
        """
        try:
            # Get socket context
            socket_context = self.fd_to_socket_context[file_no]
            sock = self.fd_to_socket[file_no]

            # Try to send as much data as possible
            # NOTE: Validation and preprocessing comes from message handling. A promotion to the send buffer is a 
            #       final promotion, meaning we can just send all we can
            if len(socket_context.send_buffer) > 0:
                sent = sock.send(socket_context.send_buffer)

                if sent > 0:
                    # Remove sent data from buffer
                    socket_context.send_buffer = socket_context.send_buffer[sent:]

                    # If buffer is empty, prepare next message if available
                    if len(socket_context.send_buffer) == 0:
                        if socket_context.socket_type == SocketType.CLIENT_TO_PROXY:
                            # For client, prepare next response
                            self._prepare_next_client_response(socket_context)
                        else:
                            # For backend, prepare next request
                            self._prepare_next_backend_request(socket_context)

                        # If still nothing to send, modify epoll to read-only
                        if len(socket_context.send_buffer) == 0:
                            self.epoll.modify(file_no, READ_ONLY)
                
                # Else: Keep the send_buffer partially full, to be handled by next WRITE event (non-blocking)

        except (ConnectionError, socket.error) as e:
            logging.error(f"Error handling write event: {e}")
            self.close_connection(file_no)


    # -------------------------- Helper Methods: Read ---------------------------------
    def _find_message_boundaries(self, buffer: bytearray) -> list[bytearray]:
        """
        Find HTTP message boundaries in the buffer
        Returns a list of bytearrays, where first and/or last elements might be incomplete
        """
        messages = []
        current_pos = 0
        buffer_len = len(buffer)
        
        while current_pos < buffer_len:
            # Look for start of a new HTTP message
            next_message_start = -1
            
            # Search for common HTTP method/response patterns
            for pattern in [b'GET ', b'POST ', b'PUT ', b'DELETE ', b'HEAD ', b'HTTP/']:
                pos = buffer.find(pattern, current_pos + 1)  # +1 to avoid checking current_pos
                if pos != -1:
                    if next_message_start == -1 or pos < next_message_start:
                        next_message_start = pos
            
            if next_message_start == -1:
                # No more message boundaries found
                messages.append(buffer[current_pos:])
                break
                
            # Add chunk from current_pos to start of next message
            current_chunk = buffer[current_pos:next_message_start]
            if current_chunk:  # Only add non-empty chunks
                messages.append(current_chunk)
            current_pos = next_message_start
        
        if not messages:
            messages.append(buffer)
            
        return messages


    def _handle_complete_client_request(self, client_context: SocketContext, request: MessageBuilderHTTP):
        """Process a complete client request"""
        # 1. Generate X-Request-ID if not present
        if not request.x_request_id:
            request.x_request_id = str(uuid.uuid4())
            request.headers['X-Request-ID'] = request.x_request_id

        request_id = request.x_request_id
        
        # 2. Store request ID to client mapping
        self.req_to_client[request_id] = client_context.socket.fileno()
        
        # 3. Add to client's request order and queue
        client_context.client_request_order.append(request_id)
        client_context.request_queue.append((request_id, request))
        
        # 4. Select backend server (round-robin)
        backend = self.backend_servers[self.backend_index]
        self.backend_index = (self.backend_index + 1) % self.num_backends

        # 5. Get or create backend connection
        backend_fd = self._get_or_create_backend(backend)
        if backend_fd:
            backend_context = self.fd_to_socket_context[backend_fd]
            # Add request to backend's queue and update its send buffer
            if len(backend_context.send_buffer) == 0:
                self._prepare_next_backend_request(backend_context)
            self.epoll.modify(backend_fd, READ_WRITE)


    def _handle_complete_backend_response(self, backend_context: SocketContext, response: MessageBuilderHTTP):
        """Process a complete response from a backend server"""
        # 1. Get request ID and client file descriptor
        response_id = response.x_request_id
        client_fd = self.req_to_client.get(response_id)
        
        if client_fd:
            # 2. Get client context
            client_context = self.fd_to_socket_context.get(client_fd)
            if client_context:
                # 3. Add response to client's queue
                client_context.response_queue.append((response_id, response))
                
                # 4. If client's send buffer is empty, prepare next response
                if len(client_context.send_buffer) == 0:
                    self._prepare_next_client_response(client_context)
                
                # 5. Update epoll to write response
                self.epoll.modify(client_fd, READ_WRITE)


    # -------------------------- Helper Methods: Response ---------------------------------
    def _prepare_next_client_response(self, client_context: SocketContext):
        """Prepare the next response to send to the client"""
        while client_context.response_queue and client_context.client_request_order:
            # Get the next expected request ID
            expected_id = client_context.client_request_order[0]
            
            # Look for matching response (HEAD OF LINE BLOCKING)
            for i, (resp_id, response) in enumerate(client_context.response_queue):
                if resp_id == expected_id:
                    # Found matching response, remove from queues
                    client_context.client_request_order.popleft()
                    client_context.response_queue.remove((resp_id, response))
                    
                    # Add to send buffer and clean up tracking (ONE OF TWO SEND_BUFFER.EXTENDS)
                    client_context.send_buffer.extend(response.build_full_message())
                    del self.req_to_client[resp_id]
                    return
            
            # If we didn't find the next expected response, we need to wait until we do to flush messages in order
            break
        

    def _prepare_next_backend_request(self, backend_context: SocketContext):
        """Prepare the next request to send to the backend"""
        if backend_context.request_queue:
            _, request = backend_context.request_queue.popleft()  # Pop a {x-req-id : MessageBuilderHTTP }
            backend_context.send_buffer.extend(request.build_full_message())  # (ONE OF TWO SEND_BUFFER.EXTENDS)


    def _get_or_create_backend(self, backend):
        """Get existing backend connection or create a new one"""
        backend_addr = f"{backend['ip']}:{backend['port']}"
        
        # Return existing connection if available
        if backend_addr in self.address_to_fd:
            return self.address_to_fd[backend_addr]
        
        try:
            # Create new connection
            backend_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            backend_sock.setblocking(False)
            backend_sock.connect_ex((backend['ip'], backend['port']))
            
            # Register with epoll
            backend_fd = backend_sock.fileno()
            self.fd_to_socket[backend_fd] = backend_sock
            self.epoll.register(backend_fd, READ_WRITE)
            
            # Create and register context
            backend_context = SocketContext(backend_sock)
            backend_context.socket_type = SocketType.PROXY_TO_BACKEND
            backend_context.current_request = MessageBuilderHTTP()
            backend_context.current_response = MessageBuilderHTTP()
            backend_context.address = backend_addr
            backend_context.file_descriptor = backend_fd
            self.fd_to_socket_context[backend_fd] = backend_context
            
            # Store mapping
            self.address_to_fd[backend_addr] = backend_fd
            
            return backend_fd
            
        except Exception as e:
            logging.error(f"Error creating backend connection: {e}")
            return None
        
    # -------------------------- Helper Methods: Proxy Server ---------------------------------
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

        # 1) Close all connection sockets
        for fd, sock in self.fd_to_socket.items():
            try:
                logging.info(f"Closing socket {fd}...")
                sock.close()
            except Exception as e:
                logging.error(f"Error closing socket {fd}: {e}")

        self.fd_to_socket.clear()  # Clear the dictionary after closing all sockets

        # 2) Close epoll instance
        if self.epoll:
            self.epoll.unregister(self.server_socket.fileno())  # untrack server socket from epoll
            self.epoll.close()

        # 3) Close server socket
        if self.server_socket:
            self.server_socket.close()
        
        logging.info("Server shut down.")
