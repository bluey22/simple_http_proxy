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
    """Efficient HTTP message container"""
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytearray = field(default_factory=bytearray)
    method: str = ""
    path: str = ""
    version: str = "HTTP/1.1"
    status_code: str = ""
    status_text: str = ""
    content_length: int = 0
    is_response: bool = False
    keep_alive: bool = True
    x_request_id: Optional[str] = None

    def build(self) -> bytes:
        """Build HTTP message efficiently"""
        parts = []
        if self.is_response:
            parts.append(f"{self.version} {self.status_code} {self.status_text}\r\n")
        else:
            parts.append(f"{self.method} {self.path} {self.version}\r\n")
        
        for k, v in self.headers.items():
            parts.append(f"{k}: {v}\r\n")
        
        parts.append("\r\n")
        message = "".join(parts).encode()
        
        if self.body:
            return message + self.body
        return message

@dataclass
class Connection:
    """Connection state manager"""
    socket: socket.socket
    addr: tuple
    input_buffer: bytearray = field(default_factory=bytearray)
    output_buffer: bytearray = field(default_factory=bytearray)
    current_message: Optional[HTTPMessage] = None
    pending_requests: deque = field(default_factory=deque)
    pending_responses: deque = field(default_factory=deque)
    request_order: deque = field(default_factory=deque)
    headers_complete: bool = False
    headers_size: int = 0
    body_received: int = 0
    is_backend: bool = False
    keep_alive: bool = True

class ProxyServer:
    def __init__(self, config_path: str):
        self.backend_servers = self._load_config(config_path)
        self.backend_index = 0
        self.connections: Dict[int, Connection] = {}
        self.backend_pool: Dict[str, Set[int]] = {}
        self.request_map: Dict[str, int] = {}
        
        # Initialize server socket
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.setblocking(False)
        
        # Initialize epoll
        self.epoll = select.epoll()
        
    def _load_config(self, path: str) -> list:
        with open(path) as f:
            return json.load(f)["backend_servers"]

    def start(self):
        """Start the proxy server"""
        try:
            self.server.bind((LISTENER_ADDRESS, LISTENER_PORT))
            self.server.listen(CONNECTION_QUEUE_LIMIT)
            self.epoll.register(self.server.fileno(), READ_ONLY)
            
            logging.info(f"Proxy listening on {LISTENER_ADDRESS}:{LISTENER_PORT}")
            
            while True:
                events = self.epoll.poll(1)
                for fd, event in events:
                    if fd == self.server.fileno():
                        self._accept_connection()
                    elif event & (select.EPOLLIN | select.EPOLLPRI):
                        self._handle_read(fd)
                    elif event & select.EPOLLOUT:
                        self._handle_write(fd)
                    
                    if event & (select.EPOLLHUP | select.EPOLLERR):
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
            
            # Initialize connection state
            self.connections[fd] = Connection(
                socket=client_socket,
                addr=addr
            )
            
            self.epoll.register(fd, READ_ONLY)
            logging.debug(f"Accepted connection from {addr}")
            
        except socket.error as e:
            logging.error(f"Error accepting connection: {e}")

    def _get_backend_connection(self, backend: dict) -> Optional[int]:
        """Get or create backend connection"""
        addr = f"{backend['ip']}:{backend['port']}"
        
        # Check existing pool
        if addr in self.backend_pool and self.backend_pool[addr]:
            return self.backend_pool[addr].pop()
            
        try:
            # Create new connection
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setblocking(False)
            sock.connect_ex((backend['ip'], backend['port']))
            
            fd = sock.fileno()
            self.connections[fd] = Connection(
                socket=sock,
                addr=(backend['ip'], backend['port']),
                is_backend=True
            )
            
            if addr not in self.backend_pool:
                self.backend_pool[addr] = set()
            
            self.epoll.register(fd, READ_WRITE)
            return fd
            
        except socket.error as e:
            logging.error(f"Backend connection error: {e}")
            return None

    def _handle_read(self, fd: int):
        """Handle read events"""
        conn = self.connections[fd]
        
        try:
            data = conn.socket.recv(BUF_SIZE)
            if not data:
                self._close_connection(fd)
                return
                
            conn.input_buffer.extend(data)
            self._process_input(fd)
            
        except socket.error as e:
            if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                self._close_connection(fd)

    def _process_input(self, fd: int):
        """Process input buffer"""
        conn = self.connections[fd]
        
        while conn.input_buffer:
            if not conn.headers_complete:
                if not self._process_headers(conn):
                    break
            
            if conn.current_message:
                remaining = conn.current_message.content_length - conn.body_received
                if remaining > 0:
                    body_data = conn.input_buffer[:remaining]
                    conn.current_message.body.extend(body_data)
                    conn.body_received += len(body_data)
                    conn.input_buffer = conn.input_buffer[len(body_data):]
                
                if conn.body_received >= conn.current_message.content_length:
                    self._handle_complete_message(fd)
                    conn.headers_complete = False
                    conn.body_received = 0
                    conn.headers_size = 0
                    conn.current_message = None
                else:
                    break

    def _process_headers(self, conn: Connection) -> bool:
        """Process HTTP headers"""
        if b'\r\n\r\n' not in conn.input_buffer:
            conn.headers_size += len(conn.input_buffer)
            if conn.headers_size > MAX_HEADERS_SIZE:
                return False
            return False
            
        headers_data, conn.input_buffer = conn.input_buffer.split(b'\r\n\r\n', 1)
        lines = headers_data.split(b'\r\n')
        
        # Parse status/request line
        first_line = lines[0].decode('utf-8')
        parts = first_line.split()
        
        msg = HTTPMessage()
        
        if parts[0].startswith('HTTP/'):
            msg.is_response = True
            msg.version = parts[0]
            msg.status_code = parts[1]
            msg.status_text = ' '.join(parts[2:])
        else:
            msg.method = parts[0]
            msg.path = parts[1]
            msg.version = parts[2]
            
        # Parse headers
        for line in lines[1:]:
            if b':' in line:
                k, v = line.decode('utf-8').split(':', 1)
                k = k.strip()
                v = v.strip()
                msg.headers[k] = v
                
                if k.lower() == 'content-length':
                    msg.content_length = int(v)
                elif k == 'X-Request-ID':
                    msg.x_request_id = v
                    
        conn.current_message = msg
        conn.headers_complete = True
        return True

    def _handle_complete_message(self, fd: int):
        """Handle complete HTTP message"""
        conn = self.connections[fd]
        msg = conn.current_message
        
        if not msg.x_request_id:
            msg.x_request_id = str(uuid.uuid4())
            msg.headers['X-Request-ID'] = msg.x_request_id
            
        if not conn.is_backend:
            # Client request - forward to backend
            backend = self.backend_servers[self.backend_index]
            self.backend_index = (self.backend_index + 1) % len(self.backend_servers)
            
            backend_fd = self._get_backend_connection(backend)
            if backend_fd:
                backend_conn = self.connections[backend_fd]
                backend_conn.output_buffer.extend(msg.build())
                self.request_map[msg.x_request_id] = fd
                conn.request_order.append(msg.x_request_id)
                self.epoll.modify(backend_fd, READ_WRITE)
        else:
            # Backend response - forward to client
            client_fd = self.request_map.get(msg.x_request_id)
            if client_fd:
                client_conn = self.connections[client_fd]
                client_conn.pending_responses.append(msg)
                self._prepare_client_response(client_fd)

    def _prepare_client_response(self, fd: int):
        """Prepare next response for client"""
        conn = self.connections[fd]
        
        while conn.pending_responses and conn.request_order:
            next_id = conn.request_order[0]
            
            for resp in conn.pending_responses:
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
        
        if conn.output_buffer:
            try:
                sent = conn.socket.send(conn.output_buffer)
                conn.output_buffer = conn.output_buffer[sent:]
                
                if not conn.output_buffer:
                    if conn.is_backend:
                        self._prepare_next_request(fd)
                    else:
                        self._prepare_client_response(fd)
                        
                    if not conn.output_buffer:
                        self.epoll.modify(fd, READ_ONLY)
                        
            except socket.error as e:
                if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    self._close_connection(fd)

    def _prepare_next_request(self, fd: int):
        """Prepare next request for backend"""
        conn = self.connections[fd]
        
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
                        
            del self.connections[fd]

    def cleanup(self):
        """Cleanup server resources"""
        for fd in list(self.connections.keys()):
            self._close_connection(fd)
            
        self.epoll.unregister(self.server.fileno())
        self.epoll.close()
        self.server.close()

if __name__ == "__main__":
    proxy = ProxyServer("servers.conf")
    proxy.start()
