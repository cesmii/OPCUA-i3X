"""One-off probe: PropertyType usage on ua-ref-server + DataTypeDictionary
on Kepware. Standalone — does not touch the wrapper's adapter code, just
asyncua. Intended to be deleted after the investigation closes.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from asyncua import ua

from i3xua.adapters.asyncua.connection import _extract_san_uri
from i3xua.adapters.asyncua.uri_aware import (
    _UriAwareClient,
    pick_matching_endpoint,
)

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s")

REF_SERVER = "opc.tcp://localhost:62541/Quickstarts/ReferenceServer"
KEPWARE = "opc.tcp://192.168.64.4:49320"


def _safe_str(v: Any) -> str:
    try:
        return str(v)
    except Exception as exc:
        return f"<unstr: {exc}>"


async def _read_attrs(node: Any, attrs: list[ua.AttributeIds]) -> dict[str, Any]:
    """Read a list of attributes via uaclient.read so we can capture per-attr
    StatusCode (asyncua's per-node read short-circuits on first Bad)."""
    client = node.session
    items = [ua.ReadValueId(NodeId_=node.nodeid, AttributeId=a) for a in attrs]
    results = await client.read(ua.ReadParameters(NodesToRead=items))
    out: dict[str, Any] = {}
    for attr, dv in zip(attrs, results, strict=True):
        sc = dv.StatusCode_
        if sc.is_good():
            val = dv.Value.Value if dv.Value is not None else None
            out[attr.name] = _safe_str(val)
        else:
            out[attr.name] = f"<{sc.name}>"
    return out


_VAR_ATTRS: list[ua.AttributeIds] = [
    ua.AttributeIds.NodeClass,
    ua.AttributeIds.BrowseName,
    ua.AttributeIds.DisplayName,
    ua.AttributeIds.Description,
    ua.AttributeIds.DataType,
    ua.AttributeIds.ValueRank,
    ua.AttributeIds.ArrayDimensions,
    ua.AttributeIds.AccessLevel,
    ua.AttributeIds.UserAccessLevel,
    ua.AttributeIds.MinimumSamplingInterval,
    ua.AttributeIds.Historizing,
    ua.AttributeIds.Value,
]


async def _dump_node(node: Any, depth: int = 0) -> None:
    indent = "  " * depth
    nc = await node.read_node_class()
    bn = await node.read_browse_name()
    print(f"{indent}- {node.nodeid.to_string()}  [{nc.name}]  BrowseName={bn.Name}")
    attrs = await _read_attrs(node, _VAR_ATTRS)
    for k, v in attrs.items():
        if v == "<BadAttributeIdInvalid>":
            continue
        print(f"{indent}    {k}: {v[:100]}")
    # HasProperty children — what makes a node "have properties"
    try:
        props = await node.get_references(
            refs=ua.NodeId(46, 0), direction=ua.BrowseDirection.Forward
        )
    except Exception as exc:
        print(f"{indent}    <HasProperty browse failed: {exc}>")
        return
    for p in props:
        prop_node = node.session.get_node(p.NodeId)
        nc2 = await prop_node.read_node_class()
        bn2 = await prop_node.read_browse_name()
        # The TypeDefinition (BaseDataVariableType / PropertyType / etc.)
        td_refs = await prop_node.get_references(
            refs=ua.NodeId(40, 0), direction=ua.BrowseDirection.Forward
        )
        td = td_refs[0].NodeId.to_string() if td_refs else "<no TypeDef>"
        print(
            f"{indent}    └── HasProperty → {p.NodeId.to_string()} [{nc2.name}] "
            f"BrowseName={bn2.Name}  TypeDef={td}"
        )
        if nc2 == ua.NodeClass.Variable:
            pa = await _read_attrs(prop_node, _VAR_ATTRS)
            for k, v in pa.items():
                if v == "<BadAttributeIdInvalid>":
                    continue
                print(f"{indent}        {k}: {v[:100]}")


async def probe_ref_server() -> None:
    print("=" * 80)
    print(f"REFERENCE SERVER  {REF_SERVER}")
    print("=" * 80)
    # Discovery on a separate, throwaway client so its watchdog can't
    # interfere with the actual probe session.
    disco = _UriAwareClient(REF_SERVER)
    eps = await disco.connect_and_get_server_endpoints()
    await disco.disconnect()
    chosen = pick_matching_endpoint(eps, policy="None", mode=ua.MessageSecurityMode.None_)
    if chosen is None:
        chosen = eps[0]

    client = _UriAwareClient(REF_SERVER)
    client._override_server_uri = chosen.Server.ApplicationUri
    await client.connect()
    try:
        # Find a few classic AnalogItemType + TwoStateVariableType instances —
        # they should have HasProperty children (EURange, EngineeringUnits,
        # TrueState/FalseState, etc.) typed by PropertyType.
        print("\n[Boilers/Boiler1/Output] — AnalogItemType, expect EURange/EngineeringUnits")
        try:
            output = client.get_node("ns=4;i=1242")
            await _dump_node(output)
        except Exception as exc:
            print(f"  failed: {exc}")

        print("\n[Demo/Static/Scalar/Boolean] — bare BaseDataVariableType")
        try:
            scalar = client.get_node("ns=2;s=Demo.Static.Scalar.Boolean")
            await _dump_node(scalar)
        except Exception as exc:
            print(f"  failed: {exc}")

        # AlarmsServer or similar — TwoStateVariable shape
        print("\n[Server/ServerStatus] — has property children")
        try:
            ss = client.get_node("ns=0;i=2256")
            await _dump_node(ss)
        except Exception as exc:
            print(f"  failed: {exc}")

        # Count usage: how many distinct PropertyType-typed variables are
        # under the typical instance roots?
        print("\n[count] PropertyType usage under Server.Diagnostics + Demo")
        for root_id, label in (
            ("ns=0;i=2274", "Server.ServerDiagnostics"),
            ("ns=2;s=Demo", "Demo"),
        ):
            try:
                root = client.get_node(root_id)
                count_pt = 0
                count_bdvt = 0
                count_other = 0
                queue: list[Any] = [root]
                visited: set[str] = set()
                while queue and len(visited) < 500:
                    node = queue.pop(0)
                    nid = node.nodeid.to_string()
                    if nid in visited:
                        continue
                    visited.add(nid)
                    children = await node.get_references(
                        refs=ua.NodeId(33, 0),  # HierarchicalReferences
                        direction=ua.BrowseDirection.Forward,
                    )
                    for c in children:
                        cn = client.get_node(c.NodeId)
                        nc = await cn.read_node_class()
                        if nc == ua.NodeClass.Variable:
                            td_refs = await cn.get_references(
                                refs=ua.NodeId(40, 0),
                                direction=ua.BrowseDirection.Forward,
                            )
                            if td_refs:
                                td = td_refs[0].NodeId.to_string()
                                if td == "i=68":
                                    count_pt += 1
                                elif td == "i=63":
                                    count_bdvt += 1
                                else:
                                    count_other += 1
                        if nc in (ua.NodeClass.Object, ua.NodeClass.Variable):
                            queue.append(cn)
                print(
                    f"  {label}: PropertyType={count_pt}  BaseDataVariableType={count_bdvt}  "
                    f"other={count_other} (visited={len(visited)})"
                )
            except Exception as exc:
                print(f"  {label}: failed: {exc}")
    finally:
        await client.disconnect()


async def probe_kepware() -> None:
    print("\n" + "=" * 80)
    print(f"KEPWARE  {KEPWARE}")
    print("=" * 80)
    client = _UriAwareClient(KEPWARE)
    cert_path = Path("certs/client.der")
    key_path = Path("certs/client.pem")
    san_uri = _extract_san_uri(cert_path)
    if san_uri:
        client.application_uri = san_uri
    eps = await client.connect_and_get_server_endpoints()
    # Kepware advertises SignAndEncrypt + Basic256Sha256
    chosen = next(
        e
        for e in eps
        if e.SecurityMode == ua.MessageSecurityMode.SignAndEncrypt
        and "Basic256Sha256" in e.SecurityPolicyUri
    )
    client._override_server_uri = chosen.Server.ApplicationUri
    # Kepware needs the server cert too; write the discovered cert to a
    # temp file and load it.
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".der", delete=False) as f:
        f.write(chosen.ServerCertificate)
        server_cert_path = f.name
    sec_str = (
        f"Basic256Sha256,SignAndEncrypt,{cert_path},{key_path},{server_cert_path}"
    )
    await client.set_security_string(sec_str)
    await client.connect()
    try:
        print("\n[NamespaceArray]")
        ns_array = await client.get_namespace_array()
        for i, u in enumerate(ns_array):
            print(f"  ns={i}: {u}")

        # DataTypeDictionary — `OPCBinarySchema_TypeSystem` is i=92.
        # Children of ns=0;i=92 are one per namespace that has structured
        # types; each child is a Variable holding the binary schema XML.
        print("\n[DataTypeDictionary @ ns=0;i=92] — children (one per namespace)")
        dtd = client.get_node("ns=0;i=92")
        children = await dtd.get_children()
        for c in children:
            nc = await c.read_node_class()
            bn = await c.read_browse_name()
            print(f"  - {c.nodeid.to_string()} [{nc.name}] BrowseName={bn.Name}")

        # Also: TypesFolder / DataTypes
        print("\n[Types/DataTypes @ i=24] — top-level subtypes")
        dts = client.get_node("ns=0;i=24")
        for c in await dts.get_children():
            bn = await c.read_browse_name()
            print(f"  - {c.nodeid.to_string()} BrowseName={bn.Name}")

        # Pick a representative Kepware tag: probe its full attribute set
        # AND HasProperty children. Kepware's typical layout:
        # Channel1.Device1.Tag1 lives under ns=2.
        print("\n[probe a Kepware tag fullhouse — first Variable found under ns=2]")
        objects = client.get_node("ns=0;i=85")
        # BFS to find the first Variable in ns=2
        queue: list[Any] = [objects]
        found: Any = None
        seen: set[str] = set()
        while queue and not found:
            node = queue.pop(0)
            nid = node.nodeid.to_string()
            if nid in seen:
                continue
            seen.add(nid)
            for c in await node.get_children():
                cnid = c.nodeid.to_string()
                if cnid in seen:
                    continue
                nc = await c.read_node_class()
                if nc == ua.NodeClass.Variable and cnid.startswith("ns=2;"):
                    found = c
                    break
                if nc == ua.NodeClass.Object:
                    queue.append(c)
            if len(seen) > 200:
                break
        if found is None:
            print("  no Variable found in ns=2 within 200 nodes")
        else:
            await _dump_node(found)
    finally:
        await client.disconnect()


async def main() -> None:
    await probe_ref_server()
    await probe_kepware()


if __name__ == "__main__":
    asyncio.run(main())
