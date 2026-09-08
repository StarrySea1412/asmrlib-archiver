from __future__ import annotations

import socket
import sys
import unittest

from desktop_main import pick_free_port


class PickFreePortTests(unittest.TestCase):
    def test_returns_preferred_port_when_free(self) -> None:
        port = pick_free_port(preferred=58765)
        self.assertGreaterEqual(port, 58765)

    def test_skips_occupied_port(self) -> None:
        # A plain listener without special socket options: the old code used
        # SO_REUSEADDR, which on Windows silently double-binds this port and
        # routes the app's requests to the other process (SeeFlow symptom).
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
            blocker.bind(("127.0.0.1", 58766))
            blocker.listen(1)
            port = pick_free_port(preferred=58766)
        self.assertNotEqual(port, 58766)

    def test_exclusive_probe_never_double_binds(self) -> None:
        if sys.platform != "win32":
            self.skipTest("SO_EXCLUSIVEADDRUSE semantics are Windows-specific")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
            blocker.bind(("127.0.0.1", 58767))
            blocker.listen(1)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                with self.assertRaises(OSError):
                    probe.bind(("127.0.0.1", 58767))


if __name__ == "__main__":
    unittest.main()
