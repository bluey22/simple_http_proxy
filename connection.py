# connection.py
from collections import deque as queue
from utils import SocketType
from message_builder_http import MessageBuilderHTTP

class SocketContext:
    """
    A Data-Access Object (DAO) that stores information based on a socket connection efficiently
    for HTTP interactions, along with the socket itself. You will have one of these persistent
    SocketHTTP connections for each client (and for each backend, on the other side of the proxy)
    """
    def __init__(self, socket):
        self.socket_type: SocketType = None  # Enum, CLIENT_TO_PROXY or PROXY_TO_BACKEND
        self.file_descriptor: int = None
        self.socket = socket
        self.address: str = ""

        # Buffer Level 0: Socket Direct
        self.recv_buffer = bytearray()  # Can be reading a response to pass forward, or a request

        # Buffer Level 1: Partial HTTP Message in case of partial HTTP request after recv_buffer read (Promotion from Level 0)
        self.current_request: MessageBuilderHTTP = None
        self.current_response: MessageBuilderHTTP = None

        # Buffer Level 2: Ready for Transport, Read In Complete HTTP Messages (Promotion from Level 1)
        self.request_queue = queue()   # Stores Tuple of (X-Request-ID, Complete MessageBuilderHttp) from self.current_request
        self.response_queue = queue()  # Stores Tuple of (X-Request-ID, Complete MessageBuilderHttp) from self.current_response

        # Buffer Level 3: Transport: Requests from the Client/Proxy can be forwarded, Responses from the backend can be forwarded
        self.send_buffer = bytearray()

        # If we are a Client connection, we care about keeping order of the HTTP Requests we just got because we
        #   need to do 'Head-of-line' blocking on our promotions to self.send_buffer. This blocking is based on
        #   our self.client_request_order defined below, which follows the order of request_queue, but will not be
        #   cleaned out by our proxy, only getting dropped once responses are sent back that match
        self.client_request_order: queue[str] = queue()