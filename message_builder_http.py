# message_builder_http.py

class MessageBuilderHTTP:
    """
    A lightweight state object to accumulate information about a complete HTTP request
    """
    def __init__(self):
        self.is_response = False
        self.headers_received = False
        self.header_buffer = bytearray()     # partial header
        self.headers = {}                    # final parsed headers (Host, User-Agent, Content-Length, etc.)
        self.method = ''                     # e.g. GET, POST, ...
        self.path = ''                       # e.g. /index.html (request target)
        self.http_version = "HTTP/1.1"
        self.content_length = 0
        self.body_received = 0
        self.body_data = bytearray()
        self.status_code = 0
        self.status_message = ''
        self.keep_alive = True               # default for HTTP/1.1 unless told otherwise

    def reset(self):
        """Reset for the next pipelined message on the same connection."""
        self.__init__()  # re-init all fields

    def parse_data(self, data: bytes) -> int:
        """
        Parse as much from 'data' as possible into headers/body
        Return how many bytes were consumed from 'data'

        Example workflow: Create object, call parse_data() then check is_complete() after
        """
        offset = 0
        length = len(data)

        # 1) If headers not received, parse them
        while offset < length and not self.headers_received:
            self.header_buffer.append(data[offset])
            offset += 1

            # 2) Check if we've hit the end of the headers
            if b"\r\n\r\n" in self.header_buffer:
                self.headers_received = True
                
                # 3) Parse Header Lines
                raw_headers = bytes(self.header_buffer)
                header_part, _ = raw_headers.split(b"\r\n\r\n", 1)  # single split occurence, return headers
                self.parse_header_lines(header_part)
                break

        # 2) If all headers are received, parse the body (if it exists!)
        if self.headers_received:
            body_needed = self.content_length - self.body_received

            if body_needed > 0 and offset < length:
                can_take = min(body_needed, length - offset)
                self.body_data.extend(data[offset: offset + can_take])
                self.body_received += can_take
                offset += can_take

        return offset

    def parse_header_lines(self, header_part: bytes):
        """
        Poppulate MessageBuilderHTTP object based on header information
        """
        lines = header_part.split(b"\r\n")  # lines = list of byte objects

        # 1) Deal with the first request/response line
        first_line = lines[0].decode("utf-8", errors="replace")
        parts = first_line.split()

        if len(parts) >= 3:
            if parts[0].startswith("HTTP"):
                # Case 1) Response code handling
                self.is_response = True
                self.http_version = parts[0]
                self.status_code = parts[1]
                self.status_message = " ".join(parts[2:]) 
            else:
                # Case 2) Request code handling
                self.method = parts[0]
                self.path = parts[1]
                self.http_version = parts[2]

        # 2) Deal with the rest of the HTTP Headers key-value mappings
        for line in lines[1:]:
            line_dec = line.decode("utf-8", errors="replace")
            if ":" in line_dec:
                k, v = line_dec.split(":", 1)
                self.headers[k.strip()] = v.strip()

        # 3) Get content length based on this map of headers
        cl = self.headers.get("Content-Length", "0")
        try:
            self.content_length = int(cl)
        except ValueError:
            self.content_length = 0

        # Check keep-alive
        conn_hdr = self.headers.get("Connection", "").lower()
        if self.http_version == "HTTP/1.1":
            if conn_hdr == "close":
                self.keep_alive = False
        else:
            # If not HTTP/1.1, assume close unless "keep-alive"
            self.keep_alive = (conn_hdr == "keep-alive")

    def is_complete(self) -> bool:
        """Returns True if MessageBuilderHTTP is a complete HTTP message"""
        return (self.headers_received and (self.body_received >= self.content_length))

    def build_full_message(self) -> bytes:
        """Returns the final raw HTTP message"""
        raw_headers = bytes(self.header_buffer)
        separator = raw_headers.find(b"\r\n\r\n")
        if separator == -1:
            return raw_headers + self.body_data
        return raw_headers + self.body_data
    
    def build_full_message(self) -> bytes:
        """Returns the final raw HTTP message"""
        message = bytearray()

        # 1) Build the start line (response or request)
        if self.is_response:
            message.extend(f"{self.http_version} {self.status_code} {self.status_message}\r\n".encode("utf-8"))
        else:
            message.extend(f"{self.method} {self.path} {self.http_version}\r\n".encode("utf-8"))

        # 2) Append the rest of the headers
        for key, value in self.headers.items():
            message.extend(f"{key}: {value}\r\n".encode("utf-8"))

        # 3) Append the separator and the body if it exists
        message.extend(b"\r\n")
        if self.body_data:
            message.extend(self.body_data)

        return bytes(message) 