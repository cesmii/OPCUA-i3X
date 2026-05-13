"""Live regression test: BrowseResponse-driven walker stays cheap.

Asserts an upper bound on `browse_initial_calls` after a full browse
against each server. The current numbers are:

  Reference Server (~7,400 instances, browse_variable_properties=True): ≤ 200 initial Browse calls (measured ~150)
  Kepware (~740,000 instances, browse_variable_properties=False): ≤ 200 initial Browse calls

These ceilings are deliberately tight. If a future change reintroduces
leaf-browsing or per-parent attribute reads, this test catches it before
we ship a 50x slowdown.

Endpoints:
  OPCUA_REFERENCE_TEST_ENDPOINT default opc.tcp://Mac-5999.lan:62541/Quickstarts/ReferenceServer
  OPCUA_KEPWARE_TEST_ENDPOINT default opc.tcp://192.168.64.4:49320

The Reference Server default uses the machine's mDNS `.lan` name so TCP
resolves to localhost. The _UriAwareClient pattern (same as production) first
calls connect_and_get_server_endpoints() to discover the server's real
ApplicationUri, then overrides ServerUri before CreateSession so strict
servers don't reject with BadServerUriInvalid.

Run locally with:
    uv run pytest -m live tests/live/test_browse_round_trip_count.py -v

CI skips these (no @pytest.mark.live infrastructure).
"""

from __future__ import annotations

import os

import pytest
from asyncua import ua

from i3xua.adapters.asyncua.browse import BrowseConfig, browse
from i3xua.adapters.asyncua.upstream import _AsyncuaBrowseSource
from i3xua.adapters.asyncua.uri_aware import (
    _UriAwareClient,
    pick_matching_endpoint,
)

REFERENCE_DEFAULT = "opc.tcp://Mac-5999.lan:62541/Quickstarts/ReferenceServer"
KEPWARE_DEFAULT = "opc.tcp://192.168.64.4:49320"


def _ref_endpoint() -> str:
    return os.environ.get("OPCUA_REFERENCE_TEST_ENDPOINT", REFERENCE_DEFAULT)


def _kep_endpoint() -> str:
    return os.environ.get("OPCUA_KEPWARE_TEST_ENDPOINT", KEPWARE_DEFAULT)


async def _connect(url: str) -> _UriAwareClient:
    """Connect using the same _UriAwareClient + ApplicationUri-override pattern
    as the production AsyncuaUpstreamDataSource._pre_connect().

    Steps:
      1. connect_and_get_server_endpoints() — TCP only, no session — to learn
         the server's real ApplicationUri.
      2. Override ServerUri on the _UriAwareClient instance.
      3. connect() — CreateSession now carries the correct ServerUri.
    """
    client = _UriAwareClient(url=url, timeout=60)
    endpoints = await client.connect_and_get_server_endpoints()
    match = pick_matching_endpoint(endpoints, policy="None", mode=ua.MessageSecurityMode.None_)
    if match is not None:
        client._override_server_uri = match.Server.ApplicationUri
    await client.connect()
    return client


@pytest.mark.live
async def test_reference_server_round_trip_ceiling() -> None:
    """Reference Server full browse stays under 200 initial Browse calls.

    Uses browse_variable_properties=True since Reference Server's content
    is rich in Method.InputArguments / TwoStateVariable properties / Variable
    EURange/EngineeringUnits — we WANT those discovered. The default False
    is for Kepware-class tag-heavy servers where Variable properties are
    not exposed.
    """
    client = await _connect(_ref_endpoint())
    try:
        source = _AsyncuaBrowseSource(client)
        cfg = BrowseConfig(browse_variable_properties=True)
        result = await browse(source, connection="reference", cfg=cfg)
        assert len(result.nodes) >= 7000, (
            f"Reference Server browse looks truncated: only {len(result.nodes)} "
            f"nodes (expected ~7,372 with browse_variable_properties=True). "
            f"Pre-optimization baseline was 7,372 instances."
        )
        assert source.counters.browse_initial_calls <= 200, (
            f"Reference Server browse_initial_calls = "
            f"{source.counters.browse_initial_calls}; expected ≤ 200. "
            f"Measured baseline (with browse_variable_properties=True): ~150. "
            f"A regression past 200 usually means the walker is re-browsing "
            f"already-visited nodes or has reintroduced redundant attribute reads."
        )
    finally:
        await client.disconnect()


@pytest.mark.live
async def test_kepware_round_trip_ceiling() -> None:
    """Kepware full browse stays under 200 initial Browse calls.

    The full address space is ~740,000 instances. A correct walker
    discovers all of them. Use a soft floor (>100,000) to detect silent
    truncation — the previous floors before the optimization landed
    were 23,460 and 76,560.

    Uses browse_variable_properties=False explicitly to mirror
    config-kep.yaml — Kepware tags don't expose meaningful HasProperty
    children, and skipping them is what gives us the ~50x speedup.
    """
    client = await _connect(_kep_endpoint())
    try:
        source = _AsyncuaBrowseSource(client)
        cfg = BrowseConfig(browse_variable_properties=False)  # explicit; matches config-kep.yaml
        result = await browse(source, connection="kepware", cfg=cfg)
        assert len(result.nodes) > 100_000, (
            f"Kepware browse looks truncated: only {len(result.nodes)} nodes "
            f"(expected ~740,000). Verify CP draining is working and the "
            f"asyncua Client timeout is sufficient."
        )
        assert source.counters.browse_initial_calls <= 200, (
            f"Kepware browse_initial_calls = "
            f"{source.counters.browse_initial_calls}; expected ≤ 200. "
            f"Walker is doing more batched browses than the leaf-skip "
            f"optimization should require."
        )
    finally:
        await client.disconnect()
