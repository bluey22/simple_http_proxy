# TODO DELETE: A NON-PIPELINED PROXY Demo

import socket
import select
import queue
import time
import re
import json
import sys
import argparse
from collections import defaultdict
from urllib.parse import urlparse
import logging
import signal
import uuid

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
 
class Backend:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.active = True
        self.last_checked = time.time()
        self.pending_requests = 0
        self.total_requests = 0
        self.failed_attempts = 0
        self.last_response_time = 0
        self.avg_response_time = 0
        self.max_failures = 10
        self.recovery_timeout = 1  # seconds
        
    def __str__(self):
        return f"Host={self.host} : Port={self.port}"
        
    def mark_request_complete(self, response_time):
        """Update statistics when a request completes"""
        self.pending_requests = max(0, self.pending_requests - 1)
        self.last_response_time = response_time
        # Update moving average of response time
        if self.avg_response_time == 0:
            self.avg_response_time = response_time
        else:
            self.avg_response_time = 0.8 * self.avg_response_time + 0.2 * response_time
            
    def mark_failure(self):
        """Record a failed request"""
        self.failed_attempts += 1
        if self.failed_attempts >= self.max_failures:
            self.active = False
            self.last_checked = time.time()
            logger.warning(f"Backend {self} marked as inactive after {self.failed_attempts} failures")
            
    def mark_success(self):
        """Record a successful request"""
        self.failed_attempts = 0
        if not self.active:
            self.active = True
            logger.info(f"Backend {self} is now active again")
            
    def should_retry(self):
        """Check if we should attempt to recover this backend"""
        if not self.active and time.time() - self.last_checked > self.recovery_timeout:
            return True
        return False
        
    def health_check(self):
        """Perform a health check on this backend"""
        if not self.active and not self.should_retry():
            return False
            
        try:
            start_time = time.time()
            with socket.create_connection((self.host, self.port), timeout=2) as sock:
                # Send a basic HEAD request
                sock.send(b"HEAD / HTTP/1.0\r\n\r\n")
                response = sock.recv(1024)
                if response:
                    self.mark_success()
                    self.last_response_time = time.time() - start_time
                    return True
        except (socket.timeout, ConnectionRefusedError, OSError) as e:
            logger.error(f"Health check failed for {self}: {e}")
            self.mark_failure()
            return False
        finally:
            self.last_checked = time.time()

class Connection:
    def __init__(self, socket, address):
        self.socket = socket
        self.address = address
        self.recv_buffer = b''
        self.send_buffer = b''
        self.request_queue = queue.Queue()
        self.response_queue = queue.Queue()
        self.current_request = None
        self.backend_socket = None
        self.backend_address = None
        self.expect_close = False
        self.retry_count = 0
        self.request_id = None

class HTTPRequest:
    def __init__(self, raw_data):
        self.raw = raw_data
        self.method = None
        self.path = None
        self.version = None
        self.headers = {}
        self.keep_alive = True
        self.complete = False
        self.content_length = 0
        self.body = b''
        self.parse()
        
    def parse(self):
        try:
            if b'\r\n\r\n' not in self.raw:
                return False
            headers, self.body = self.raw.split(b'\r\n\r\n', 1)
            request_lines = headers.split(b'\r\n')

            # Parse request line
            request_line = request_lines[0].decode('utf-8')
            self.method, self.path, self.version = request_line.split()

            # Parse headers
            for line in request_lines[1:]:
                if b':' in line:
                    key, value = line.decode('utf-8').split(':', 1)
                    key = key.strip().lower()
                    value = value.strip()
                    self.headers[key] = value

            if 'content-length' in self.headers:
                self.content_length = int(self.headers['content-length'])
            if 'connection' in self.headers:
                self.keep_alive = self.headers['connection'].lower() != 'close'

            # Insert X-Request-ID header
            self.headers['x-request-id'] = str(uuid.uuid4())

            # Reconstruct the raw request with the new header
            self.raw = f"{self.method} {self.path} {self.version}\r\n".encode('utf-8')
            for key, value in self.headers.items():
                self.raw += f"{key}: {value}\r\n".encode('utf-8')
            self.raw += b'\r\n' + self.body

            # Check if request is complete
            if self.content_length > 0:
                self.complete = len(self.body) >= self.content_length
            else:
                self.complete = True
            return self.complete
        except Exception as e:
            logger.error(f"Error parsing request: {e}")
            return False

class LoadBalancer:
    def __init__(self, backends):
        self.backends = {backend: Backend(*backend) for backend in backends}
        self.total_requests = 0
        self.current_backend_index = 0

    def get_next_backend(self):
        available_backends = [b for b in self.backends.values() if b.active or b.should_retry()]
        if not available_backends:
            raise Exception("No available backends")

        # Round-robin selection
        selected = available_backends[self.current_backend_index % len(available_backends) - 1]
        self.current_backend_index += 1

        # Perform health check if needed
        if not selected.active:
            if not selected.health_check():
                # Try again with only active backends
                available_backends = [b for b in self.backends.values() if b.active]
                if not available_backends:
                    raise Exception("No available backends")
                selected = available_backends[self.current_backend_index % len(available_backends)]
                self.current_backend_index += 1

        selected.pending_requests += 1
        selected.total_requests += 1
        return selected

class Proxy:
    def __init__(self, host, port, backends):
        self.host = host
        self.port = port
        self.load_balancer = LoadBalancer(backends)

        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 262144)
        self.server_socket.bind((host, port))
        self.server_socket.listen(2048) ### CHANGE TO HIGHER???
        self.server_socket.setblocking(False)

        self.connections = {}
        self.request_id_to_client = {}
        self.backend_to_client = {}
        self.epoll = select.epoll()
        self.epoll.register(self.server_socket.fileno(), select.EPOLLIN)

    def handle_client_data(self, client_fd):
        connection = self.connections.get(client_fd)
        if not connection:
            return

        try:
            data = connection.socket.recv(8192)
            if not data:
                ### STORE DATA IN RELATION TO REQUEST ID AND THEN GET THE DATA
                self.close_connection(client_fd)
                logger.info("unexpected closing")
                return
            connection.recv_buffer += data

            # Try to parse complete requests
            while connection.recv_buffer:
                if not connection.current_request:
                    connection.current_request = HTTPRequest(connection.recv_buffer)
                    connection.request_id = connection.current_request.headers.get('x-request-id')

                if connection.current_request.complete:
                    # Queue the complete request
                    connection.request_queue.put(connection.current_request)

                    # Remove processed request from buffer
                    consumed = len(connection.current_request.raw)
                    connection.recv_buffer = connection.recv_buffer[consumed:]
                    connection.current_request = None
                    self.request_id_to_client[connection.request_id] = client_fd

                    # Process queued requests
                    self.process_request_queue(client_fd)
                else:
                    break
        except OSError as e:
            logger.error(f"Error handling client data: {e}")
            # self.close_connection(client_fd)

    def process_request_queue(self, client_fd):
        connection = self.connections[client_fd]

        while not connection.request_queue.empty():
            request = connection.request_queue.get()

            # Create backend connection if needed
            if not connection.backend_socket:
                backend = self.load_balancer.get_next_backend()
                backend_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                backend_socket.setblocking(False)

                backend_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
                backend_socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 262144)

                try:
                    backend_socket.connect((backend.host, backend.port))
                except BlockingIOError:
                    pass
                connection.backend_socket = backend_socket
                connection.backend_address = (backend.host, backend.port)
                backend_fd = backend_socket.fileno()

                self.connections[backend_fd] = connection
                self.backend_to_client[backend_fd] = client_fd
                self.epoll.register(backend_fd, select.EPOLLIN | select.EPOLLOUT)

            # Queue the request for sending
            connection.send_buffer += request.raw

            # Update connection state
            if not request.keep_alive:
                connection.expect_close = True
                
    def handle_backend_data(self, backend_fd):
        # client_fd = self.backend_to_client.get(backend_fd)
        connection = self.connections.get(backend_fd)
        client_fd = self.request_id_to_client.get(connection.request_id)
        if not client_fd:
            return

        connection = self.connections.get(client_fd)
        if not connection:
            return

        try:
            data = connection.backend_socket.recv(8192)
            if not data:
                if connection.expect_close:
                    self.close_connection(client_fd)
                return
            # Send response back to client
            total_sent = 0
            while total_sent < len(data):
                try:
                    sent = connection.socket.send(data[total_sent:])
                    if sent == 0:
                        self.close_connection(client_fd)
                        return
                    total_sent += sent
                except BlockingIOError:
                    # Wait for the socket to be ready
                    continue
                except OSError as e:
                    logger.error(f"Error sending to client: {e}")
                    self.close_connection(client_fd)
                    return

            # Reset retry count on success
            connection.retry_count = 0

            # Update backend stats
            if backend_fd in self.load_balancer.backends:
                backend = self.load_balancer.backends[backend_fd]
                backend.pending_requests = max(0, backend.pending_requests - 1)
        except BlockingIOError:
            # Resource temporarily unavailable, retry later
            return
        except OSError as e:
            logger.error(f"Error handling backend data: {e}")
            self.retry(client_fd, self.handle_backend_data, backend_fd)

    def retry(self, client_fd, func, *args):
        connection = self.connections.get(client_fd)
        if not connection:
            return

        connection.retry_count = connection.retry_count + 1 if hasattr(connection, 'retry_count') else 1
        if connection.retry_count <= 10:  # Retry up to 10 times
            logger.info(f"Retrying for client {client_fd}, attempt {connection.retry_count}")
            time.sleep(1)  # Delay before retrying
            func(*args)
        else:
            logger.error(f"Max retries reached for client {client_fd}, closing connection")
            self.close_connection(client_fd)

    def run(self):
        try:
            while True:
                events = self.epoll.poll(1)
                for fd, event in events:
                    if fd == self.server_socket.fileno():
                        client_socket, address = self.server_socket.accept()
                        client_socket.setblocking(False)
                        client_fd = client_socket.fileno()

                        self.connections[client_fd] = Connection(client_socket, address)
                        self.epoll.register(client_fd, select.EPOLLIN)

                        ### NO RESPONSES LIST TO KEEP TRACK OF WITH CLIENT ID
                    elif event & select.EPOLLIN:
                        if fd in self.connections:
                            if fd in self.backend_to_client:
                                self.handle_backend_data(fd)
                            else:
                                self.handle_client_data(fd)

                    elif event & select.EPOLLOUT:
                        if fd in self.connections:
                            connection = self.connections[fd]
                            if connection.send_buffer:
                                try:
                                    sent = connection.backend_socket.send(connection.send_buffer)
                                    connection.send_buffer = connection.send_buffer[sent:]

                                    # Reset retry count on success
                                    connection.retry_count = 0
                                except BlockingIOError:
                                    # Resource temporarily unavailable, retry later
                                    pass
                                except OSError as e:
                                    logger.error(f"Error sending data to backend: {e}")
                                    self.retry(fd, self.handle_backend_data, fd)
        except KeyboardInterrupt:
            self.cleanup()

    def cleanup(self):
        self.epoll.unregister(self.server_socket.fileno())
        self.epoll.close()
        self.server_socket.close()

        for fd in self.connections:
            try:
                self.close_connection(fd)
            except:
                pass
    
    def close_connection(self, fd):
        if fd in self.connections:
            connection = self.connections.pop(fd)
            try:
                self.epoll.unregister(fd)
            except OSError as e:
                logger.error(f"Error unregistering fd {fd}: {e}")
            connection.socket.close()
            logger.info(f"Closed connection to client {connection.address}")

            if connection.backend_socket:
                backend_fd = connection.backend_socket.fileno()
                if backend_fd in self.backend_to_client:
                    try:
                        self.epoll.unregister(backend_fd)
                    except OSError as e:
                        logger.error(f"Error unregistering backend fd {backend_fd}: {e}")
                    connection.backend_socket.close()
                    self.backend_to_client.pop(backend_fd)
                    logger.info(f"Closed connection to backend {connection.backend_address}")

        elif fd in self.backend_to_client:
            client_fd = self.backend_to_client.pop(fd)
            if client_fd in self.connections:
                connection = self.connections.pop(client_fd)
                try:
                    self.epoll.unregister(client_fd)
                except OSError as e:
                    logger.error(f"Error unregistering client fd {client_fd}: {e}")
                connection.socket.close()
                logger.info(f"Closed connection to client {connection.address}")

                if connection.backend_socket:
                    backend_fd = connection.backend_socket.fileno()
                    try:
                        self.epoll.unregister(backend_fd)
                    except OSError as e:
                        logger.error(f"Error unregistering backend fd {backend_fd}: {e}")
                    connection.backend_socket.close()
                    logger.info(f"Closed connection to backend {connection.backend_address}")

def load_backends(config_file):
    with open(config_file, 'r') as f:
        backends = json.load(f)
    return [(backend['host'], backend['port']) for backend in backends]

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='HTTP Load Balancing Proxy')
    parser.add_argument('--host', type=str, default='127.0.0.1', help='Proxy host')
    parser.add_argument('--port', type=int, default=9000, help='Proxy port')
    parser.add_argument('--config', type=str, required=True, help='Path to backend servers config file')
    args = parser.parse_args()

    backends = load_backends(args.config)
    proxy = Proxy(args.host, args.port, backends)
    proxy.run()