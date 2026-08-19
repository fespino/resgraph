"""Response budgets enforced inside the tool.

The consumer is a language model: overflow answers with a prose hint
that teaches the next move, never an error.

Decisions: D20 (SPEC.md).
"""

from collections.abc import Sequence

from pydantic import BaseModel

TOOL_RESPONSE_TOKEN_CAP = 8000


class ResourceRef(BaseModel):
    id: str
    type: str
    one_line: str


def estimate_tokens(model: BaseModel) -> int:
    # len/4 — precision is not the point, the ceiling is
    return len(model.model_dump_json()) // 4


def _over_cap(page: Sequence[BaseModel], probe: type[BaseModel]) -> bool:
    body = probe.model_validate({"items": [i.model_dump() for i in page]})
    return estimate_tokens(body) > TOOL_RESPONSE_TOKEN_CAP


def paginate[T: BaseModel](
    items: Sequence[T], offset: int, tool: str, probe: type[BaseModel]
) -> tuple[list[T], bool, str | None, int]:
    """Largest page from `offset` that keeps the response under the cap.

    `probe` wraps a candidate page the way the tool's output model will,
    so the estimate measures what actually ships.
    """
    total = len(items)
    offset = max(0, offset)
    page: list[T] = list(items[offset:])
    while page and _over_cap(page, probe):
        page = page[: max(1, len(page) * 3 // 4)]
        if len(page) == 1:
            break
    truncated = offset + len(page) < total
    hint = (
        f"{total} results total; this page covers {offset}..{offset + len(page) - 1}. "
        f"Call {tool} again with offset={offset + len(page)} for the next page."
        if truncated
        else None
    )
    return page, truncated, hint, total


class _Page(BaseModel):
    items: list[ResourceRef]


def paginate_refs(
    refs: Sequence[ResourceRef], offset: int, tool: str
) -> tuple[list[ResourceRef], bool, str | None, int]:
    return paginate(refs, offset, tool, _Page)
