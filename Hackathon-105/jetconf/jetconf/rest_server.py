import asyncio
import ssl

from io import BytesIO
from collections import OrderedDict
from colorlog import error, warning as warn, info
from typing import List, Tuple, Dict, Callable, Optional

from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.errors import ErrorCodes as H2ErrorCodes
from h2.exceptions import ProtocolError
from h2.events import DataReceived, RequestReceived, RemoteSettingsChanged, StreamEnded, WindowUpdated

from . import http_handlers as handlers
from .config import CONFIG_HTTP, API_ROOT_data, API_ROOT_RUNNING_data, API_ROOT_ops, API_ROOT_ylv
from .helpers import SSLCertT, LogHelpers
from .data import BaseDatastore
from .http_handlers import (
    HttpResponse,
    HttpStatus,
    RestconfErrType,
    ERRTAG_MALFORMED,
    ERRTAG_OPNOTSUPPORTED,
    ERRTAG_REQLARGE
)


HandlerConditionT = Callable[[str, str], bool]  # Function(method, path) -> bool
HttpHandlerT = Callable[[OrderedDict, Optional[str], SSLCertT], handlers.HttpResponse]
debug_srv = LogHelpers.create_module_dbg_logger(__name__)

h2_handlers = None  # type: HttpHandlerList


class RequestData:
    def __init__(self, headers: OrderedDict, data: BytesIO):
        self.headers = headers
        self.data = data
        self.data_overflow = False


class ResponseData:
    def __init__(self, data: bytes):
        self.data = data
        self.bytes_sent = 0


class HttpHandlerList:
    def __init__(self):
        self.handlers = []              # type: List[Tuple[HandlerConditionT, HttpHandlerT]]
        self.default_handler = None     # type: HttpHandlerT

    def register(self, condition: HandlerConditionT, handler: HttpHandlerT):
        self.handlers.append((condition, handler))

    def register_default(self, handler: HttpHandlerT):
        self.default_handler = handler

    def get_handler(self, method: str, path: str) -> HttpHandlerT:
        for h in self.handlers:
            if h[0](method, path):
                return h[1]

        return self.default_handler


class H2Protocol(asyncio.Protocol):
    def __init__(self):
        self.conn = H2Connection(H2Configuration(client_side=False, header_encoding="utf-8"))
        self.transport = None
        self.stream_data = {}       # type: Dict[int, RequestData]
        self.resp_stream_data = {}  # type: Dict[int, ResponseData]
        self.client_cert = None     # type: SSLCertT

    def connection_made(self, transport: asyncio.Transport):
        self.transport = transport
        self.client_cert = transport.get_extra_info("peercert")

        ssl_context = transport.get_extra_info("ssl_object")
        if ssl.HAS_ALPN:
            agreed_protocol = ssl_context.selected_alpn_protocol()
        else:
            agreed_protocol = ssl_context.selected_npn_protocol()

        if agreed_protocol is None:
            error("Connection error, client does not support HTTP/2")
            transport.close()
        else:
            self.conn.initiate_connection()

    def data_received(self, data: bytes):
        events = self.conn.receive_data(data)

        for event in events:
            if isinstance(event, RequestReceived):
                # Store request headers
                headers = OrderedDict(event.headers)
                request_data = RequestData(headers, BytesIO())
                self.stream_data[event.stream_id] = request_data
            elif isinstance(event, DataReceived):
                # Store incoming data
                try:
                    stream_data = self.stream_data[event.stream_id]
                except KeyError:
                    self.conn.reset_stream(event.stream_id, error_code=H2ErrorCodes.PROTOCOL_ERROR)
                else:
                    # Check if incoming data are not excessively large
                    if (stream_data.data.tell() + len(event.data)) < (CONFIG_HTTP["UPLOAD_SIZE_LIMIT"] * 1048576):
                        stream_data.data.write(event.data)
                    else:
                        stream_data.data_overflow = True
                        self.conn.reset_stream(event.stream_id, error_code=H2ErrorCodes.ENHANCE_YOUR_CALM)
            elif isinstance(event, StreamEnded):
                # Process request
                try:
                    request_data = self.stream_data.pop(event.stream_id)
                except KeyError:
                    self.send_response(
                        HttpResponse.error(HttpStatus.BadRequest, RestconfErrType.Transport, ERRTAG_MALFORMED),
                        event.stream_id
                    )
                else:
                    if request_data.data_overflow:
                        self.send_response(
                            HttpResponse.error(HttpStatus.ReqTooLarge, RestconfErrType.Transport, ERRTAG_REQLARGE),
                            event.stream_id
                        )
                    else:
                        headers = request_data.headers
                        http_method = headers[":method"]

                        if http_method in ("GET", "DELETE", "OPTIONS", "HEAD"):
                            self.run_request_handler(headers, event.stream_id, None)
                        elif http_method in ("PUT", "POST"):
                            body = request_data.data.getvalue().decode("utf-8")
                            self.run_request_handler(headers, event.stream_id, body)
                        else:
                            warn("Unknown http method \"{}\"".format(headers[":method"]))
                            self.send_response(
                                HttpResponse.error(
                                    HttpStatus.MethodNotAllowed,
                                    RestconfErrType.Transport,
                                    ERRTAG_OPNOTSUPPORTED
                                ),
                                event.stream_id
                            )
            elif isinstance(event, RemoteSettingsChanged):
                changed_settings = {}
                for s in event.changed_settings.items():
                    changed_settings[s[0]] = s[1].new_value
                self.conn.update_settings(changed_settings)
            elif isinstance(event, WindowUpdated):
                try:
                    debug_srv(
                        "str {} nw={}".format(event.stream_id, self.conn.local_flow_control_window(event.stream_id))
                    )
                    self.send_response_continue(event.stream_id)
                except (ProtocolError, KeyError) as e:
                    # debug_srv("wupdexception strid={}: {}".format(event.stream_id, str(e)))
                    pass
            # else:
            #     print(type(event))

            dts = self.conn.data_to_send()
            if dts:
                self.transport.write(dts)

    def max_chunk_size(self, stream_id: int):
        return min(self.conn.max_outbound_frame_size, self.conn.local_flow_control_window(stream_id))

    # Find and run handler for specific URI and HTTP method
    def run_request_handler(self, headers: OrderedDict, stream_id: int, data: Optional[str]):
        url_path = headers[":path"].split("?")[0].rstrip("/")
        method = headers[":method"]

        if method == "HEAD":
            h = h2_handlers.get_handler("GET", url_path)
        else:
            h = h2_handlers.get_handler(method, url_path)

        if not h:
            self.send_response(
                HttpResponse.error(HttpStatus.BadRequest, RestconfErrType.Transport, ERRTAG_MALFORMED),
                stream_id
            )
        else:
            # Run handler and send HTTP response
            resp = h(headers, data, self.client_cert)
            if method == "HEAD":
                resp.data = bytes()
            self.send_response(resp, stream_id)

    def send_response(self, resp: HttpResponse, stream_id: int):
        resp_headers = (
            (":status", resp.status_code),
            ("Content-Type", resp.content_type),
            ("Content-Length", str(resp.content_length)),
            ("Server", CONFIG_HTTP["SERVER_NAME"]),
            ("Cache-Control", "No-Cache"),
            ("Access-Control-Allow-Origin", "*"),
            ("Access-Control-Allow-Methods", "POST, GET, OPTIONS, PUT, DELETE"),
            ("Access-Control-Allow-Headers", "Content-Type")
        )

        if resp.extra_headers:
            resp_headers_od = OrderedDict(resp_headers)
            resp_headers_od.update(resp.extra_headers)
            resp_headers = resp_headers_od.items()

        self.conn.send_headers(stream_id, resp_headers)

        # Do this for optimization
        if len(resp.data) <= self.max_chunk_size(stream_id):
            self.conn.send_data(stream_id, resp.data, end_stream=True)
        else:
            self.resp_stream_data[stream_id] = ResponseData(resp.data)
            self.send_response_continue(stream_id)

    def send_response_continue(self, stream_id: int):
        resp_data = self.resp_stream_data[stream_id]
        debug_srv("Continuing...")

        while self.max_chunk_size(stream_id) != 0:
            if resp_data.bytes_sent >= len(resp_data.data):
                self.send_response_end(stream_id)
                return

            # Get available window
            chunk_size = self.max_chunk_size(stream_id)
            data_chunk = resp_data.data[resp_data.bytes_sent:resp_data.bytes_sent + chunk_size]
            resp_data.bytes_sent += chunk_size
            debug_srv("len = {}, max = {}, sent={}, dlen={}, strid={}".format(
                len(data_chunk),
                chunk_size,
                resp_data.bytes_sent,
                len(resp_data.data),
                stream_id
            ))
            self.conn.send_data(stream_id, data_chunk, end_stream=False)

    def send_response_end(self, stream_id: int):
        debug_srv("Ending stream {}...".format(stream_id))
        self.conn.send_data(stream_id, bytes(), end_stream=True)
        del self.resp_stream_data[stream_id]


class RestServer:
    def __init__(self):
        # HTTP server init
        self.http_handlers = HttpHandlerList()
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.options |= (ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1 | ssl.OP_NO_COMPRESSION)
        ssl_context.load_cert_chain(certfile=CONFIG_HTTP["SERVER_SSL_CERT"], keyfile=CONFIG_HTTP["SERVER_SSL_PRIVKEY"])
        if ssl.HAS_ALPN:
            ssl_context.set_alpn_protocols(["h2"])
        else:
            info("Python not compiled with ALPN support, using NPN instead.")
            ssl_context.set_npn_protocols(["h2"])
        if not CONFIG_HTTP["DBG_DISABLE_CERTS"]:
            ssl_context.verify_mode = ssl.CERT_REQUIRED
        ssl_context.load_verify_locations(cafile=CONFIG_HTTP["CA_CERT"])

        self.loop = asyncio.get_event_loop()

        # Each client connection will create a new H2Protocol instance
        listener = self.loop.create_server(
            H2Protocol,
            "127.0.0.1" if CONFIG_HTTP["LISTEN_LOCALHOST_ONLY"] else "",
            CONFIG_HTTP["PORT"],
            ssl=ssl_context
        )
        self.server = self.loop.run_until_complete(listener)

    def register_api_handlers(self, datastore: BaseDatastore):
        global h2_handlers

        # Register HTTP handlers
        api_get_root = handlers.api_root_handler
        api_get_ylv = handlers.api_ylv_handler
        api_get = handlers.create_get_api(datastore)
        api_get_run = handlers.create_get_running_api(datastore)
        api_post = handlers.create_post_api(datastore)
        api_put = handlers.create_put_api(datastore)
        api_delete = handlers.create_api_delete(datastore)
        api_get_op = handlers.create_api_get_op(datastore)
        api_op = handlers.create_api_op(datastore)

        self.http_handlers.register(lambda m, p: (m == "GET") and (p.startswith(API_ROOT_data)), api_get)
        self.http_handlers.register(lambda m, p: (m == "GET") and (p.startswith(API_ROOT_RUNNING_data)), api_get_run)
        self.http_handlers.register(lambda m, p: (m == "GET") and (p == API_ROOT_ylv), api_get_ylv)
        self.http_handlers.register(lambda m, p: (m == "GET") and (p == CONFIG_HTTP["API_ROOT"]), api_get_root)
        self.http_handlers.register(lambda m, p: (m == "POST") and (p.startswith(API_ROOT_data)), api_post)
        self.http_handlers.register(lambda m, p: (m == "PUT") and (p.startswith(API_ROOT_data)), api_put)
        self.http_handlers.register(lambda m, p: (m == "DELETE") and (p.startswith(API_ROOT_data)), api_delete)
        self.http_handlers.register(lambda m, p: (m == "GET") and (p.startswith(API_ROOT_ops)), api_get_op)
        self.http_handlers.register(lambda m, p: (m == "POST") and (p.startswith(API_ROOT_ops)), api_op)
        self.http_handlers.register(lambda m, p: m == "OPTIONS", handlers.options_api)

        h2_handlers = self.http_handlers

    def register_static_handlers(self):
        global h2_handlers

        self.http_handlers.register(lambda m, p: (m == "GET") and not (p.startswith(CONFIG_HTTP["API_ROOT"])), handlers.get_file)
        self.http_handlers.register_default(handlers.unknown_req_handler)

        h2_handlers = self.http_handlers

    def run(self):
        info("Server started on {}".format(self.server.sockets[0].getsockname()))
        self.loop.run_forever()

    def shutdown(self):
        self.server.close()
        self.loop.run_until_complete(self.server.wait_closed())
        self.loop.close()
