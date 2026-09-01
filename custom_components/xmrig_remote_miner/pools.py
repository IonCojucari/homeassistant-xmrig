"""The list of pools a rig may be pointed at, and how it is written down.

The list lives in the config entry rather than in the miner. XMRig knows the
pool it is connected to and nothing about the alternatives, so the choice has
to be described somewhere Home Assistant can read before it asks the miner to
move -- and the config flow is where the user already is.

One pool per line, either

    pool.supportxmr.com:443
    Nanopool = xmr-eu1.nanopool.org:14433

so that the common case costs no syntax and a nicer name is available when the
host is unreadable, which most of them are.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# XMRig accepts a bare host:port and also a stratum URL. `connection.pool` in
# the summary reports the bare form, so the scheme is stripped before anything
# is compared -- otherwise `stratum+ssl://host:443` and `host:443`, which are
# the same pool, would never match and the select would show no selection.
_SCHEME = re.compile(r"^[a-z0-9+.\-]+://", re.IGNORECASE)


def normalize(url: str) -> str:
    """A pool address reduced to what identifies it: host and port."""
    return _SCHEME.sub("", url.strip()).rstrip("/").lower()


@dataclass(frozen=True)
class Pool:
    """One entry of the choice list."""

    label: str
    url: str

    @property
    def key(self) -> str:
        return normalize(self.url)


def parse(raw: str | None) -> list[Pool]:
    """Read the configured text into pools, dropping what cannot be one.

    Silent about bad lines on purpose: this is parsed on every entity build,
    long after the form that could have complained is gone. The config flow
    validates the same text while the user is still looking at it, which is
    where a mistake is worth a message.
    """
    pools: list[Pool] = []
    seen: set[str] = set()
    for line in (raw or "").splitlines():
        pool = parse_line(line)
        if pool is None or pool.key in seen:
            continue
        seen.add(pool.key)
        pools.append(pool)
    return pools


def parse_line(line: str) -> Pool | None:
    """One line into a pool, or None if there is nothing on it.

    The label is split on the *first* "=", because a URL may contain one --
    some pools carry options in a query string -- while a label containing one
    is a label asking for trouble.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    label, sep, url = line.partition("=")
    if not sep:
        label, url = "", line
    label, url = label.strip(), url.strip()
    if not url:
        return None
    return Pool(label=label or url, url=url)
