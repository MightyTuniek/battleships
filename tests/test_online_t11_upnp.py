"""T11: UPnP client (spec 9). Fake IGD on loopback; never touches LAN."""
import http.server
import socket
import socketserver
import sys
import threading
import time
import xml.etree.ElementTree as ET

sys.path.insert(0, ".")

import battleships as bs

DESC_XML = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
 <device>
  <deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:1</deviceType>
  <serviceList>
   <service>
    <serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
    <controlURL>/ctl</controlURL>
   </service>
  </serviceList>
 </device>
</root>"""


class FakeIGD:
    """SSDP responder (loopback unicast) + SOAP HTTP server."""

    def __init__(self, external_ip="203.0.113.8", desc=DESC_XML, fault=None):
        self.external_ip = external_ip
        self.desc = desc
        self.fault = fault  # action substring that gets a SOAP fault
        self.added = []
        self.deleted = []
        self.got_external = 0
        self._ssdp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._ssdp.bind(("127.0.0.1", 0))
        self._ssdp.settimeout(0.2)
        self.ssdp_port = self._ssdp.getsockname()[1]
        self._closed = False
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = outer.desc.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length)
                action = self.headers.get("SOAPAction", "")
                if outer.fault and outer.fault in action:
                    fault = ("<s:Envelope xmlns:s="
                             "'http://schemas.xmlsoap.org/soap/envelope/'>"
                             "<s:Body><s:Fault><faultstring>err</faultstring>"
                             "</s:Fault></s:Body></s:Envelope>").encode()
                    self.send_response(500)
                    self.send_header("Content-Length", str(len(fault)))
                    self.end_headers()
                    self.wfile.write(fault)
                    return
                try:
                    root = ET.fromstring(raw)
                    ns = {"s": "http://schemas.xmlsoap.org/soap/envelope/"}
                    body = root.find("s:Body", ns)
                    call = list(body)[0]
                    args = {}
                    for child in list(call):
                        tag = child.tag.split("}")[-1]
                        args[tag] = child.text or ""
                except Exception:
                    args = {}
                if "GetExternalIPAddress" in action:
                    outer.got_external += 1
                    resp = ("<s:Envelope xmlns:s="
                            "'http://schemas.xmlsoap.org/soap/envelope/'>"
                            "<s:Body><u:GetExternalIPAddressResponse xmlns:u="
                            "'urn:schemas-upnp-org:service:WANIPConnection:1'>"
                            "<NewExternalIPAddress>%s</NewExternalIPAddress>"
                            "</u:GetExternalIPAddressResponse></s:Body>"
                            "</s:Envelope>" % outer.external_ip).encode()
                elif "AddPortMapping" in action:
                    outer.added.append(args)
                    resp = ("<s:Envelope xmlns:s="
                            "'http://schemas.xmlsoap.org/soap/envelope/'>"
                            "<s:Body><u:AddPortMappingResponse xmlns:u="
                            "'urn:schemas-upnp-org:service:WANIPConnection:1'/>"
                            "</s:Body></s:Envelope>").encode()
                elif "DeletePortMapping" in action:
                    outer.deleted.append(args)
                    resp = ("<s:Envelope xmlns:s="
                            "'http://schemas.xmlsoap.org/soap/envelope/'>"
                            "<s:Body><u:DeletePortMappingResponse xmlns:u="
                            "'urn:schemas-upnp-org:service:WANIPConnection:1'/>"
                            "</s:Body></s:Envelope>").encode()
                else:
                    resp = b"<s:Envelope/>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

        self._http = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        self._http.daemon_threads = True
        self.http_port = self._http.server_address[1]
        threading.Thread(target=self._http.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True).start()
        threading.Thread(target=self._ssdp_loop, daemon=True).start()

    @property
    def ssdp_addr(self):
        return ("127.0.0.1", self.ssdp_port)

    def _ssdp_loop(self):
        while not self._closed:
            try:
                data, src = self._ssdp.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                text = data.decode("latin-1")
            except ValueError:
                continue
            if "M-SEARCH" not in text:
                continue
            loc = "http://127.0.0.1:%d/desc.xml" % self.http_port
            resp = ("HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=1800\r\n"
                    "LOCATION: %s\r\nST: %s\r\n\r\n" % (loc, "upnp:rootdevice"))
            try:
                self._ssdp.sendto(resp.encode("latin-1"), src)
            except OSError:
                return

    def close(self):
        self._closed = True
        try:
            self._ssdp.close()
        except OSError:
            pass
        try:
            self._http.shutdown()
        except Exception:
            pass
        try:
            self._http.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    import unittest

    class UpnpTest(unittest.TestCase):
        def test_discover_find_fake(self):
            igd = FakeIGD()
            try:
                locs = bs.upnp_discover(ssdp_addr=igd.ssdp_addr, wait=0.5)
                self.assertTrue(any("desc.xml" in loc for loc in locs))
            finally:
                igd.close()

        def test_no_router_returns_empty_fast(self):
            dead = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            dead.bind(("127.0.0.1", 0))
            addr = ("127.0.0.1", dead.getsockname()[1])
            dead.close()
            t0 = time.monotonic()
            locs = bs.upnp_discover(ssdp_addr=addr, wait=0.5)
            self.assertEqual(locs, [])
            self.assertLess(time.monotonic() - t0, bs.SSDP_WAIT_S + 1)

        def test_describe_and_external_ip(self):
            igd = FakeIGD()
            try:
                locs = bs.upnp_discover(ssdp_addr=igd.ssdp_addr, wait=0.5)
                ctl, svc = bs.upnp_describe(locs[0])
                self.assertTrue(ctl.endswith("/ctl"))
                self.assertIn("WANIPConnection", svc)
                self.assertEqual(bs.upnp_external_ip(ctl, svc),
                                 "203.0.113.8")
            finally:
                igd.close()

        def test_malformed_xml_never_raises(self):
            igd = FakeIGD(desc="<root><broken")
            try:
                locs = bs.upnp_discover(ssdp_addr=igd.ssdp_addr, wait=0.5)
                self.assertIsNone(bs.upnp_describe(locs[0]))
                info = bs.upnp_map(51234, ssdp_addr=igd.ssdp_addr, wait=0.5)
                self.assertFalse(info["available"])
            finally:
                igd.close()

        def test_add_and_delete_mapping_args(self):
            igd = FakeIGD()
            try:
                info = bs.upnp_map(51234, internal_client="192.168.1.7",
                                   ssdp_addr=igd.ssdp_addr, wait=0.5)
                self.assertTrue(info["available"])
                self.assertEqual(info["external_ip"], "203.0.113.8")
                self.assertEqual(len(igd.added), 1)
                args = igd.added[0]
                self.assertEqual(args.get("NewExternalPort"), "51234")
                self.assertEqual(args.get("NewInternalPort"), "51234")
                self.assertEqual(args.get("NewInternalClient"), "192.168.1.7")
                self.assertEqual(args.get("NewProtocol"), "TCP")
                self.assertEqual(args.get("NewRemoteHost"), "")
                self.assertEqual(args.get("NewLeaseDuration"),
                                 str(bs.UPNP_LEASE_S))
                self.assertTrue(bs.upnp_unmap(info))
                self.assertEqual(len(igd.deleted), 1)
                self.assertEqual(igd.deleted[0].get("NewExternalPort"),
                                 "51234")
            finally:
                igd.close()

        def test_soap_fault_is_unavailable(self):
            igd = FakeIGD(fault="AddPortMapping")
            try:
                info = bs.upnp_map(51234, ssdp_addr=igd.ssdp_addr, wait=0.5)
                self.assertFalse(info["available"])
            finally:
                igd.close()

        def test_cgnat_flagged(self):
            for ip in ("10.1.2.3", "172.16.9.9", "192.168.0.1",
                       "100.64.0.5"):
                self.assertTrue(bs.upnp_is_cgnat(ip), ip)
            self.assertFalse(bs.upnp_is_cgnat("203.0.113.8"))
            igd = FakeIGD(external_ip="100.64.0.5")
            try:
                info = bs.upnp_map(51234, ssdp_addr=igd.ssdp_addr, wait=0.5)
                self.assertFalse(info["available"])
                self.assertTrue(info["cgnat"])
            finally:
                igd.close()

        def test_cleanup_paths_delete_mapping(self):
            import signal as _signal
            igd = FakeIGD()
            prev = _signal.getsignal(_signal.SIGINT)
            try:
                registered = {}
                real_register = __import__("atexit").register
                __import__("atexit").register = lambda fn: registered.setdefault("fn", fn)
                try:
                    info = bs.upnp_map(51235, ssdp_addr=igd.ssdp_addr,
                                       wait=0.5)
                finally:
                    __import__("atexit").register = real_register
                self.assertTrue(info["available"])
                self.assertIn("fn", registered)
                # atexit path
                registered["fn"]()
                self.assertEqual(len(igd.deleted), 1)
                # signal path chains to a custom previous handler
                chained = []
                _signal.signal(_signal.SIGINT,
                               lambda s, f: chained.append(s))
                bs._upnp_install_handlers()
                handler = _signal.getsignal(_signal.SIGINT)
                info2 = bs.upnp_map(51236, ssdp_addr=igd.ssdp_addr, wait=0.5)
                handler(_signal.SIGINT, None)
                self.assertEqual(chained, [_signal.SIGINT])
                self.assertTrue(any(d.get("NewExternalPort") == "51236"
                                    for d in igd.deleted))
                # default previous handler: deletes, then KeyboardInterrupt
                _signal.signal(_signal.SIGINT, _signal.default_int_handler)
                bs._upnp_install_handlers()
                handler2 = _signal.getsignal(_signal.SIGINT)
                with self.assertRaises(KeyboardInterrupt):
                    handler2(_signal.SIGINT, None)
                bs.upnp_unmap(info)
                bs.upnp_unmap(info2)
            finally:
                _signal.signal(_signal.SIGINT, prev)
                igd.close()

    unittest.main(verbosity=2)
