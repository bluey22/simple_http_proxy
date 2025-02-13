# utils.py
from enum import Enum

class SocketType(Enum):
    CLIENT_TO_PROXY = 1
    PROXY_TO_BACKEND = 2