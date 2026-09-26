"""Network facts that more than one component has to recognise.

A local proxy in **fake-ip** mode — what a machine running Clash / Mihomo /
sing-box TUN has — answers *every* hostname with an address of its own
choosing, so a resolved address stops being evidence about where a connection
will land.  slife is expected to work normally behind such a proxy: the one
thing it cannot do is keep a long-lived connection, which is sharefile's
problem and is reported rather than fixed.

Two components care about the same addresses and want **opposite things** from
them:

* ``url_save``'s SSRF guard must not read a synthetic answer as "this is LAN or
  metadata infrastructure" — believing that refused every public URL on such a
  machine.
* sharefile's tunnel health **flags** it.  A synthetic edge address means the
  proxy is carrying — and cutting — cloudflared's control connection, which is
  what makes the tunnel flap every 30-60s while answering every published link
  with HTTP 530 in the gaps.

So the facts live here once, and each caller owns what they mean for it:

``is_public_address``
    Whether an address is globally routable, by Python's own classification
    against the IANA special-purpose registries.  A fact about the address.

``resolver_uses_fake_ip``
    Whether this host's resolver answers names that do not exist.  A fact about
    the machine, and **measured rather than configured**: a fake-ip resolver
    answers whatever it is asked, so a name that cannot exist is answered too,
    while an honest resolver says NXDOMAIN.

``is_fake_ip_answer``
    Both of the above at once: an address no real destination can be, answered
    by a resolver that answers everything.

Why the resolver fact is measured and not a list of the pools proxies
synthesise from: the pool is per-profile configuration (Clash's
``fake-ip-range``, mihomo's ``fake-ip-range6``, sing-box's own defaults), so a
list is a patch per pool — and each pool that was not listed failed *every*
URL fetch on that machine.  Asking the resolver what it does needs no list,
and no future pool needs a line.
"""

from __future__ import annotations

import ipaddress
import socket
import uuid

#: Cached answer to :func:`resolver_uses_fake_ip`.  Whether the machine's
#: resolver lies is a property of the network, not of a request, so it is
#: measured once per process rather than on every guard call.
_resolver_fakes: bool | None = None


def is_public_address(address: object) -> bool:
    """Whether *address* is globally routable — a real destination.

    Takes an address as a string or an ``ipaddress`` object.  Anything
    unparseable — a hostname, a malformed value, the ``""`` that means "never
    observed" — answers False: a caller asking about something that is not an
    address is not asking a question this can answer yes to.

    ``is_global`` is Python's own reading of the IANA special-purpose
    registries, so it tracks them instead of being restated here — but it
    counts multicast as global (``224.0.0.0/4`` and ``ff00::/8`` are in no
    private list), and a multicast address is not a destination anything can
    be fetched from.
    """
    try:
        ip = ipaddress.ip_address(address)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return False
    return ip.is_global and not ip.is_multicast


def resolver_uses_fake_ip() -> bool:
    """Whether this host's resolver answers names that do not exist."""
    global _resolver_fakes
    if _resolver_fakes is None:
        _resolver_fakes = _probe_fake_ip_resolver()
    return _resolver_fakes


def is_fake_ip_answer(address: object) -> bool:
    """Whether *address* is an address a fake-ip resolver answered a name with.

    False for a public address, which is a real destination, and for anything
    unparseable — an edge address never observed is not an interception.
    """
    if not resolver_uses_fake_ip():
        return False
    try:
        ip = ipaddress.ip_address(address)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return False
    return not ip.is_global


def _probe_fake_ip_resolver() -> bool:
    """Ask the resolver for a name that cannot exist.

    A random name under ``.com``: no fake-ip-filter lists it, because filtering
    ``.com`` wholesale would break the proxy.  The answer must be a *non-public*
    address to count.  A resolver that hijacks NXDOMAIN answers with its own ad
    server — a real, globally routable address — and that is an annoying
    resolver, not a fake-ip one; reading it as fake-ip would relax every
    caller's guard on a machine whose answers are honest, so the narrower test
    is also the fail-closed one.
    """
    try:
        infos = socket.getaddrinfo(f"{uuid.uuid4().hex}.com", None)
    except OSError:
        return False
    return any(not is_public_address(info[4][0]) for info in infos)
