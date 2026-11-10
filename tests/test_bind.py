"""Bind addresses of the portal and metrics: loopback by default, any interface on request (like ComfyUI --listen)."""
import contextlib
import http.client
import io
import unittest
from unittest import mock

from hydraweave import bind, cli
from hydraweave.config import ConfigError
from hydraweave.web import server as portal


class AddressTests(unittest.TestCase):
    def test_addresses_and_listen_values(self):
        self.assertEqual(bind.address("0.0.0.0"), "0.0.0.0")
        self.assertEqual(bind.address("::"), "::")
        self.assertEqual(bind.address("[::1]"), "::1")
        self.assertEqual(bind.address("192.0.2.10"), "192.0.2.10")
        self.assertEqual(bind.listen("0.0.0.0:18700"), {"address": "0.0.0.0", "port": 18700})
        self.assertEqual(bind.listen("[::]:18700"), {"address": "::", "port": 18700})
        self.assertEqual(bind.listen("10.0.0.5", default_port=18700), {"address": "10.0.0.5", "port": 18700})
        self.assertEqual(bind.listen("::", default_port=1), {"address": "::", "port": 1})

    def test_bad_values_are_refused(self):
        for value in ("", "localhost", "example.com", "224.0.0.1", "fe80::1%eth0", "1.2.3", None, "0.0.0.0 "):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bind.address(value)
        for value in ("0.0.0.0", "0.0.0.0:x", "0.0.0.0:70000", "[::1", "[::1]18700", "a:b:c"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bind.listen(value)

    def test_browser_address(self):
        self.assertEqual(bind.browse_address("0.0.0.0"), "127.0.0.1")
        self.assertEqual(bind.browse_address("::"), "::1")
        self.assertEqual(bind.browse_address("192.0.2.10"), "192.0.2.10")
        self.assertEqual(bind.url_host("::1", 5), "[::1]:5")


class HostTests(unittest.TestCase):
    names = {"halo-1", "halo-1.local"}

    def test_loopback_bind_keeps_the_historical_rule(self):
        self.assertTrue(bind.host_allowed("127.0.0.1:18700", "127.0.0.1", 18700, self.names))
        self.assertTrue(bind.host_allowed("localhost:18700", "127.0.0.1", 18700, self.names))
        for header in ("192.0.2.10:18700", "halo-1:18700", "127.0.0.1:18701", "127.0.0.1", "evil.example:18700"):
            with self.subTest(header=header):
                self.assertFalse(bind.host_allowed(header, "127.0.0.1", 18700, self.names))

    def test_every_interface_accepts_ip_literals_and_own_names_only(self):
        for header in ("192.0.2.10:18700", "10.1.2.3:18700", "[fd00::5]:18700", "halo-1:18700",
                       "HALO-1.local:18700", "localhost:18700"):
            with self.subTest(header=header):
                self.assertTrue(bind.host_allowed(header, "0.0.0.0", 18700, self.names))
        for header in ("evil.example:18700", "192.0.2.10:1", "192.0.2.10", "[fd00::5:18700", ""):
            with self.subTest(header=header):
                self.assertFalse(bind.host_allowed(header, "0.0.0.0", 18700, self.names))

    def test_one_interface_accepts_its_own_address(self):
        self.assertTrue(bind.host_allowed("192.0.2.10:18700", "192.0.2.10", 18700, self.names))
        self.assertFalse(bind.host_allowed("192.0.2.11:18700", "192.0.2.10", 18700, self.names))


class PortalTests(unittest.TestCase):
    def test_portal_on_every_interface_answers_a_lan_host_and_refuses_foreign_names(self):
        with portal.create_server(0, listen="0.0.0.0") as server:
            self.assertTrue(server.lan)
            self.assertTrue(server.origin.startswith("http://127.0.0.1:"))
            import threading
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
            thread.start()
            try:
                port = server.server_port
                for host, status in ((f"192.0.2.10:{port}", 200), (f"evil.example:{port}", 403)):
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                    connection.putrequest("GET", "/", skip_host=True)
                    connection.putheader("Host", host)
                    connection.endheaders()
                    response = connection.getresponse()
                    response.read()
                    connection.close()
                    self.assertEqual(response.status, status, host)
            finally:
                server.shutdown()
                thread.join(5)

    def test_default_portal_is_loopback(self):
        with portal.create_server(0) as server:
            self.assertFalse(server.lan)
            self.assertEqual(server.listen_address, "127.0.0.1")

    def test_cli_passes_listen_and_port(self):
        with mock.patch.object(portal, "serve") as serve:
            self.assertEqual(cli.main(["ui", "--listen", "0.0.0.0", "--port", "18800", "--no-browser"]), 0)
        self.assertEqual(serve.call_args.args, (18800,))
        self.assertEqual(serve.call_args.kwargs["listen"], "0.0.0.0")
        with mock.patch.object(portal, "serve") as serve:
            self.assertEqual(cli.main(["ui", "--no-browser"]), 0)
        self.assertEqual((serve.call_args.args, serve.call_args.kwargs["listen"]), ((None,), None))
        with mock.patch.object(portal, "serve") as serve, contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(cli.main(["ui", "--listen", "example.com", "--no-browser"]), 0)
        serve.assert_not_called()

    def test_serve_warns_on_a_lan_bind(self):
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        fake.lan, fake.listen_address, fake.server_port, fake.origin = True, "0.0.0.0", 18700, "http://127.0.0.1:18700"
        fake.serve_forever.side_effect = KeyboardInterrupt
        err, out = io.StringIO(), io.StringIO()
        with mock.patch.object(portal, "create_server", return_value=fake) as create, \
                mock.patch.object(portal, "start_telemetry"), mock.patch("hydraweave.nodepoll.start"), \
                mock.patch("hydraweave.nodegate.portal"), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            portal.serve(18700, no_browser=True, listen="0.0.0.0")
        self.assertEqual(create.call_args.kwargs["listen"], "0.0.0.0")
        self.assertIn("without authentication", err.getvalue())
        self.assertIn("0.0.0.0:18700", err.getvalue())


class ConfigTests(unittest.TestCase):
    def test_ui_listen_accepts_any_interface(self):
        from tests.test_config import example
        from hydraweave import config
        for value in ("0.0.0.0:18700", "192.0.2.10:18700", "[::]:18700", "127.0.0.1:18700"):
            obj = example()
            obj.setdefault("pc", {})["ui_listen"] = value
            with self.subTest(value=value):
                config.parse(obj)
        obj = example()
        obj.setdefault("pc", {})["ui_listen"] = "localhost:18700"
        with self.assertRaises(ConfigError):
            config.parse(obj)


if __name__ == "__main__":
    unittest.main()
