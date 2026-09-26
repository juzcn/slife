"""Network address facts that more than one component has to recognise.

A local proxy in **fake-ip** mode (Clash / Mihomo / sing-box — what a machine
behind a subscription proxy almost always runs) answers *public* hostnames
with synthetic addresses drawn from pools Python classifies as private or
reserved.  Two components need to know those pools, and they want **opposite
things** from them:

* ``url_save``'s SSRF guard **exempts** them.  They are the proxy's front door
  for public hosts, not LAN / metadata infrastructure, so refusing them would
  make that guard reject every URL whenever a fake-ip resolver is the system
  DNS.
* sharefile's tunnel health **flags** them.  An edge address that lands in one
  is being intercepted by such a proxy — which is what cuts cloudflared's
  long-lived control connection to Cloudflare and makes the tunnel flap every
  30-60 s while answering every published link with HTTP 530 in the gaps.

Same pools, opposite verdicts, so they are declared here once rather than
restated per component.  Nothing here decides anything: it answers "is this
address synthetic?", and the caller owns what that means for it.
"""

from __future__ import annotations

import ipaddress

#: Pools a fake-ip resolver synthesises addresses from.  The IPv4 side has one
#: ecosystem default: ``198.18.0.0/15``, RFC 2544 benchmarking space, which
#: Clash's ``fake-ip-range`` (``198.18.0.1/16``) sits inside.  The IPv6 side
#: has none — sing-box answers from ``fdfe:dcba:9876::/48`` (the ULA pool Clash
#: Verge ships in its own DNS template), while mihomo takes a per-profile
#: ``fake-ip-range6``: the profile on this machine sets ``2001:2::0/64``, RFC
#: 5180 benchmarking space.  Both v6 ranges are listed.
#:
#: A range belongs here when no real host answers from it (both benchmarking
#: ranges are unroutable, and nobody points a DNS answer at ULA) and no
#: cloud-metadata address sits inside it — IPv6 metadata (``fd00:ec2::254``)
#: is ULA, but outside all three.  Adding a range is a one-line change, and
#: the SSRF guard names this module in its refusal, so a proxy configured
#: with some other pool says so instead of just failing.
FAKE_IP_NETS: tuple[
    ipaddress.IPv4Network | ipaddress.IPv6Network, ...
] = (
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("2001:2::/48"),
    ipaddress.ip_network("fdfe:dcba:9876::/48"),
)


def is_fake_ip(address: object) -> bool:
    """Whether *address* is one a fake-ip resolver synthesised.

    Accepts an IP literal as a string or an ``ipaddress`` object.  Anything
    unparseable — a hostname, a malformed value — answers False: a caller
    asking about something that is not an address is not asking a question
    this can answer yes to.
    """
    try:
        ip = ipaddress.ip_address(address)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return False
    return any(ip in net for net in FAKE_IP_NETS)
