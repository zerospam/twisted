# Copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.

"""
Tests for L{twisted.internet.iocpreactor}.
"""

import errno
import time
from array import array
from struct import pack
from socket import AF_INET6, AF_INET, SOCK_STREAM, SOL_SOCKET, error, socket
from unittest import skipIf

from zope.interface.verify import verifyClass

from twisted.trial.unittest import TestCase
from twisted.python.log import msg
from twisted.internet.interfaces import IPushProducer

try:
    from twisted.internet.iocpreactor import iocpsupport as _iocp, tcp, udp
    from twisted.internet.iocpreactor.reactor import (
        IOCPReactor,
        EVENTS_PER_LOOP,
        KEY_NORMAL,
    )
    from twisted.internet.iocpreactor.interfaces import IReadWriteHandle
    from twisted.internet.iocpreactor.const import SO_UPDATE_ACCEPT_CONTEXT
    from twisted.internet.iocpreactor.abstract import FileHandle
except ImportError:
    skip = "This test only applies to IOCPReactor"

try:
    socket(AF_INET6, SOCK_STREAM).close()
except error as e:
    ipv6Skip = True
    ipv6SkipReason = str(e)

else:
    ipv6Skip = False
    ipv6SkipReason = ""


class SupportTests(TestCase):
    """
    Tests for L{twisted.internet.iocpreactor.iocpsupport}, low-level reactor
    implementation helpers.
    """

    def _acceptAddressTest(self, family, localhost):
        """
        Create a C{SOCK_STREAM} connection to localhost using a socket with an
        address family of C{family} and assert that the result of
        L{iocpsupport.get_accept_addrs} is consistent with the result of
        C{socket.getsockname} and C{socket.getpeername}.
        """
        msg("family = %r" % (family,))
        port = socket(family, SOCK_STREAM)
        self.addCleanup(port.close)
        port.bind(("", 0))
        port.listen(1)
        client = socket(family, SOCK_STREAM)
        self.addCleanup(client.close)
        client.setblocking(False)
        try:
            client.connect((localhost, port.getsockname()[1]))
        except error as e:
            self.assertIn(e.errno, (errno.EINPROGRESS, errno.EWOULDBLOCK))

        server = socket(family, SOCK_STREAM)
        self.addCleanup(server.close)
        buff = array("B", b"\0" * 256)
        self.assertEqual(0, _iocp.accept(port.fileno(), server.fileno(), buff, None))

        for _ in range(3):
            # Calling setsockopt after _iocp.accept might fail for both IPv4
            # and IPV6 with [Errno 10057] A request to send or receive ...
            # This is when ERROR_IO_PENDING is returned and means that the
            # socket is not yet ready and accept will be handled via the
            # callback event.
            # For this test, with the purpose of keeping the test simple,
            # we don't implement the event callback.
            # The event callback functionality is tested via the high level
            # tests for general reactor API.
            # We retry twice as retrying once is not enough.
            try:
                server.setsockopt(
                    SOL_SOCKET, SO_UPDATE_ACCEPT_CONTEXT, pack("P", port.fileno())
                )
                break
            except error as socketError:
                # getattr is used below to make mypy happy.
                if socketError.errno == getattr(errno, 'WSAENOTCONN'):
                    # Without a sleep here even retrying 20 times will fail.
                    # This should allow other threads to execute.
                    time.sleep(0.1)
                    pass
                else:
                    # Not the excepted error so we re-raise the error without
                    # retying.
                    raise
            # Second try also failed so we just re-raise the error.
            raise

        self.assertEqual(
            (family, client.getpeername()[:2], client.getsockname()[:2]),
            _iocp.get_accept_addrs(server.fileno(), buff),
        )

    def test_ipv4AcceptAddress(self):
        """
        L{iocpsupport.get_accept_addrs} returns a three-tuple of address
        information about the socket associated with the file descriptor passed
        to it.  For a connection using IPv4:

          - the first element is C{AF_INET}
          - the second element is a two-tuple of a dotted decimal notation IPv4
            address and a port number giving the peer address of the connection
          - the third element is the same type giving the host address of the
            connection
        """
        self._acceptAddressTest(AF_INET, "127.0.0.1")

    @skipIf(ipv6Skip, ipv6SkipReason)
    def test_ipv6AcceptAddress(self):
        """
        Like L{test_ipv4AcceptAddress}, but for IPv6 connections.
        In this case:

          - the first element is C{AF_INET6}
          - the second element is a two-tuple of a hexadecimal IPv6 address
            literal and a port number giving the peer address of the connection
          - the third element is the same type giving the host address of the
            connection
        """
        self._acceptAddressTest(AF_INET6, "::1")


class IOCPReactorTests(TestCase):
    def test_noPendingTimerEvents(self):
        """
        Test reactor behavior (doIteration) when there are no pending time
        events.
        """
        ir = IOCPReactor()
        ir.wakeUp()
        self.assertFalse(ir.doIteration(None))

    def test_reactorInterfaces(self):
        """
        Verify that IOCP socket-representing classes implement IReadWriteHandle
        """
        self.assertTrue(verifyClass(IReadWriteHandle, tcp.Connection))
        self.assertTrue(verifyClass(IReadWriteHandle, udp.Port))

    def test_fileHandleInterfaces(self):
        """
        Verify that L{Filehandle} implements L{IPushProducer}.
        """
        self.assertTrue(verifyClass(IPushProducer, FileHandle))

    def test_maxEventsPerIteration(self):
        """
        Verify that we don't lose an event when more than EVENTS_PER_LOOP
        events occur in the same reactor iteration
        """

        class FakeFD:
            counter = 0

            def logPrefix(self):
                return "FakeFD"

            def cb(self, rc, bytes, evt):
                self.counter += 1

        ir = IOCPReactor()
        fd = FakeFD()
        event = _iocp.Event(fd.cb, fd)
        for _ in range(EVENTS_PER_LOOP + 1):
            ir.port.postEvent(0, KEY_NORMAL, event)
        ir.doIteration(None)
        self.assertEqual(fd.counter, EVENTS_PER_LOOP)
        ir.doIteration(0)
        self.assertEqual(fd.counter, EVENTS_PER_LOOP + 1)
