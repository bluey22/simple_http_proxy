# proxy.py
import errno
import json
import socket
import select
import uuid
import logging
from collections import deque
from typing import Dict, Set, Optional
from dataclasses import dataclass, field

# Constants
LISTENER_ADDRESS = "127.0.0.1"
LISTENER_PORT = 9000
BUF_SIZE = 4096
MAX_HEADERS_SIZE = 8192
CONNECTION_QUEUE_LIMIT = 150

# Epoll flags
READ_ONLY = select.EPOLLIN | select.EPOLLPRI | select.EPOLLHUP | select.EPOLLERR
READ_WRITE = READ_ONLY | select.EPOLLOUT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [proxy] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

@dataclass
class HTTPMessage:
    """HTTP message container"""
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytearray = field(default_factory=bytearray)
    method: str = ""
    path: str = ""
    version: str = "HTTP/1.1"
    status_code: str = ""
    status_text: str = ""
    content_length: int = 0
    is_response: bool = False  # Otherwise is a request
    keep_alive: bool = True
    x_request_id: Optional[str] = None  # Added into header for routing (load-balancing)

    def build(self) -> bytes:
        """Convert this HTTPMessage to a byte message"""
        parts = []
        if self.is_response:
            parts.append(f"{self.version} {self.status_code} {self.status_text}\r\n")
        else:
            parts.append(f"{self.method} {self.path} {self.version}\r\n")
        
        for k, v in self.headers.items():
            parts.append(f"{k}: {v}\r\n")  # "\r\n" = (Carriage Return + Line Feed)
        
        parts.append("\r\n")
        message = "".join(parts).encode()
        
        if self.body:
            return message + self.body
        return message

@dataclass
class Connection:
    """Connection state manager"""
    socket: socket.socket
    addr: tuple  # (IP, Port)
    input_buffer: bytearray = field(default_factory=bytearray)   # Read buffer: received from the socket but not yet processed
    output_buffer: bytearray = field(default_factory=bytearray)  # Write buffer: stores outgoing raw bytes waiting to be sent to THIS socket connection
    current_message: Optional[HTTPMessage] = None                # The current HTTP message being processed (For partial/chunked HTTP requests)
    pending_requests: deque = field(default_factory=deque)       # Queue of HTTPMessage Requests
    pending_responses: deque = field(default_factory=deque)      # Queue of HTTPMessage Responses
    request_order: deque = field(default_factory=deque)          # Tracks order of requests for Head-Of-Line blocking in our pipelining
    headers_complete: bool = False  # Flag indicating if the current message has complete headers
    headers_size: int = 0           # Tracks the size of received headers and enforces limits
    body_received: int = 0          # Flag indicating if the current message has a complete body
    is_backend: bool = False        # Otherwise is aclient connection
    keep_alive: bool = True         # HTTP/1.1 "Persistent" Connection for multiple requests

class ProxyServer:
    def __init__(self, config_path: str):
        self.backend_servers = self._load_config(config_path)  # Load backend servers from servers.conf
        self.backend_index = 0                                 # Round-robin index for load balancing
       
        # Stores active connections (client and backend), mapped by file descriptor (fd)
        self.connections: Dict[int, Connection] = {}    

        # Stores available backend connections, with key="ip:port" : val=fd
        self.backend_pool: Dict[str, Set[int]] = {}  # See NOTE under _get_backend_connection to see how this pool is managed

        # Maps 'X-Request-ID' to client fds to route responses back
        self.request_map: Dict[str, int] = {}
        
        # Initialize server socket
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)    # creates a new TCP socket
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # Socket option: allows immediate re-use of the same port
        self.server.setblocking(False)  # Non-blocking socket
        
        # Initialize epoll
        self.epoll = select.epoll()
        
    def _load_config(self, path: str) -> list:
        "Load backend servers from a JSON config file"
        with open(path) as f:
            return json.load(f)["backend_servers"]

    def start(self):
        """Start the proxy server"""
        try:
            self.server.bind((LISTENER_ADDRESS, LISTENER_PORT))   # Bind front-end server socket to server address
            self.server.listen(CONNECTION_QUEUE_LIMIT)            # Allow up to N queued connections
            self.epoll.register(self.server.fileno(), READ_ONLY)  # Register front-end socket with epoll()
            
            logging.info(f"Proxy listening on {LISTENER_ADDRESS}:{LISTENER_PORT}")
            
            while True:
                events = self.epoll.poll(timeout=1)

                for fd, event in events:
                    if fd == self.server.fileno():
                        # Case 1: New client is connecting
                        self._accept_connection()

                    elif event & (select.EPOLLIN | select.EPOLLPRI):
                        # Case 2: Data from client/backend (new data or connection closed packet)
                        self._handle_read(fd)

                    elif event & select.EPOLLOUT:
                        # Case 3: Socket is ready to send data
                        self._handle_write(fd)
                    
                    if event & (select.EPOLLHUP | select.EPOLLERR):
                        # Case 4: error handling
                        self._close_connection(fd)
                        
        except KeyboardInterrupt:
            logging.info("Shutting down...")

        finally:
            self.cleanup()

    def _accept_connection(self):
        """Accept new client connection"""
        try:
            client_socket, addr = self.server.accept()
            client_socket.setblocking(False)
            fd = client_socket.fileno()
            
            # Initialize connection object
            self.connections[fd] = Connection(
                socket=client_socket,
                addr=addr
            )
            
            # Register the socket file descriptor
            self.epoll.register(fd, READ_ONLY)
            logging.debug(f"Accepted connection from {addr}")
            
        except socket.error as e:
            logging.error(f"Error accepting connection: {e}")

    def _handle_read(self, fd: int):
        """Handle read events"""
        conn = self.connections[fd]
        
        try:
            data = conn.socket.recv(BUF_SIZE)
            if not data:  # Connection closed
                self._close_connection(fd)
                return
            
            # Read as much as possible into our input/read buffer
            conn.input_buffer.extend(data)

            # Process input/read buffer
            self._process_input(fd)
            
        except socket.error as e:
            if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                self._close_connection(fd)

    def _process_input(self, fd: int):
        """Process input buffer"""
        conn = self.connections[fd]
        
        while conn.input_buffer:  # While input_buffer is not empty

            # Check 1: Does our current HTTP message have complete headers?
            if not conn.headers_complete:
                if not self._process_headers(conn):  # If not, process them and check again
                    break  # We need to wait for more data to complete our message
            
            if conn.current_message:  # If headers are complete, and we're currently still building an HTTP message
                remaining = conn.current_message.content_length - conn.body_received  # How many more bytes we need
                
                if remaining > 0:
                    # Find these bytes from the remaining input_buffer
                    body_data = conn.input_buffer[:remaining]
                    conn.current_message.body.extend(body_data)
                    conn.body_received += len(body_data)

                    # Input_buffer now is the rest of the message
                    conn.input_buffer = conn.input_buffer[len(body_data):]
                
                # We have a full and complete body (a complete message)
                if conn.body_received >= conn.current_message.content_length:
                    self._handle_complete_message(fd)  # Route/send this to the correct socket

                    # Reset our connection's current message
                    conn.headers_complete = False
                    conn.body_received = 0
                    conn.headers_size = 0
                    conn.current_message = None
                else:
                    break

    def _process_headers(self, conn: Connection) -> bool:
        """Process HTTP headers. Returns whether or not we have a complete set of headers for a message"""
        if b'\r\n\r\n' not in conn.input_buffer:
            conn.headers_size += len(conn.input_buffer)
            if conn.headers_size > MAX_HEADERS_SIZE:
                return False  # In either case, exceed header size or end not found, this is not a complete header
            return False  # Wait for more data
        
        # Extract headers and remaining buffer
        headers_data, conn.input_buffer = conn.input_buffer.split(b'\r\n\r\n', 1)
        lines = headers_data.split(b'\r\n')
        
        # Parse first line (Request or Response line)
        first_line = lines[0].decode('utf-8')
        parts = first_line.split()
        
        msg = HTTPMessage()
        
        if parts[0].startswith('HTTP/'):  # Dealing with a response
            msg.is_response = True
            msg.version = parts[0]
            msg.status_code = parts[1]
            msg.status_text = ' '.join(parts[2:])

        else:  # Dealing with a request
            msg.method = parts[0]
            msg.path = parts[1]
            msg.version = parts[2]
            
        # Parse rest of headers for key-value pairs
        for line in lines[1:]:
            if b':' in line:
                k, v = line.decode('utf-8').split(':', 1)
                k = k.strip()
                v = v.strip()
                msg.headers[k] = v
                
                # Check for meaningful headers: message size and routing
                if k.lower() == 'content-length':
                    msg.content_length = int(v)
                elif k == 'X-Request-ID':
                    msg.x_request_id = v
        
        # If we've got here, we've successfully parsed complete headers and this can be our start for our current message
        conn.current_message = msg
        conn.headers_complete = True
        return True

    def _handle_complete_message(self, fd: int):
        """Handle complete HTTP message"""
        # Retrieve the connection object with the fd, and the complete message
        conn = self.connections[fd]
        msg = conn.current_message
        
        # If this message does not have a X-Request-ID (i.e., a client HTTP Request)
        if not msg.x_request_id:
            # Generate a uuid for it
            msg.x_request_id = str(uuid.uuid4())
            msg.headers['X-Request-ID'] = msg.x_request_id  # Add it to HTTP headers
        
        # If this connection is from a client
        if not conn.is_backend:
            # Select the next backend server using round-robin load balancing - forward to backend
            backend = self.backend_servers[self.backend_index]
            self.backend_index = (self.backend_index + 1) % len(self.backend_servers)
            
            # Get existing connection to backend or make one
            backend_fd = self._get_backend_connection(backend)
            
            if backend_fd:
                # Add the HTTP request to the backend's output buffer for sending
                backend_conn = self.connections[backend_fd]
                backend_conn.output_buffer.extend(msg.build())

                # Associate this X-Request-ID with FD, the Client socket (which we passed in)
                self.request_map[msg.x_request_id] = fd
                conn.request_order.append(msg.x_request_id)  # Maintain this order for Head-of-line blocking

                # Mark this backend socket as writable, so the send buffer can be sent
                self.epoll.modify(backend_fd, READ_WRITE)

        else:
            # Backend response - forward to client (fd is our backend fd)
            client_fd = self.request_map.get(msg.x_request_id)  # Use the X-Request-ID to get the client

            if client_fd:
                # Get the client connection object
                client_conn = self.connections[client_fd]

                # Append this complete HTTPMessage to the sending queue
                client_conn.pending_responses.append(msg)
                self._prepare_client_response(client_fd)  # Prep the send buffer

    def _get_backend_connection(self, backend: dict) -> Optional[int]:
        """Get or create backend connection"""
        # NOTE: The proxy does not open one connection per request, but per active client that persists for the client
        #       - If a client sends multiple requests, it may reuse the same backend connection.
        #       - If multiple clients request the same backend, the proxy may open multiple backend connections if needed.
        #       - To avoid excessive socket creation, backend_pool stores reusable backend connections.
        addr = f"{backend['ip']}:{backend['port']}"  # Identify backend by address
        
        # Check existing pool if we already have an open connection to this backend
        if addr in self.backend_pool and self.backend_pool[addr]:
            return self.backend_pool[addr].pop()
        
        # Otherwise, create a new connection to this backend
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setblocking(False)

            # Attempt a non-blocking connection, rather than blocking until connection established or failed
            sock.connect_ex((backend['ip'], backend['port']))
            
            # Get the new fd from the socket
            fd = sock.fileno()
            
            # Store this new backend connection in `connections`
            self.connections[fd] = Connection(
                socket=sock,
                addr=(backend['ip'], backend['port']),
                is_backend=True
            )
            
            # Ensure the backend pool exists for this address
            if addr not in self.backend_pool:
                self.backend_pool[addr] = set()
            
            # Register and return backend fd
            self.epoll.register(fd, READ_WRITE)
            return fd
            
        except socket.error as e:
            logging.error(f"Backend connection error: {e}")
            return None

    def _prepare_client_response(self, fd: int):
        """Prepare next response for client"""
        # Get the connection object
        conn = self.connections[fd]
        
        # Head of line blocking! We want to send the clients their responses based on the order they requested
        while conn.pending_responses and conn.request_order:
            next_id = conn.request_order[0]
            
            # We look to match our X-Request-ID in our pending HTTPMessage responses
            for resp in conn.pending_responses:
                # If found, we can remove this from the order, remove it from the send_queue, and send out the convert bytes HTTPMessage
                if resp.x_request_id == next_id:
                    conn.request_order.popleft()
                    conn.pending_responses.remove(resp)
                    conn.output_buffer.extend(resp.build())
                    self.epoll.modify(fd, READ_WRITE)
                    return
                    
            break

    def _handle_write(self, fd: int):
        """Handle write events"""
        conn = self.connections[fd]
        
        # If items to send queued in output_buffer
        if conn.output_buffer:
            try:
                # Try and send as much as we can
                sent = conn.socket.send(conn.output_buffer)
                conn.output_buffer = conn.output_buffer[sent:]
                
                # If we sent out the entire buffer
                if not conn.output_buffer:

                    if conn.is_backend:
                        # Add the next request to this buffer (to backend)
                        self._prepare_next_request(fd)
                    else:
                        # Add the next response to this buffer (to the client_)
                        self._prepare_client_response(fd)
                        
                    if not conn.output_buffer:
                        self.epoll.modify(fd, READ_ONLY)
                        
            except socket.error as e:
                if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    self._close_connection(fd)

    def _prepare_next_request(self, fd: int):
        """Prepare next request for backend"""
        # Get the backend connection
        conn = self.connections[fd]
        
        # Retrieve the next request from this client (fd), and place the request to be forwarded in the send buffer
        if conn.pending_requests:
            req = conn.pending_requests.popleft()
            conn.output_buffer.extend(req.build())
            self.epoll.modify(fd, READ_WRITE)

    def _close_connection(self, fd: int):
        """Close connection and cleanup"""
        if fd in self.connections:
            conn = self.connections[fd]
            
            try:
                self.epoll.unregister(fd)
            except:
                pass
                
            try:
                conn.socket.close()
            except:
                pass
                
            # Cleanup connection state
            if conn.is_backend:
                addr = f"{conn.addr[0]}:{conn.addr[1]}"
                if addr in self.backend_pool:
                    self.backend_pool[addr].discard(fd)
            else:
                # Cleanup any pending requests
                for req_id in conn.request_order:
                    if req_id in self.request_map:
                        del self.request_map[req_id]
                        
            del self.connections[fd]  # Remove from active connections

    def cleanup(self):  
        """Cleanup server resources"""
        # Close all sockets
        for fd in list(self.connections.keys()):
            self._close_connection(fd)
        
        # Close self
        self.epoll.unregister(self.server.fileno())
        self.epoll.close()
        self.server.close()

if __name__ == "__main__":
    # Driver code
    proxy = ProxyServer("servers.conf")
    proxy.start()
