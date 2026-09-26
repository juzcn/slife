"""Tests for slife.net — the address facts two components share.

memfiles' SSRF guard and sharefile's tunnel health read the same facts for
opposite reasons (one treats a fake-ip answer as "not evidence about the
destination", the other as "the proxy is in the path"), so the facts live in
one place and both ask the same question.

``resolver_uses_fake_ip`` is *measured*, so these tests stub both halves of it:
the socket answer for the probe itself, and the cached value for everything
that reads it.  No test here needs the machine's real DNS, which is the point —
one of them is behind a TUN proxy and another is not.
"""

import ipaddress
import socket

import pytest; pytestmark = pytest.mark.unit

from slife import net


@pytest.fixture
def fake_ip_machine(monkeypatch):
    """The cached answer for a machine whose proxy answers every name."""
    monkeypatch.setattr(net, "_resolver_fakes", True)


@pytest.fixture
def honest_resolver(monkeypatch):
    """The cached answer for a machine whose resolver tells the truth."""
    monkeypatch.setattr(net, "_resolver_fakes", False)


class TestIsPublicAddress:
    def test_public_addresses(self):
        assert net.is_public_address("8.8.8.8")
        assert net.is_public_address("1.1.1.1")
        assert net.is_public_address("104.16.0.1")      # a cloudflared edge
        assert net.is_public_address("2606:4700::1")
        assert net.is_public_address(ipaddress.ip_address("93.184.216.34"))

    def test_the_ranges_a_fetch_must_not_reach(self):
        """Refused by the address alone, on a lying resolver as much as on an
        honest one — this is the part of the guard that never depends on DNS."""
        for address in (
            "127.0.0.1", "::1",                    # loopback
            "10.0.0.1", "172.16.0.1", "192.168.1.1",   # LAN
            "169.254.169.254", "fe80::1",          # link-local, cloud metadata
            "fd00::1", "fd00:ec2::254",            # a ULA LAN host, IPv6 metadata
            "::ffff:127.0.0.1",                    # loopback, IPv6 spelling
            "100.64.0.1",                          # CGNAT (what Tailscale uses)
            "198.18.0.1", "2001:2::127",           # benchmarking space
            "2001:db8::1", "0.0.0.0",
            # multicast: Python's is_global counts it as global, and no fetch
            # can reach a multicast address, so it is excluded by hand
            "224.0.0.1", "ff02::1",
        ):
            assert not net.is_public_address(address), address

    def test_unparseable_answers_false(self):
        """A hostname, a malformed value, and the "" that means "never
        observed" are not questions this can answer yes to."""
        assert not net.is_public_address("")
        assert not net.is_public_address("region1.v2.argotunnel.com")
        assert not net.is_public_address("not-an-ip")
        assert not net.is_public_address(None)


class TestIsFakeIpAnswer:
    def test_the_addresses_a_proxy_answers_public_names_with(self, fake_ip_machine):
        """Each range here was measured on a machine that was failing: Clash's
        RFC 2544 pool, the cloudflared edge from the live log, mihomo's
        per-profile RFC 5180 pool, sing-box's ULA default."""
        assert net.is_fake_ip_answer("198.18.0.32")
        assert net.is_fake_ip_answer("198.19.255.254")   # the top of the /15
        assert net.is_fake_ip_answer("2001:2::127")
        assert net.is_fake_ip_answer("fdfe:dcba:9876::20")

    def test_a_real_edge_address_is_not_one(self, fake_ip_machine):
        """Otherwise every healthy tunnel reports a proxy interception."""
        assert not net.is_fake_ip_answer("104.16.0.1")
        assert not net.is_fake_ip_answer("2606:4700::1")

    def test_an_unobserved_edge_is_not_one(self, fake_ip_machine):
        """``""`` is "never observed", never "intercepted"."""
        assert not net.is_fake_ip_answer("")

    def test_an_honest_resolver_is_never_answering(self, honest_resolver):
        """On a machine whose resolver tells the truth the same address is a
        LAN host — a different problem, and not this one."""
        assert not net.is_fake_ip_answer("198.18.0.32")
        assert not net.is_fake_ip_answer("192.168.1.1")


class TestResolverUsesFakeIp:
    """The probe: a name that cannot exist, asked of the resolver."""

    @staticmethod
    def _answers(monkeypatch, *addresses: str) -> None:
        monkeypatch.setattr(net, "_resolver_fakes", None)
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **kw: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0))
                for addr in addresses
            ],
        )

    @staticmethod
    def _nxdomain(monkeypatch) -> None:
        monkeypatch.setattr(net, "_resolver_fakes", None)

        def raise_gaierror(*a, **kw):
            raise socket.gaierror("Name or service not known")

        monkeypatch.setattr(socket, "getaddrinfo", raise_gaierror)

    def test_answered_with_a_non_public_address(self, monkeypatch):
        """What this machine's TUN resolver does: <uuid>.com -> 198.18.1.53."""
        self._answers(monkeypatch, "198.18.1.53", "2001:2::12f")
        assert net.resolver_uses_fake_ip() is True

    def test_nxdomain_is_an_honest_resolver(self, monkeypatch):
        self._nxdomain(monkeypatch)
        assert net.resolver_uses_fake_ip() is False

    def test_a_hijacking_resolver_is_not_a_fake_ip_one(self, monkeypatch):
        """An NXDOMAIN hijacker answers with its own ad server — a real,
        globally routable address.  Reading that as fake-ip would relax every
        caller's guard on a machine whose answers are otherwise honest."""
        self._answers(monkeypatch, "93.184.216.34")
        assert net.resolver_uses_fake_ip() is False

    def test_one_probe_per_process(self, monkeypatch):
        """Every guard call asks this question; the network is asked once."""
        monkeypatch.setattr(net, "_resolver_fakes", None)
        probes: list[int] = []
        monkeypatch.setattr(
            net, "_probe_fake_ip_resolver", lambda: probes.append(1) or True
        )
        assert net.resolver_uses_fake_ip() is True
        assert net.resolver_uses_fake_ip() is True
        assert len(probes) == 1

    def test_the_probe_name_cannot_exist(self, monkeypatch):
        """Whatever it is answered with, the name must be one no honest
        resolver has a record for — and one no fake-ip-filter lists, which is
        why it is random and under .com rather than .invalid or .lan."""
        monkeypatch.setattr(net, "_resolver_fakes", None)
        asked: list[str] = []

        def record(host, *a, **kw):
            asked.append(host)
            raise socket.gaierror("Name or service not known")

        monkeypatch.setattr(socket, "getaddrinfo", record)
        net.resolver_uses_fake_ip()
        assert len(asked) == 1
        assert asked[0].endswith(".com")
        assert len(asked[0].removesuffix(".com")) == 32   # a fresh uuid4 hex
