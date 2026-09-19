"""Tests for slife.net — the fake-ip address pools.

Two components need these pools for opposite reasons (memfiles' SSRF guard
exempts them, sharefile's tunnel health flags them), so the pools live in one
place and both ask the same question.
"""

import ipaddress

import pytest; pytestmark = pytest.mark.unit

from slife.net import FAKE_IP_NETS, is_fake_ip


class TestIsFakeIp:
    def test_clash_ipv4_pool(self):
        """198.18.0.0/15 is RFC 2544 space; Clash's default fake-ip-range
        (198.18.0.1/16) sits inside it.  The address from the live cloudflared
        log is the regression case."""
        assert is_fake_ip("198.18.0.32")
        assert is_fake_ip("198.18.0.1")
        assert is_fake_ip("198.19.255.254")   # the top of /15
        assert is_fake_ip(ipaddress.ip_address("198.18.0.33"))

    def test_sing_box_ipv6_pool(self):
        """fdfe:dcba:9876::/48 — the address a fake-ip resolver answered the
        edge hostname with on the machine that reported this."""
        assert is_fake_ip("fdfe:dcba:9876::20")
        assert is_fake_ip("fdfe:dcba:9876::1")

    def test_real_public_addresses_are_not_fake(self):
        """The whole value of the check: a genuine edge address must not be
        flagged, or every healthy tunnel reports a proxy interception."""
        assert not is_fake_ip("104.16.0.1")
        assert not is_fake_ip("198.20.0.1")     # just outside the /15
        assert not is_fake_ip("2606:4700::1")

    def test_private_and_loopback_are_not_fake(self):
        """These are a DIFFERENT problem (the SSRF guard's real targets) and
        must not be waved through as synthetic."""
        assert not is_fake_ip("127.0.0.1")
        assert not is_fake_ip("192.168.1.1")
        assert not is_fake_ip("169.254.169.254")   # cloud metadata
        assert not is_fake_ip("fd00::1")           # a real ULA, not sing-box's

    def test_unparseable_answers_false(self):
        """A hostname or a malformed value is not a question this can answer
        yes to — and an empty string is the "never observed" case."""
        assert not is_fake_ip("")
        assert not is_fake_ip("region1.v2.argotunnel.com")
        assert not is_fake_ip("not-an-ip")
        assert not is_fake_ip(None)

    def test_pools_are_declared_once(self):
        """Both consumers read this tuple; a third copy is the thing this
        module exists to prevent."""
        assert len(FAKE_IP_NETS) == 2
        assert {str(n) for n in FAKE_IP_NETS} == {
            "198.18.0.0/15", "fdfe:dcba:9876::/48",
        }
