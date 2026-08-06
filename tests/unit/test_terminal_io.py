from docker_manage_server.api import _socket_read, _socket_send


class SocketIoLike:
    def __init__(self):
        self.writes = []

    def read(self, size):
        assert size == 4096
        return b"output"

    def write(self, data):
        self.writes.append(data)
        return len(data)


class SocketIoWithRawSocket:
    class RawSocket:
        def __init__(self):
            self.writes = []

        def sendall(self, data):
            self.writes.append(data)

        def recv(self, size):
            assert size == 4096
            return b"raw-output"

    def __init__(self):
        self._sock = self.RawSocket()


def test_terminal_socket_adapter_supports_socket_io_read_and_write():
    socket = SocketIoLike()
    assert _socket_read(socket) == b"output"
    _socket_send(socket, b"input")
    assert socket.writes == [b"input"]


def test_terminal_socket_adapter_writes_through_raw_socket_when_available():
    socket = SocketIoWithRawSocket()
    _socket_send(socket, b"input")
    assert socket._sock.writes == [b"input"]


def test_terminal_socket_adapter_reads_through_raw_socket_when_available():
    socket = SocketIoWithRawSocket()
    assert _socket_read(socket) == b"raw-output"
