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
  machine.  It must, however, still refuse a destination that really is LAN or
  metadata.
* sharefile's tunnel health **flags** it.  A synthetic edge address means the
  proxy is carrying — and cutting — cloudflared's control connection, which is
  what makes the tunnel flap every 30-60s while answering every published link
  with HTTP 530 in the gaps.

So the facts live here once, and each caller owns what they mean for it.  There
are four, and the direction of error each is allowed to make is part of what it
is:

``is_public_address``
    Whether an address is globally routable, by Python's own classification
    against the IANA special-purpose registries.  A fact about the address.

``is_fake_ip_address``
    Whether an address lies in a pool a fake-ip proxy synthesises from
    (:data:`FAKE_IP_NETS`).  A fact about the address, and a **permission** —
    the SSRF guard grants a fetch through it.  Coarse is not allowed here: one
    entry holding a real destination is a hole in the guard.  The refresh risk
    runs the other way, so the list is exact and a pool it omits keeps that
    pool's literals refused.

``resolver_uses_fake_ip``
    Whether this host's resolver answers names that do not exist.  A fact about
    the machine, and **measured rather than configured**: a fake-ip resolver
    answers whatever it is asked, so a name that cannot exist is answered too,
    while an honest resolver says NXDOMAIN.

``is_fake_ip_answer``
    Whether a synthetic address is what a lying resolver just answered with.
    An **accusation**, read by sharefile to name the proxy as the cause of a
    flapping tunnel, and deliberately coarse: on a lying resolver every
    non-public address reads as one.  Harmless where the verdict is a warning,
    and expensive where it is a permission — which is why the guard never
    grants through it.

``fake_ip_exempt``
    The one **switch** over the whole question: whether a fetch may be aimed at
    a synthetic address at all (``net.fake_ip_exempt`` in slife.yaml).

Why the resolver fact is measured and not a list of the pools proxies
synthesise from: the pool is per-profile configuration (Clash's
``fake-ip-range``, mihomo's ``fake-ip-range6``, sing-box's own defaults), so a
list is a patch per pool — and each pool that was not listed failed *every*
URL fetch on that machine.  Asking the resolver what it does needs no list, and
no future pool needs a line.  That reasoning holds for the **name** question,
where a missing entry costs every fetch on the machine.  It does not hold for
the **literal** question, where a missing entry costs one URL shape and the
list can therefore be exact.  Both live here: :data:`FAKE_IP_NETS` answers "may
a fetch be aimed at this address", ``resolver_uses_fake_ip`` answers "is this
answer evidence about the destination".
"""

from __future__ import annotations

import ipaddress
import socket
import uuid

#: Pools a fake-ip resolver synthesises addresses from.
#:
#: An entry belongs here when (1) the range is benchmarking space or a vendor's
#: *fixed* synthetic prefix, (2) **no loopback, LAN-as-deployed or cloud
#: metadata address sits inside it**, and (3) it is a pool observed on a
#: machine this repo runs on, or a default the ecosystem ships.  Criterion (2)
#: is the one that binds: it is why the IPv6 side can never be ``fd00::/8`` —
#: AWS's metadata address ``fd00:ec2::254`` and GCP's ``fd20:ce::254`` are ULA,
#: and ULA is also exactly where a real LAN lives.
#:
#: * ``198.18.0.0/15`` — RFC 2544 benchmarking space, the ecosystem's one IPv4
#:   default (Clash's ``fake-ip-range`` ``198.18.0.1/16`` sits inside).
#: * ``2001:2::/48`` — RFC 5180 benchmarking space, the IPv6 twin.  The
#:   machine this was written on runs mihomo with ``fake-ip-range6 2001:2::0/64``.
#: * ``fdfe:dcba:9876::/48`` — the ULA pool Clash Verge ships in its own DNS
#:   template (``fdfe:dcba:9876::1/64``), also sing-box's example.  Specific
#:   enough that no network is assigned it.
#: * ``fc00::/18`` — the IPv6 range sing-box documents for fake-ip.
#:
#: Exempting a pool is not free: a proxy's pool also holds the proxy's **own**
#: addresses (on the machine this was measured, the TUN interface is
#: ``198.18.0.1/30`` and Clash's DNS ``198.18.0.2``), so a literal aimed at one
#: of those reaches the local machine rather than the proxy's reverse mapping —
#: see ``url_save``'s guard, which carries the residual.  Every other address in
#: these ranges is unreachable as a destination: either the proxy maps it back
#: to a name, or nothing answers.
FAKE_IP_NETS: tuple[
    ipaddress.IPv4Network | ipaddress.IPv6Network, ...
] = (
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("2001:2::/48"),
    ipaddress.ip_network("fdfe:dcba:9876::/48"),
    ipaddress.ip_network("fc00::/18"),
)

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


def is_fake_ip_address(address: object) -> bool:
    """Whether *address* lies in a pool a fake-ip proxy synthesises from.

    A **permission** — the SSRF guard grants a fetch through this — so it says
    yes to :data:`FAKE_IP_NETS` and to nothing else.  In particular it never
    says yes to loopback, LAN, link-local or metadata space, which is what
    makes it usable where :func:`is_fake_ip_answer` is not.

    An IPv4-mapped IPv6 address is judged as the IPv4 address it carries
    (``::ffff:198.18.0.1`` is ``198.18.0.1``; one destination, two spellings).
    Anything unparseable is not a pool address, the same way it is not a public
    one.
    """
    try:
        ip = ipaddress.ip_address(address)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    # The version compare is explicit because containment across families is
    # not a question with one answer across Python versions, and "is this in
    # the v4 pool" must not depend on which one is running.
    return any(
        ip.version == network.version and ip in network
        for network in FAKE_IP_NETS
    )


def fake_ip_exempt() -> bool:
    """Whether a fetch may be aimed at an address from a fake-ip pool.

    The switch over the whole question, from ``net.fake_ip_exempt`` in
    slife.yaml — ``off``, ``auto`` or ``on``.  An absent section is ``off``:

    ``off``
        Never exempt.  A non-public literal is refused exactly as it was before
        the pools were a permission.  Names are unaffected either way, since
        that question is answered by the measurement, not by this.
    ``auto``
        Exempt exactly while this machine's resolver is measured to lie — the
        premise that a pool address is the proxy's own synthetic space rather
        than a host.
    ``on``
        Exempt regardless.  For a proxy that is in the path but invisible to
        the probe: a resolver that answers the probe name honestly (a
        ``fake-ip-filter`` that lists it), or a probe that ran before the proxy
        came up, which the cached measurement never revisits.

    Read per call rather than cached — a config edit should not be a restart —
    and an unreadable file reads as ``off``, because this grants a fetch and a
    switch nothing can read must not grant one.
    """
    policy = fake_ip_exempt_policy()
    if policy == "on":
        return True
    if policy == "auto":
        return resolver_uses_fake_ip()
    return False


def fake_ip_exempt_policy() -> str:
    """The configured policy — ``"off"``, ``"auto"`` or ``"on"``.

    Read straight off slife.yaml, because the guard runs in the memfiles plugin
    process, which holds no :class:`~slife.config.Config`; the section's one
    parser is still :class:`~slife.config.NetConfig`, so the plugin and the
    host cannot disagree about what the file says.
    """
    try:
        from slife.config import NetConfig
        from slife.paths import get_config_path
        from slife.tools._config_io import read_config

        section = read_config(get_config_path()).get("net")
    except Exception:  # noqa: BLE001 — an unreadable switch must not grant
        return "off"
    return NetConfig.from_dict(section).fake_ip_exempt


def resolver_uses_fake_ip() -> bool:
    """Whether this host's resolver answers names that do not exist."""
    global _resolver_fakes
    if _resolver_fakes is None:
        _resolver_fakes = _probe_fake_ip_resolver()
    return _resolver_fakes


def is_fake_ip_answer(address: object) -> bool:
    """Whether a lying resolver answered *address* for a name.

    An **accusation**, not a permission.  Read by sharefile's tunnel health to
    name the proxy behind a flapping tunnel, and deliberately coarse: on a
    lying resolver any non-public address reads as one, including a LAN or
    metadata address the resolver never answered with.  That is the right error
    for an accusation — a false positive adds a hint that is still true of the
    machine ("a fake-ip proxy is in the path"), while a false negative leaves
    the tunnel flapping with no cause named.  It is the wrong error for a
    permission, so no guard may grant through this; :func:`is_fake_ip_address`
    is the permission, and it is exact.

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
