from nameko.extensions import SharedExtension, Entrypoint
from nameko.constants import WEB_SERVER_CONFIG_KEY
from nameko.exceptions import ConfigurationError
from collections import namedtuple, OrderedDict
import re
import select
from h2.errors import PROTOCOL_ERROR  # changed under h2 from 2.6.4?
from h2.events import (
    RequestReceived,
    DataReceived,
    StreamEnded,
    WindowUpdated,
    SettingsAcknowledged,
    RemoteSettingsChanged,
)
from h2.config import H2Configuration
from h2.connection import H2Connection
import eventlet
from functools import partial
from nameko.exceptions import ContainerBeingKilled
from nameko_grpc.inspection import Inspector
from nameko_grpc.streams import ReceiveStream, SendStream
from .constants import Cardinality
import socket
from logging import getLogger


log = getLogger(__name__)


SELECT_TIMEOUT = 0.01


def parse_address(address_string):
    # lifted from nameko.web.server
    BindAddress = namedtuple("BindAddress", ["address", "port"])
    address_re = re.compile(r"^((?P<address>[^:]+):)?(?P<port>\d+)$")
    match = address_re.match(address_string)
    if match is None:
        raise ConfigurationError(
            "Misconfigured bind address `{}`. "
            "Should be `[address:]port`".format(address_string)
        )
    address = match.group("address") or ""
    port = int(match.group("port"))
    return BindAddress(address, port)


class ServerConnectionManager(object):
    """
    An object that manages a single HTTP/2 connection on a GRPC server.
    """

    def __init__(self, sock, registered_paths, handle_request):
        self.sock = sock
        self.registered_paths = registered_paths
        self.handle_request = handle_request

        config = H2Configuration(client_side=False)
        self.conn = H2Connection(config=config)

        self.receive_streams = {}
        self.send_streams = {}

    def run_forever(self):
        self.conn.initiate_connection()
        self.sock.sendall(self.conn.data_to_send())

        while True:

            ready = select.select([self.sock], [], [], SELECT_TIMEOUT)
            if not ready[0]:
                self.on_idle_iteration()
                events = []
            else:
                data = self.sock.recv(65535)
                if not data:
                    break
                events = self.conn.receive_data(data)

            for event in events:
                if isinstance(event, RequestReceived):
                    self.request_received(event.headers, event.stream_id)
                elif isinstance(event, DataReceived):
                    self.data_received(event.data, event.stream_id)
                elif isinstance(event, StreamEnded):
                    self.stream_ended(event.stream_id)
                elif isinstance(event, WindowUpdated):
                    self.window_updated(event.stream_id)
                elif isinstance(event, SettingsAcknowledged):
                    pass
                elif isinstance(event, RemoteSettingsChanged):
                    pass

            self.sock.sendall(self.conn.data_to_send())

    def on_idle_iteration(self):
        for stream_id in list(self.send_streams.keys()):
            self.send_data(stream_id)

    def request_received(self, headers, stream_id):

        log.debug("request received, stream %s", stream_id)

        headers = OrderedDict(headers)
        http_path = headers[":path"]

        if http_path not in self.registered_paths:
            response_headers = (
                (":status", "404"),
                ("content-length", "0"),
                ("server", "nameko-grpc"),
            )
            self.conn.send_headers(stream_id, response_headers, end_stream=True)

        request_type = self.registered_paths[http_path]

        request_stream = ReceiveStream(stream_id, request_type)
        response_stream = SendStream(stream_id)
        self.receive_streams[stream_id] = request_stream
        self.send_streams[stream_id] = response_stream

        self.handle_request(http_path, request_stream, response_stream)

        self.conn.send_headers(
            stream_id,
            (
                (":status", "200"),
                ("content-type", "application/grpc+proto"),
                ("server", "nameko-grpc"),
            ),
            end_stream=False,
        )

    def data_received(self, data, stream_id):

        log.debug("data received on stream %s: %s...", stream_id, data[:100])

        request_stream = self.receive_streams.get(stream_id)
        if request_stream is None:
            # data for unknown stream, exit?
            self.conn.reset_stream(stream_id, error_code=PROTOCOL_ERROR)
            return

        request_stream.write(data)

        # if there is stuff to send now, send it
        self.send_data(stream_id)

    def stream_ended(self, stream_id):

        log.debug("stream ended, stream %s", stream_id)

        request_stream = self.receive_streams.pop(stream_id)
        request_stream.close()

        self.send_data(stream_id)

    def window_updated(self, stream_id):

        log.debug("window updated, stream %s", stream_id)

        self.send_data(stream_id)

    def send_data(self, stream_id):

        send_stream = self.send_streams.get(stream_id)

        if not send_stream:
            # window updates trigger sending of data, but can happen after a stream
            # has been completely sent
            return

        window_size = self.conn.local_flow_control_window(stream_id=stream_id)
        max_frame_size = self.conn.max_outbound_frame_size

        for chunk in send_stream.read(window_size, max_frame_size):
            log.debug("sending data on stream %s: %s...", stream_id, chunk[:100])
            self.conn.send_data(stream_id=stream_id, data=chunk)

        if send_stream.exhausted:
            log.debug("closing exhausted stream, stream %s", stream_id)
            self.conn.send_headers(stream_id, (("grpc-status", "0"),), end_stream=True)
            self.send_streams.pop(stream_id)


class GrpcServer(SharedExtension):
    def __init__(self):
        super(GrpcServer, self).__init__()
        self.is_accepting = True
        self.entrypoints = {}

    @property
    def method_path_map(self):
        return {
            entrypoint.method_path: entrypoint.input_type
            for entrypoint in self.entrypoints.values()
        }

    @property
    def bind_addr(self):
        address_str = self.container.config.get(WEB_SERVER_CONFIG_KEY, "0.0.0.0:50051")
        return parse_address(address_str)

    def register(self, entrypoint):
        self.entrypoints[entrypoint.method_path] = entrypoint

    def unregister(self, entrypoint):
        self.entrypoints.pop(entrypoint.method_path, None)

    def handle_request(self, method_path, request_stream, response_stream):
        entrypoint = self.entrypoints[method_path]
        self.container.spawn_managed_thread(
            partial(entrypoint.handle_request, request_stream, response_stream)
        )

    def run(self):
        while self.is_accepting:
            new_sock, _ = self.server_socket.accept()
            manager = ServerConnectionManager(
                new_sock, self.method_path_map, self.handle_request
            )
            self.container.spawn_managed_thread(manager.run_forever)

    def start(self):
        self.server_socket = eventlet.listen(self.bind_addr)
        # work around https://github.com/celery/kombu/issues/838
        self.server_socket.settimeout(None)
        self.container.spawn_managed_thread(self.run)

    def stop(self):
        self.is_accepting = False
        self.server_socket.close()
        super(GrpcServer, self).stop()

    def kill(self):
        # TODO extension should have a default kill?
        self.stop()


class Grpc(Entrypoint):

    grpc_server = GrpcServer()

    def __init__(self, stub, **kwargs):
        self.stub = stub
        super().__init__(**kwargs)

    @property
    def method_path(self):
        if self.is_bound():  # TODO why is this not a property?
            return Inspector(self.stub).path_for_method(self.method_name)

    @property
    def input_type(self):
        if self.is_bound():
            return Inspector(self.stub).input_type_for_method(self.method_name)

    @property
    def output_type(self):
        if self.is_bound():
            return Inspector(self.stub).output_type_for_method(self.method_name)

    @property
    def cardinality(self):
        if self.is_bound():
            return Inspector(self.stub).cardinality_for_method(self.method_name)

    def setup(self):
        self.grpc_server.register(self)

    def stop(self):
        self.grpc_server.unregister(self)

    def handle_request(self, request_stream, response_stream):

        # where does this come from?
        context = None

        request = request_stream

        if self.cardinality in (Cardinality.UNARY_STREAM, Cardinality.UNARY_UNARY):
            request = next(request)

        args = (request, context)
        kwargs = {}

        # context_data = self.unpack_message_headers(message)
        context_data = {}

        handle_result = partial(self.handle_result, response_stream)
        try:
            self.container.spawn_worker(
                self,
                args,
                kwargs,
                context_data=context_data,
                handle_result=handle_result,
            )
        except ContainerBeingKilled:
            # how to reject GRPC requests?
            pass

    def handle_result(self, response_stream, worker_ctx, result, exc_info):

        if self.cardinality in (Cardinality.STREAM_UNARY, Cardinality.UNARY_UNARY):
            result = (result,)

        response_stream.populate(result)

        return result, exc_info


grpc = Grpc.decorator
