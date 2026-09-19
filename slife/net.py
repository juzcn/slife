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

#: Pools a fake-ip resolver synthesises addresses from.
#: ``198.18.0.0/15`` is RFC 2544 benchmarking space (Clash's default
#: ``fake-ip-range``, ``198.18.0.1/16``, sits inside it); ``fdfe:dcba:9876::/48``
#: is sing-box's default IPv6 pool.
FAKE_IP_NETS: tuple[
    ipaddress.IPv4Network | ipaddress.IPv6Network, ...
] = (
    ipaddress.ip_network("198.18.0.0/15"),
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
