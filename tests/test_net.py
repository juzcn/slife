"""Tests for slife.net — the address facts two components share.

memfiles' SSRF guard and sharefile's tunnel health read the same facts for
opposite reasons (one treats a fake-ip answer as "not evidence about the
destination", the other as "the proxy is in the path"), so the facts live in
one place and both ask the same question.

The facts are not interchangeable, and the split is what most of this file
tests: ``is_fake_ip_address`` is a **permission** (exact — the guard grants a
fetch through it) while ``is_fake_ip_answer`` is an **accusation** (coarse —
sharefile names the proxy with it).  Two tests hold the line between them, one
asserting the accusation stays coarse and one asserting the permission never
covers an address that is a real destination.

``resolver_uses_fake_ip`` is *measured*, so these tests stub both halves of it:
the socket answer for the probe itself, and the cached value for everything
that reads it.  No test here needs the machine's real DNS, which is the point —
one of them is behind a TUN proxy and another is not.  The exemption switch
reads slife.yaml, so those tests point the data dir at a throwaway file.
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


#: Addresses that **are** something: a host, a metadata endpoint, a multicast
#: group.  No fetch may reach one, and no pool a permission grants through may
#: contain one — a permission covering any of these is a hole in the guard,
#: which is what the last test in ``TestIsFakeIpAddress`` enforces.
#:
#: The benchmarking and documentation ranges are deliberately *not* here.  They
#: are unroutable, so they hold no destination, and they are exactly what the
#: pools are made of; they belong to the wider refusal set below.
_RANGES_A_PERMISSION_MUST_NOT_COVER = (
    "127.0.0.1", "::1",                          # loopback
    "10.0.0.1", "172.16.0.1", "192.168.1.1",     # LAN
    "169.254.169.254", "fe80::1",                # link-local, cloud metadata
    "fd00::1", "fd00:ec2::254", "fd20:ce::254",  # a ULA LAN host, the metadata pair
    "::ffff:127.0.0.1",                          # loopback, IPv6 spelling
    "100.64.0.1",                                # CGNAT (what Tailscale uses)
    # multicast: Python's is_global counts it as global, and no fetch
    # can reach a multicast address, so it is excluded by hand
    "224.0.0.1", "ff02::1",
)

#: Everything ``is_public_address`` must refuse — the set above plus the
#: unroutable space a fetch has no business in either.  A pool address is
#: unroutable *and* granted, so it appears here and not above; that difference
#: is the one the two predicates are allowed to have.
_RANGES_A_FETCH_MUST_NOT_REACH = (
    *_RANGES_A_PERMISSION_MUST_NOT_COVER,
    "198.18.0.1", "2001:2::127",                 # benchmarking space
    "2001:db8::1", "0.0.0.0",
)


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
        for address in _RANGES_A_FETCH_MUST_NOT_REACH:
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


    def test_the_accusation_is_coarse_on_purpose(self, fake_ip_machine):
        """A lying resolver makes every non-public address read as one —
        including a LAN host and AWS's IPv6 metadata address, which no proxy
        answered with.  That is the right error for an *accusation* (the hint
        it adds is still true of the machine) and the wrong one for a
        permission, which is why the guard grants through
        ``is_fake_ip_address`` instead."""
        assert net.is_fake_ip_answer("192.168.1.1")
        assert net.is_fake_ip_answer("fd00:ec2::254")
        assert not net.is_fake_ip_address("192.168.1.1")
        assert not net.is_fake_ip_address("fd00:ec2::254")


class TestIsFakeIpAddress:
    """The permission: pool membership alone, no resolver consulted.

    Each range here is one the ecosystem ships or one measured on a machine
    that was failing: Clash's RFC 2544 pool, mihomo's per-profile RFC 5180
    pool, the ULA pool Clash Verge's DNS template carries, sing-box's
    documented v6 range.
    """

    def test_the_pools_proxies_synthesise_from(self):
        for address in (
            "198.18.0.1", "198.18.0.104", "198.19.255.255",   # RFC 2544, v4
            "2001:2::127",                                    # RFC 5180, v6
            "fdfe:dcba:9876::20", "fc00::1",                  # the ULA pools
        ):
            assert net.is_fake_ip_address(address), address

    def test_one_destination_two_spellings(self):
        """An IPv4-mapped IPv6 address is judged as the IPv4 address it
        carries — which is what the OS routes it as, so the two spellings of
        one pool address must not get two answers."""
        assert net.is_fake_ip_address(ipaddress.ip_address("::ffff:198.18.0.1"))
        assert net.is_fake_ip_address("::ffff:198.18.0.104")

    def test_just_outside_a_pool_is_not_in_it(self):
        """The ends matter: a permission that is one bit too wide covers a
        neighbour of the pool."""
        for address in (
            "198.17.255.255", "198.20.0.1",      # either side of 198.18.0.0/15
            "2001:3::1",                         # the next /48 after 2001:2::/48
            "fdfe:dcba:9877::1",                 # the next /48 after the ULA pool
            "fc40::1",                           # just above fc00::/18
        ):
            assert not net.is_fake_ip_address(address), address

    def test_unparseable_answers_false(self):
        assert not net.is_fake_ip_address("")
        assert not net.is_fake_ip_address("198.18.0.1.5")
        assert not net.is_fake_ip_address(None)

    def test_no_pool_holds_an_address_that_is_a_real_destination(self):
        """The invariant that is the whole point of the list being exact: a
        permission may never cover loopback, LAN, link-local, metadata or
        multicast space.  This is the test that would catch a future
        ``fd00::/8`` (AWS's ``fd00:ec2::254`` and GCP's ``fd20:ce::254`` are
        ULA) or a widened ``10/8`` — the one mistake that turns the guard's
        exemption into the hole it exists to close."""
        for address in _RANGES_A_PERMISSION_MUST_NOT_COVER:
            assert not net.is_fake_ip_address(address), address

    def test_a_pool_address_is_still_not_a_destination(self):
        """And the two facts do not collapse into one: an address the
        permission grants is still not globally routable.  If a pool entry ever
        becomes public, the pool is wrong, not this predicate."""
        for address in ("198.18.0.1", "2001:2::127", "fdfe:dcba:9876::20"):
            assert net.is_fake_ip_address(address), address
            assert not net.is_public_address(address), address


class TestFakeIpExempt:
    """The switch over the whole question — ``net.fake_ip_exempt`` in
    slife.yaml, read by the guard on each pool-shaped refusal."""

    @staticmethod
    def _config(monkeypatch, tmp_path, text: str | None) -> None:
        """Point the data dir at *text* as slife.yaml (None = no file)."""
        monkeypatch.setenv("SLIFE_DATA_DIR", str(tmp_path))
        path = tmp_path / "slife.yaml"
        if text is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(text, encoding="utf-8")

    def test_an_absent_section_is_off(self, monkeypatch, tmp_path):
        self._config(monkeypatch, tmp_path, "agent:\n  max_iterations: 5\n")
        assert net.fake_ip_exempt_policy() == "off"
        assert net.fake_ip_exempt() is False

    def test_a_config_it_cannot_read_is_off(self, monkeypatch, tmp_path):
        """It grants a fetch, so a switch nothing can read must not."""
        self._config(monkeypatch, tmp_path, None)
        assert net.fake_ip_exempt() is False

    def test_on_exempts_on_an_honest_resolver_too(
        self, honest_resolver, monkeypatch, tmp_path
    ):
        """``on`` is the escape hatch for a proxy the probe cannot see, so it
        must not defer to the measurement."""
        self._config(monkeypatch, tmp_path, "net:\n  fake_ip_exempt: on\n")
        assert net.fake_ip_exempt() is True

    def test_off_refuses_on_a_lying_resolver_too(
        self, fake_ip_machine, monkeypatch, tmp_path
    ):
        self._config(monkeypatch, tmp_path, "net:\n  fake_ip_exempt: off\n")
        assert net.fake_ip_exempt() is False

    def test_auto_follows_the_measurement(self, monkeypatch, tmp_path):
        self._config(monkeypatch, tmp_path, "net:\n  fake_ip_exempt: auto\n")
        monkeypatch.setattr(net, "_resolver_fakes", True)
        assert net.fake_ip_exempt() is True
        monkeypatch.setattr(net, "_resolver_fakes", False)
        assert net.fake_ip_exempt() is False

    def test_an_unrecognised_value_is_off(self, monkeypatch, tmp_path):
        self._config(monkeypatch, tmp_path, "net:\n  fake_ip_exempt: maybe\n")
        assert net.fake_ip_exempt_policy() == "off"
        assert net.fake_ip_exempt() is False

    def test_the_switch_never_changes_what_a_pool_is(self, monkeypatch, tmp_path):
        """Whether an address is synthetic is a fact; whether a fetch may be
        aimed at one is the policy.  Answering the first with the second is how
        a permission ends up telling a caller something about the machine that
        is not true."""
        self._config(monkeypatch, tmp_path, "net:\n  fake_ip_exempt: on\n")
        assert net.is_fake_ip_address("198.18.0.1")
        assert not net.is_fake_ip_address("192.168.1.1")


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
