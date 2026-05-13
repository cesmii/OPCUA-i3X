"""Async Python i3X client, ported from i3X-Explorer's src/api/{client,subscription}.ts.

Used by the pytest suite to drive our server for TDD. Supports the same v0/v1
autodetection, auth modes, SSE streams, and sync polling as the TS reference.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
from httpx_sse import aconnect_sse

from i3xua.i3x.types import (
    CreateSubscriptionResponse,
    GetSubscriptionsResponse,
    HistoricalValue,
    LastKnownValue,
    Namespace,
    ObjectInstance,
    ObjectType,
    RelationshipType,
    SubscriptionSummary,
    SyncResponseItem,
)

ApiVersion = Literal["v0", "v1"]


@dataclass(frozen=True, slots=True)
class BearerCredentials:
    token: str
    type: Literal["bearer"] = "bearer"


@dataclass(frozen=True, slots=True)
class BasicCredentials:
    username: str
    password: str
    type: Literal["basic"] = "basic"


Credentials = BearerCredentials | BasicCredentials


def _extract_vqt(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Parse either the flat `{value, quality, timestamp}` or the nested
    `{value: {Data: {Value, Quality, Timestamp}}}` shape emitted by some servers."""
    raw_value = payload.get("value")
    if isinstance(raw_value, Mapping):
        data = raw_value.get("Data")
        if isinstance(data, Mapping):
            return {
                "value": data.get("Value"),
                "quality": data.get("Quality"),
                "timestamp": data.get("Timestamp"),
            }
    return {
        "value": raw_value,
        "quality": payload.get("quality"),
        "timestamp": payload.get("timestamp"),
    }


def _normalize_v1_object(raw: Mapping[str, Any]) -> ObjectInstance:
    """v1 uses `typeElementId` (not `typeId`) and nests metadata under `metadata`.

    Uses `model_validate` so extras (description, relationships stashed at the
    top level for client convenience) pass through the model's `extra="allow"`
    without tripping mypy on the unrecognized kwargs.
    """
    metadata = raw.get("metadata") or {}
    return ObjectInstance.model_validate(
        {
            "elementId": str(raw.get("elementId", "")),
            "displayName": str(raw.get("displayName", "")),
            "typeElementId": str(raw.get("typeElementId") or raw.get("typeId") or ""),
            "parentId": raw.get("parentId"),
            "isComposition": bool(raw.get("isComposition", False)),
            "isExtended": bool(raw.get("isExtended", False)),
            "description": metadata.get("description"),
            "relationships": raw.get("relationships") or metadata.get("relationships"),
            "sourceRelationship": raw.get("sourceRelationship"),
            "metadata": metadata or None,
        }
    )


def _bulk_results(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, Mapping) and "results" in raw:
        return list(raw["results"] or [])
    return []


class I3XClient:
    def __init__(
        self,
        base_url: str,
        credentials: Credentials | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._credentials = credentials
        self._api_version: ApiVersion = "v0"
        self._sync_seq: dict[str, int] = {}
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=timeout)

    # ------------------------------------------------------------------ lifecycle

    async def __aenter__(self) -> I3XClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ------------------------------------------------------------------ basics

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def api_version(self) -> ApiVersion:
        return self._api_version

    def _auth_header(self) -> dict[str, str]:
        creds = self._credentials
        if creds is None:
            return {}
        if creds.type == "bearer":
            return {"Authorization": f"Bearer {creds.token}"}
        raw = f"{creds.username}:{creds.password}".encode()
        return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: Mapping[str, str] | None = None,
    ) -> Any:
        url = f"{self._base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        headers.update(self._auth_header())

        resp = await self._http.request(method, url, headers=headers, json=body)
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {resp.status_code}: {resp.text}", request=resp.request, response=resp
            )
        data = resp.json() if resp.content else None
        # v1: auto-unwrap single-value envelope. Bulk envelopes have `results`
        # and are handled per-method.
        if (
            self._api_version == "v1"
            and isinstance(data, Mapping)
            and "result" in data
            and "results" not in data
        ):
            return data["result"]
        return data

    async def detect_version(self) -> ApiVersion:
        """Probe GET /info; set version to v1 on 2xx, else v0."""
        try:
            resp = await self._http.get(
                f"{self._base_url}/info",
                headers={"Accept": "application/json", **self._auth_header()},
            )
            self._api_version = "v1" if resp.is_success else "v0"
        except httpx.HTTPError:
            self._api_version = "v0"
        return self._api_version

    async def test_connection(self) -> bool:
        try:
            await self.detect_version()
            await self.get_namespaces()
            return True
        except httpx.HTTPError:
            return False

    # ------------------------------------------------------------------ discovery

    async def get_namespaces(self) -> list[Namespace]:
        data = await self._request("GET", "/namespaces")
        return [Namespace.model_validate(n) for n in (data or [])]

    async def get_object_types(self, namespace_uri: str | None = None) -> list[ObjectType]:
        params = {"namespaceUri": namespace_uri} if namespace_uri else None
        data = await self._request("GET", "/objecttypes", params=params)
        return [ObjectType.model_validate(t) for t in (data or [])]

    async def get_object_type(self, element_id: str) -> ObjectType:
        data = await self._request("GET", f"/objecttypes/{element_id}")
        return ObjectType.model_validate(data)

    async def get_relationship_types(
        self, namespace_uri: str | None = None
    ) -> list[RelationshipType]:
        params = {"namespaceUri": namespace_uri} if namespace_uri else None
        data = await self._request("GET", "/relationshiptypes", params=params)
        return [RelationshipType.model_validate(t) for t in (data or [])]

    async def get_objects(
        self,
        type_id: str | None = None,
        *,
        include_metadata: bool = False,
        root: bool = False,
    ) -> list[ObjectInstance]:
        params: dict[str, str] = {}
        if type_id:
            # v1 renamed typeId → typeElementId
            key = "typeElementId" if self._api_version == "v1" else "typeId"
            params[key] = type_id
        params["includeMetadata"] = (
            "true" if (self._api_version == "v1" or include_metadata) else "false"
        )
        if root and self._api_version == "v1":
            params["root"] = "true"

        raw = await self._request("GET", "/objects", params=params)
        raw_list = list(raw or [])
        if self._api_version == "v1":
            return [_normalize_v1_object(r) for r in raw_list]
        objs = [ObjectInstance.model_validate(r) for r in raw_list]
        if root:
            # v0 has no server-side root filter; parent "/" means root.
            objs = [o for o in objs if o.parentId == "/"]
        return objs

    async def get_object(
        self, element_id: str, *, include_metadata: bool = False
    ) -> ObjectInstance:
        params = {
            "includeMetadata": "true"
            if (self._api_version == "v1" or include_metadata)
            else "false"
        }
        raw = await self._request("GET", f"/objects/{element_id}", params=params)
        if self._api_version == "v1":
            return _normalize_v1_object(raw)
        return ObjectInstance.model_validate(raw)

    async def get_related_objects(
        self,
        element_id: str,
        relationship_type: str | None = None,
        *,
        include_metadata: bool = False,
    ) -> list[ObjectInstance]:
        if self._api_version == "v1":
            raw = await self._request(
                "POST",
                "/objects/related",
                body={
                    "elementIds": [element_id],
                    "relationshipType": relationship_type,
                    "includeMetadata": True,
                },
            )
            out: list[ObjectInstance] = []
            for item in _bulk_results(raw):
                if not item.get("success"):
                    continue
                result = item.get("result")
                if not isinstance(result, list):
                    continue
                for envelope in result:
                    inner = envelope.get("object") if isinstance(envelope, Mapping) else None
                    if inner is not None:
                        merged = {**inner, "sourceRelationship": envelope.get("sourceRelationship")}
                        out.append(_normalize_v1_object(merged))
                    elif isinstance(envelope, Mapping):
                        out.append(_normalize_v1_object(envelope))
            return out
        # v0
        raw = await self._request(
            "POST",
            "/objects/related",
            body={
                "elementIds": [element_id],
                "relationshiptype": relationship_type,
                "includeMetadata": include_metadata,
            },
        )
        return [ObjectInstance.model_validate(o) for o in (raw or [])]

    # ------------------------------------------------------------------ values

    async def get_value(self, element_id: str, *, max_depth: int = 1) -> LastKnownValue | None:
        values = await self.get_values([element_id], max_depth=max_depth)
        return values[0] if values else None

    async def get_values(
        self, element_ids: list[str], *, max_depth: int = 1
    ) -> list[LastKnownValue]:
        if self._api_version == "v1":
            raw = await self._request(
                "POST", "/objects/value", body={"elementIds": element_ids, "maxDepth": max_depth}
            )
            out: list[LastKnownValue] = []
            for item in _bulk_results(raw):
                if not item.get("success"):
                    continue
                result = item.get("result") or {}
                out.append(
                    LastKnownValue.model_validate(
                        {
                            "elementId": item["elementId"],
                            "value": result.get("value"),
                            "quality": result.get("quality") or "GoodNoData",
                            "timestamp": result.get("timestamp") or "",
                            "parentId": None,
                            "isComposition": "components" in result,
                            "components": result.get("components"),
                        }
                    )
                )
            return out
        # v0
        raw = await self._request(
            "POST", "/objects/value", body={"elementIds": element_ids, "maxDepth": max_depth}
        )
        out = []
        for eid in element_ids:
            entry = (raw or {}).get(eid)
            data = (entry or {}).get("data") or []
            if data:
                vqt = _extract_vqt(data[0])
                out.append(LastKnownValue.model_validate({"elementId": eid, **vqt}))
        return out

    async def get_history(
        self,
        element_id: str,
        *,
        start_time: str | None = None,
        end_time: str | None = None,
        max_depth: int = 1,
    ) -> HistoricalValue:
        body: dict[str, Any] = {"elementIds": [element_id], "maxDepth": max_depth}
        if start_time is not None:
            body["startTime"] = start_time
        if end_time is not None:
            body["endTime"] = end_time

        if self._api_version == "v1":
            raw = await self._request("POST", "/objects/history", body=body)
            for item in _bulk_results(raw):
                if item.get("success") and item["elementId"] == element_id:
                    result = item.get("result") or {}
                    return HistoricalValue.model_validate(result)
            return HistoricalValue()
        # v0
        raw = await self._request("POST", "/objects/history", body=body)
        entry = (raw or {}).get(element_id)
        data = (entry or {}).get("data") or []
        return HistoricalValue(values=data)

    # ------------------------------------------------------------------ subscriptions

    async def get_subscriptions(self) -> GetSubscriptionsResponse:
        if self._api_version == "v1":
            # v1 has no list-all; callers track IDs from create().
            return GetSubscriptionsResponse(subscriptionIds=[])
        raw = await self._request("GET", "/subscriptions")
        return GetSubscriptionsResponse(
            subscriptionIds=[SubscriptionSummary.model_validate(s) for s in (raw or [])]
        )

    async def create_subscription(self) -> CreateSubscriptionResponse:
        raw = await self._request("POST", "/subscriptions", body={})
        return CreateSubscriptionResponse(
            subscriptionId=str(raw.get("subscriptionId")),
        )

    async def delete_subscription(self, subscription_id: str) -> None:
        if self._api_version == "v1":
            await self._request(
                "POST", "/subscriptions/delete", body={"subscriptionIds": [subscription_id]}
            )
        else:
            await self._request("DELETE", f"/subscriptions/{subscription_id}")
        self._sync_seq.pop(subscription_id, None)

    async def register_monitored_items(
        self, subscription_id: str, element_ids: list[str], *, max_depth: int = 1
    ) -> Any:
        if self._api_version == "v1":
            return await self._request(
                "POST",
                "/subscriptions/register",
                body={
                    "subscriptionId": subscription_id,
                    "elementIds": element_ids,
                    "maxDepth": max_depth,
                },
            )
        return await self._request(
            "POST",
            f"/subscriptions/{subscription_id}/register",
            body={"elementIds": element_ids, "maxDepth": max_depth},
        )

    async def unregister_monitored_items(self, subscription_id: str, element_ids: list[str]) -> Any:
        if self._api_version == "v1":
            return await self._request(
                "POST",
                "/subscriptions/unregister",
                body={"subscriptionId": subscription_id, "elementIds": element_ids},
            )
        return await self._request(
            "POST", f"/subscriptions/{subscription_id}/unregister", body={"elementIds": element_ids}
        )

    async def sync(self, subscription_id: str) -> list[SyncResponseItem]:
        if self._api_version == "v1":
            body: dict[str, Any] = {"subscriptionId": subscription_id}
            if subscription_id in self._sync_seq:
                body["lastSequenceNumber"] = self._sync_seq[subscription_id]
            raw = await self._request("POST", "/subscriptions/sync", body=body)
        else:
            raw = await self._request("POST", f"/subscriptions/{subscription_id}/sync")
        return self._parse_stream_items(subscription_id, raw or [])

    def _parse_stream_items(self, subscription_id: str, raw: list[Any]) -> list[SyncResponseItem]:
        items: list[SyncResponseItem] = []
        for entry in raw:
            if not isinstance(entry, Mapping):
                continue
            if isinstance(entry.get("elementId"), str):
                # v1 flat shape
                seq = entry.get("sequenceNumber")
                if isinstance(seq, int):
                    prev = self._sync_seq.get(subscription_id, -1)
                    if seq > prev:
                        self._sync_seq[subscription_id] = seq
                items.append(
                    SyncResponseItem(
                        elementId=entry["elementId"],
                        value=entry.get("value"),
                        quality=str(entry.get("quality") or "GoodNoData"),
                        timestamp=str(entry.get("timestamp") or ""),
                        sequenceNumber=seq if isinstance(seq, int) else 0,
                    )
                )
                continue
            # v0 keyed shape: {elementId: {data: [...]}}
            for _element_id, payload in entry.items():
                if not isinstance(payload, Mapping):
                    continue
                data = payload.get("data") or []
                if data:
                    vqt = _extract_vqt(data[0])
                    items.append(
                        SyncResponseItem(
                            value=vqt["value"],
                            quality=str(vqt.get("quality") or "GoodNoData"),
                            timestamp=str(vqt.get("timestamp") or ""),
                        )
                    )
        return items

    async def stream(self, subscription_id: str) -> AsyncIterator[list[SyncResponseItem]]:
        """Open the SSE stream and yield parsed batches as they arrive.

        v1: POST /subscriptions/stream with {subscriptionId}.
        v0: GET /subscriptions/{id}/stream.
        """
        if self._api_version == "v1":
            method = "POST"
            url = f"{self._base_url}/subscriptions/stream"
            body: Any = {"subscriptionId": subscription_id}
        else:
            method = "GET"
            url = f"{self._base_url}/subscriptions/{subscription_id}/stream"
            body = None

        headers = {"Accept": "text/event-stream", **self._auth_header()}
        if body is not None:
            headers["Content-Type"] = "application/json"

        async with aconnect_sse(
            self._http, method, url, headers=headers, json=body
        ) as event_source:
            async for sse in event_source.aiter_sse():
                if not sse.data.strip():
                    continue
                try:
                    parsed = json.loads(sse.data)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, list):
                    items = self._parse_stream_items(subscription_id, parsed)
                    if items:
                        yield items


async def noop_keepalive() -> None:  # pragma: no cover - placeholder for import verification
    await asyncio.sleep(0)
