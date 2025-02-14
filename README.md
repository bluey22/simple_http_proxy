# HTTP (Reverse) Proxy and Load Balancer
**Project 1 for CSDS 325: Networks**  
**@bluey22**  

Python: 3.10.12
Linux: 22.0.4 Ubuntu

## Project Description
Implement a combination of an HTTP 1.1 proxy and a load balancer to explore:
- Non-blocking connection handling w/ epoll
- Fault tolerance
- Scalability
- Proxies in distributed systems
- Pipelining with Head-of-Line blocking
- Nginx, Wireshark, wrk

For testing purposes, we'll spin up 3 simple backend http servers that simply respond to GET requests with their own HTML page with a simeple text message

## VirtualBox Commands: 
```bash
# View Actively Running VMs
VBoxManage list runningvms

# Start VM In Headless Mode
VBoxManage startvm Ubuntu --type headless

# Stop VM
VBoxManage controlvm Ubuntu savestate
```

# Configuring Our Backends:
For testing, we run 3 backends (8001, 8002, 8003). Here is the following site for 8001.
```bash
# /etc/nginx/sites-available/site_8001
server {
    listen 8001;
    server_name localhost;

    location / {
        root /var/www/html;
        index index.html;

        # Preserve X-Request-ID from incoming requests
        proxy_set_header X-Request-ID $http_x_request_id;

        # Ensure Nginx includes it in the response
        add_header X-Request-ID $http_x_request_id;
    }
}

# /var/www/html/index.html
sudo mkdir -p /var/www/html
echo "<h1>Welcome to the Load-Balanced Web Service</h1>" | sudo tee /var/www/html/index.html
sudo chmod -R 755 /var/www/html
sudo chown -R www-data:www-data /var/www/html
```

Symbolically link these to ```bash/sites-enabled/``` so that nginx loads them on startup.

Check for errors: ```sudo nginx -t```, etc.

Restarting the nginx service (and thus, backend servers): ```bash sudo systemctl restart nginx```

We can now test our 3 web servers with ```bashcurl http://localhost:8001```, etc.

## Example GET Request and Response:
### GET
```bash
telnet localhost 8002

GET / HTTP/1.1
Host: localhost
```
### RESPONSE
```bash
HTTP/1.1 200 OK
Server: nginx/1.18.0 (Ubuntu)
Date: Thu, 13 Feb 2025 03:18:32 GMT
Content-Type: text/html
Content-Length: 359
Last-Modified: Thu, 13 Feb 2025 03:12:28 GMT
Connection: keep-alive
ETag: "67ad631c-167"
Accept-Ranges: bytes

<!DOCTYPE html>
<html>
<h1>Welcome to the Load-Balanced Web Service</h1>
</html>
```

# Test HTTP/1.1 Load Balancing HTTP Proxy

# Testing a single request
```bash 
# Run main.py in one terminal. In another, run the followiung
curl -v http://127.0.0.1:9000/
```

# Testing pipelining

# Notes:
## epoll()
epoll is a Linux-specific high-performance I/O event notification mechanism in the kernel space that efficiently monitors multiple file descriptors to detect when they become ready for I/O operations. Unlike select and poll, which require checking all file descriptors on every call, epoll only processes active file descriptors. We can configure it to be edge-triggered (on event and only once) or level-triggered (continuously set while an event condition holds - similar performance to select).

## Load Balacning and X-Request-ID
Modify Incoming HTTP Requests
- When a client request arrives, inject a unique request ID (UUID).
- Append the X-Request-ID header to the HTTP request before forwarding it.

We load balance in a round robin fashion based on the number of backend servers we have. We can route responses to the original client using 
the X-Request-ID
