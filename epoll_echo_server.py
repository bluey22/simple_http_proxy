# epoll_echo_server.py
import socket
import select
  
ECHO_PORT = 9999
BUF_SIZE = 4096
biterrs = select.EPOLLERR | select.EPOLLHUP | select.EPOLLRDHUP

def main():
    print("----- Echo Server -----")
    try:
        serverSock = socket.socket()
    except socket.error as err:
        print ("socket creation failed with error %s" %(err))
        exit(1)
    serverSock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    serverSock.bind(('0.0.0.0', ECHO_PORT))
    serverSock.listen(5)

    read_only = select.EPOLLIN | select.EPOLLPRI | select.EPOLLHUP | select.EPOLLERR
    read_write = read_only | select.EPOLLOUT
    biterrs = [25,24,8,16,9,17,26,10,18]
    epoll = select.epoll()
    epoll.register(serverSock.fileno(), read_only)

    connections = {}
    responses = {}
    try:
        while True:
            events = epoll.poll(1)
            for fileNo, event in events:
                if fileNo == serverSock.fileno():
                    connection, addr = serverSock.accept()
                    connection.setblocking(False)
                    epoll.register(connection.fileno(), read_only)
                    connections[connection.fileno()] = connection
                    responses[connection.fileno()] = b''
                elif (event & select.EPOLLIN) or (event & select.EPOLLPRI):
                    try:
                        recvData = connections[fileNo].recv(BUF_SIZE)
                        if not recvData:
                            connections[fileNo].close()
                            epoll.unregister(fileNo)
                            del connections[fileNo], responses[fileNo]
                            continue
                        responses[fileNo] += recvData
                        epoll.modify(fileNo, read_write) 

                    except ConnectionResetError:
                        epoll.unregister(fileNo)
                        connections[fileNo].close()
                        del connections[fileNo], responses[fileNo]
                        
                elif (event & select.EPOLLOUT):
                    try:
                        byteswritten = connections[fileNo].send(responses[fileNo])
                        responses[fileNo] = responses[fileNo][byteswritten:]
                        if len(responses[fileNo]) == 0:
                            epoll.modify(fileNo, read_only)

                    except ConnectionResetError:
                        epoll.unregister(fileNo)
                        connections[fileNo].close()
                        del connections[fileNo], responses[fileNo]

                elif event in bitters:
                    epoll.unregister(fileNo)
                    connections[fileNo].close()
                    del connections[fileNo], responses[fileNo]

    finally:
        epoll.unregister(serverSock.fileno())
        epoll.close()
        serverSock.close()

if __name__ == '__main__':
    main()
