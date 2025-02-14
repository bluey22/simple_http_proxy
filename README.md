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

## Benchmarking
Here are the test commands and results. In terms of setting up the tests, that is detailed later below:

Testing 500 requests to our proxy:
```bash
# Command:
(.venv) $ ab -n 500 -c 10 -k http://127.0.0.1:9000/

# Out
This is ApacheBench, Version 2.3 <$Revision: 1879490 $>
Copyright 1996 Adam Twiss, Zeus Technology Ltd, http://www.zeustech.net/
Licensed to The Apache Software Foundation, http://www.apache.org/

Benchmarking 127.0.0.1 (be patient)
Completed 100 requests
Completed 200 requests
Completed 300 requests
Completed 400 requests
Completed 500 requests
Finished 500 requests


Server Software:        nginx/1.18.0
Server Hostname:        127.0.0.1
Server Port:            9000

Document Path:          /
Document Length:        50 bytes

Concurrency Level:      10
Time taken for tests:   0.051 seconds
Complete requests:      500
Failed requests:        0
Keep-Alive requests:    500
Total transferred:      173500 bytes
HTML transferred:       25000 bytes
Requests per second:    9845.04 [#/sec] (mean)
Time per request:       1.016 [ms] (mean)
Time per request:       0.102 [ms] (mean, across all concurrent requests)
Transfer rate:          3336.16 [Kbytes/sec] received

Connection Times (ms)
              min  mean[+/-sd] median   max
Connect:        0    0   0.0      0       0
Processing:     0    1   0.3      1       2
Waiting:        0    1   0.3      1       2
Total:          0    1   0.3      1       2

Percentage of the requests served within a certain time (ms)
  50%      1
  66%      1
  75%      1
  80%      1
  90%      1
  95%      1
  98%      2
  99%      2
 100%      2 (longest request)
 ```

Testing 10 threads with 1000 connections to our proxy

```bash
# Command:
(.venv) $ wrk -t10 -c1000 -d10s --latency http://127.0.0.1:9000/

# Output: (3 Backends)
Running 10s test @ http://127.0.0.1:9000/
  10 threads and 1000 connections
  Thread Stats   Avg      Stdev     Max   +/- Stdev
    Latency    77.40ms  122.09ms   1.99s    97.96%
    Req/Sec     0.98k   765.09     8.04k    69.44%
  Latency Distribution
     50%   65.95ms
     75%   85.41ms
     90%   92.22ms
     99%  677.97ms
  89142 requests in 10.07s, 29.50MB read
  Socket errors: connect 0, read 0, write 0, timeout 368
Requests/sec:   8849.14
Transfer/sec:      2.93MB

# Output: (1 Backend - Slower and more timeouts) 
Running 10s test @ http://127.0.0.1:9000/
  10 threads and 1000 connections
  Thread Stats   Avg      Stdev     Max   +/- Stdev
    Latency    95.76ms  116.44ms   2.00s    98.34%
    Req/Sec     1.01k   276.69     2.29k    71.69%
  Latency Distribution
     50%   82.76ms
     75%   96.69ms
     90%  110.11ms
     99%  662.24ms
  95088 requests in 10.05s, 31.47MB read
  Socket errors: connect 0, read 0, write 0, timeout 326
Requests/sec:   9464.35
Transfer/sec:      3.13MB
 ```

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

# Testing the HTTP/1.1 Load Balancing HTTP Proxy (after following above config)
## Testing a single request
```bash 
# Run proxy.py in one terminal. In another, run the followiung
curl -v http://127.0.0.1:9000/
```
## Testing pipelining
```bash 
# Run proxy.py in one terminal. In another, test 500 requests:
ab -n 500 -c 10 -k http://127.0.0.1:9000/

# For limit testing, in another, run the followiung for limit testing
wrk -t10 -c1000 -d10s --latency http://127.0.0.1:9000/
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
