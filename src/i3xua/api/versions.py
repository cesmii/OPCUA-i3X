"""v0 vs v1 response shaping.

``shape_single`` wraps a single result; ``shape_bulk`` handles arrays of
``{success, elementId, ...}`` entries. v1 wraps in ``{success, result(s)}``;
v0 returns the raw value.
"""

from __future__ import annotations

from typing import Any, Literal

ApiVersion = Literal["v0", "v1"]


def shape_single(version: ApiVersion, result: Any) -> Any:
    if version == "v1":
        return {"success": True, "result": result}
    return result


def shape_bulk(version: ApiVersion, results: list[dict[str, Any]]) -> Any:
    """`results` is already a list of `{success, elementId, result?, error?}` entries."""
    if version == "v1":
        return {"success": all(r.get("success") for r in results), "results": results}
    return results


__all__ = ["ApiVersion", "shape_bulk", "shape_single"]
