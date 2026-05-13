"""Dynamic runtime registries.

The wrapper must absorb OPC UA address-space drift (new/removed types, nodes,
namespaces) while i3X clients hold the server live. We use the immutable
snapshot + atomic swap pattern so readers never observe partial state:

    reader thread writer thread
    ------------------ --------------------
    snap = reg.get() new = dict(old)
                       new[k] = ...
    use snap async-lock: reg._ref = new

Each registry exposes a zero-lock read path (`snapshot()`) and a lock-guarded
write path (`replace(...)` / `update(...)`). Structural hashes key types so
unchanged definitions are not rebuilt.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from i3xua.core.neutral import (
    ElementRef,
    NamespaceInfo,
    NodeDescriptor,
    TypeDescriptor,
)

K = TypeVar("K")
V = TypeVar("V")


# ---------------------------------------------------------------- structural hash


def _canonical(payload: Any) -> str:
    """Stable, type-independent JSON for hashing. Sorts keys recursively."""
    return json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))


def type_structural_hash(
    source_node_id: str,
    fields: Iterable[tuple[str, str, int, bool]],
    version: str | None = None,
) -> str:
    """sha256 of (NodeId, sorted fields[name, dataType NodeId, ValueRank, IsOptional]).

    Unchanged type definitions hash identically across re-browses, so we can skip
    rebuilding Pydantic models when nothing changed.
    """
    payload = {
        "nodeId": source_node_id,
        "version": version,
        "fields": sorted([list(f) for f in fields]),
    }
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


# ---------------------------------------------------------------- generic snapshot


@dataclass(frozen=True, slots=True)
class Diff(Generic[K]):
    added: frozenset[K]
    removed: frozenset[K]
    unchanged: frozenset[K]


class SnapshotRegistry(Generic[K, V]):
    """Generic immutable-snapshot dict.

    Reads are lockless (just grab `_ref`). Writes copy + swap under an
    asyncio.Lock so there's at most one writer at a time.
    """

    __slots__ = ("_lock", "_ref", "_thread_lock")

    def __init__(self) -> None:
        self._ref: dict[K, V] = {}
        self._lock = asyncio.Lock()
        # Sync mutex for contexts that don't have a running loop (e.g. tests).
        self._thread_lock = threading.Lock()

    def snapshot(self) -> dict[K, V]:
        # Returns the live reference; callers must treat it as read-only.
        return self._ref

    def __len__(self) -> int:
        return len(self._ref)

    def __contains__(self, key: object) -> bool:
        return key in self._ref

    def get(self, key: K) -> V | None:
        return self._ref.get(key)

    async def replace(self, new_items: dict[K, V]) -> None:
        """Atomically swap the entire registry contents."""
        async with self._lock:
            self._ref = dict(new_items)

    async def update(self, added: dict[K, V], removed: Iterable[K] = ()) -> Diff[K]:
        """Copy-on-write apply of a delta. Returns the resulting diff."""
        async with self._lock:
            old = self._ref
            new = dict(old)
            for k in removed:
                new.pop(k, None)
            new.update(added)
            self._ref = new
            old_keys = frozenset(old)
            new_keys = frozenset(new)
            return Diff(
                added=new_keys - old_keys,
                removed=old_keys - new_keys,
                unchanged=new_keys & old_keys,
            )

    def replace_sync(self, new_items: dict[K, V]) -> None:
        """Same as replace(), for synchronous tests or startup before the loop exists."""
        with self._thread_lock:
            self._ref = dict(new_items)


# ---------------------------------------------------------------- namespaces


class NamespaceRegistry(SnapshotRegistry[str, NamespaceInfo]):
    """Keyed by `NamespaceInfo.i3x_uri` (`<uri>#connection=<name>`)."""

    async def reconcile(self, infos: Iterable[NamespaceInfo]) -> Diff[str]:
        infos_list = list(infos)
        desired = {ns.i3x_uri: ns for ns in infos_list}
        removed = [k for k in self._ref if k not in desired]
        return await self.update(added=desired, removed=removed)


# ---------------------------------------------------------------- types


@dataclass(frozen=True, slots=True)
class RegisteredType:
    descriptor: TypeDescriptor
    model: Any  # Pydantic BaseModel subclass produced by create_model


class TypeRegistry:
    """Indexed by structural hash AND by `<connection>!<source NodeId>`.

    `reconcile()` takes the latest set of `TypeDescriptor`s plus a model-builder
    callback. For types whose structural hash is unchanged, the prior model is
    reused (no `create_model` call). The by-hash and by-source-id dicts are
    swapped atomically together so readers see one consistent view.
    """

    __slots__ = ("_by_hash", "_by_source", "_lock")

    def __init__(self) -> None:
        self._by_hash: dict[str, RegisteredType] = {}
        self._by_source: dict[str, str] = {}  # "<conn>!<nodeid>" -> hash
        self._lock = asyncio.Lock()

    def by_hash(self) -> dict[str, RegisteredType]:
        return self._by_hash

    def by_source(self) -> dict[str, str]:
        return self._by_source

    def get_by_source(self, connection: str, source_node_id: str) -> RegisteredType | None:
        h = self._by_source.get(ElementRef(connection, source_node_id).as_id())
        return self._by_hash.get(h) if h else None

    async def reconcile(
        self,
        descriptors: Iterable[TypeDescriptor],
        *,
        build_model: ModelBuilder,
    ) -> Diff[str]:
        desc_list = list(descriptors)
        async with self._lock:
            new_by_hash = dict(self._by_hash)
            new_by_source: dict[str, str] = {}
            touched: set[str] = set()

            for d in desc_list:
                touched.add(d.structural_hash)
                key = ElementRef(d.connection, d.source_node_id).as_id()
                new_by_source[key] = d.structural_hash
                if d.structural_hash in new_by_hash:
                    continue
                model = build_model(d)
                new_by_hash[d.structural_hash] = RegisteredType(descriptor=d, model=model)

            # Drop hashes no longer referenced by any source.
            referenced = set(new_by_source.values())
            for h in list(new_by_hash):
                if h not in referenced:
                    new_by_hash.pop(h)

            old_hashes = frozenset(self._by_hash)
            self._by_hash = new_by_hash
            self._by_source = new_by_source
            new_hashes = frozenset(new_by_hash)
            return Diff(
                added=new_hashes - old_hashes,
                removed=old_hashes - new_hashes,
                unchanged=new_hashes & old_hashes,
            )


# Callback signature the adapter supplies to reconcile()
from typing import Protocol  # noqa: E402


class ModelBuilder(Protocol):
    def __call__(self, descriptor: TypeDescriptor) -> Any: ...


# ---------------------------------------------------------------- instances


class InstanceRegistry(SnapshotRegistry[str, NodeDescriptor]):
    """Keyed by i3X elementId (`<connection>!<NodeId>`)."""

    async def reconcile(self, nodes: Iterable[NodeDescriptor]) -> Diff[str]:
        nodes_list = list(nodes)
        # Key by ``<conn>!<wire_node_id>`` so the registry agrees with what
        # the wire surface emits — wire_node_id is the human-readable
        # display-path for numeric NodeIds, the canonical NodeId form
        # otherwise.
        desired = {
            ElementRef(n.connection, n.wire_node_id or n.node_id).as_id(): n for n in nodes_list
        }
        removed = [k for k in self._ref if k not in desired]
        return await self.update(added=desired, removed=removed)

    def children_of(
        self,
        parent_element_id: str | None,
        relationship: str | None = None,
    ) -> list[NodeDescriptor]:
        """Direct children per `parent_node_id` link. O(n) — registries are small
        enough that a pre-indexed map isn't worth the extra invalidation cost.

        `relationship` filters by the OPC UA ref-type name stamped on each
        child's `parent_relationship` (`"HasComponent"`, `"HasProperty"`, ...).
        `None` returns every child regardless of ref type — matches the pre-
        behavior so existing callers stay correct.
        """
        parent_conn: str | None
        parent_nid: str | None
        if parent_element_id is None:
            parent_conn = parent_nid = None
        else:
            ref = ElementRef.parse(parent_element_id)
            parent_conn, parent_nid = ref.connection, ref.node_id
        # Filter by `parent_wire_node_id` — matches what the elementId on
        # the wire encodes. Falls back to raw `parent_node_id` when wire
        # isn't populated (test fixtures, pre-browse-pipeline state).
        return [
            n
            for n in self._ref.values()
            if (n.parent_wire_node_id or n.parent_node_id) == parent_nid
            and (parent_conn is None or n.connection == parent_conn)
            and (relationship is None or n.parent_relationship == relationship)
        ]
