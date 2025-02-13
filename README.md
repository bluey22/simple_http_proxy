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
        root /var/www/site_8001;
        index index.html;

        # Preserve X-Request-ID from incoming requests
        proxy_set_header X-Request-ID $http_x_request_id;

        # Ensure Nginx includes it in the response
        add_header X-Request-ID $http_x_request_id;
    }
}

# /var/www/site_8001/index.html
echo '<!DOCTYPE html>
<html>
<head>
    <title>Server 8001</title>
    <style>
        body { font-family: Arial, sans-serif; text-align: center; margin-top: 50px; }
        h1 { color: #007bff; }
    </style>
</head>
<body>
    <h1>Welcome to Server 8001</h1>
    <p>This is a simple lightweight web page running on port 8001.</p>
</body>
</html>' | sudo tee /var/www/site_8001/index.html
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
<head>
    <title>Server 8002</title>
    <style>
        body { font-family: Arial, sans-serif; text-align: center; margin-top: 50px; background-color: #f8f9fa; }
        h1 { color: #28a745; }
    </style>
</head>
<body>
    <h1>Welcome to Server 8002</h1>
    <p>This is a different web page served on port 8002.</p>
</body>
</html>
```

# Notes:
## epoll()
epoll is a Linux-specific high-performance I/O event notification mechanism in the kernel space that efficiently monitors multiple file descriptors to detect when they become ready for I/O operations. Unlike select and poll, which require checking all file descriptors on every call, epoll only processes active file descriptors. We can configure it to be edge-triggered (on event and only once) or level-triggered (continuously set while an event condition holds - similar performance to select).

## Load Balacning and X-Request-ID
Modify Incoming HTTP Requests
- When a client request arrives, inject a unique request ID (UUID).
- Append the X-Request-ID header to the HTTP request before forwarding it.

We load balance in a round robin fashion based on the number of backend servers we have. We can route responses to the original client using 
the X-Request-ID
