# HTTP (Reverse) Proxy and Load Balancer
**Project 1 for CSDS 325: Networks**  
**@bluey22**  

## Project Description
Implement a combination of an HTTP 1.1 proxy and a load balancer to explore:
- Non-blocking connection handling w/ epoll
- Fault tolerance
- Scalability
- Proxies in distributed systems
- Pipelining with Head-of-Line blocking
- Nginx, Wireshark, wrk

## VirtualBox Commands: 
```bash
# View Actively Running VMs
VBoxManage list runningvms

# Start VM In Headless Mode
VBoxManage startvm Ubuntu --type headless

# Stop VM
VBoxManage controlvm Ubuntu savestate
```

## Long List of todos
### Turn Your Echo Server into an HTTP Proxy
Parse and Load Backend Servers from JSON
- Read your .conf file (JSON) and store backend servers in a list.
- Support multiple backends for load balancing.

Modify receive_data() to Handle HTTP Requests
- Extract HTTP headers from recv() data.
- Determine request type (GET, POST, etc.).
- Extract host and path.

Forward the Request to a Backend Server
- Implement round-robin or least-connections load balancing.
- Open a new connection to the selected backend.
- Forward the client's request.

Read and Return the Response
- Read from the backend server using recv().
- Forward the response back to the client.

Handle HTTP Keep-Alive and Pipelining
- Ensure efficient handling of multiple requests over the same connection.

Improve Performance with Non-Blocking Reads & Writes
- Prevent blocking when handling multiple clients and backends.
- Use EPOLLOUT for backend communication to avoid buffer issues.

Benchmark with wrk
- Once the proxy works, run wrk tests and analyze performance.

## Specifically fo Load Balancing: X-Request-ID Strategy
Modify Incoming HTTP Requests
- When a client request arrives, inject a unique request ID (UUID).
- Append the X-Request-ID header to the HTTP request before forwarding it.

Modify receive_data() to Extract the HTTP Request
- Read the HTTP request headers from recv().
- Insert a unique X-Request-ID.
- Forward the request to a backend server.

Modify send_data() to Handle Responses
- When a backend server sends a response, check the X-Request-ID.
- Route the response to the correct client using this identifier.

Update Nginx to Preserve the Header

## epoll
epoll is a Linux-specific high-performance I/O event notification mechanism in the kernel space that efficiently monitors multiple file descriptors to detect when they become ready for I/O operations. Unlike select and poll, which require checking all file descriptors on every call, epoll only processes active file descriptors. We can configure it to be edge-triggered (on event and only once) or level-triggered (continuously set while an event condition holds - similar performance to select).
