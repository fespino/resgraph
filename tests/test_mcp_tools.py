"""Unit tests for the canonical tool layer: budgets, clamps, skills,
and the derived MCP surface — no stores touched."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from resgraph.mcp.skills import SkillError, load_skill, load_skills
from resgraph.query.executor import QueryContext
from resgraph.tools import budgets
from resgraph.tools.budgets import ResourceRef, paginate_refs
from resgraph.tools.canonical import traversal
from resgraph.tools.canonical.traversal import BlastRadiusIn, blast_radius
from resgraph.tools.context import CallerContext

SKILLS_DIR = Path(__file__).parents[1] / "skills"


def _ctx() -> CallerContext:
    return CallerContext("mcp", frozenset({"resgraph:read"}), QueryContext(session=object()))


def _refs(n: int) -> list[ResourceRef]:
    return [ResourceRef(id=f"vm-{i:06d}", type="vm", one_line="x" * 40) for i in range(n)]


def test_small_result_is_one_untruncated_page():
    page, truncated, hint, total = paginate_refs(_refs(5), 0, "blast_radius")
    assert (len(page), truncated, hint, total) == (5, False, None, 5)


def test_oversized_result_paginates_under_the_cap_with_a_prose_hint():
    refs = _refs(5000)
    page, truncated, hint, total = paginate_refs(refs, 0, "blast_radius")
    assert truncated and total == 5000 and 0 < len(page) < 5000
    assert hint is not None and f"offset={len(page)}" in hint and "blast_radius" in hint
    body = budgets._Page(items=page)
    assert budgets.estimate_tokens(body) <= budgets.TOOL_RESPONSE_TOKEN_CAP


def test_following_the_hint_yields_a_disjoint_continuing_page():
    refs = _refs(5000)
    page1, _, _, _ = paginate_refs(refs, 0, "t")
    page2, _, _, _ = paginate_refs(refs, len(page1), "t")
    assert page2[0].id == refs[len(page1)].id
    assert {r.id for r in page1}.isdisjoint({r.id for r in page2})


def test_depth_beyond_the_cap_clamps_instead_of_erroring(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(traversal, "execute_plan", lambda p, ctx: [])
    out = blast_radius(BlastRadiusIn(resource_id="db-1", depth=50), ctx=_ctx())
    assert out.depth_clamped and out.source == "hot"
    out = blast_radius(BlastRadiusIn(resource_id="db-1", depth=0), ctx=_ctx())
    assert out.depth_clamped
    out = blast_radius(BlastRadiusIn(resource_id="db-1", depth=3), ctx=_ctx())
    assert not out.depth_clamped


def test_at_flips_source_to_composite(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(traversal, "execute_plan", lambda p, ctx: [])
    out = blast_radius(
        BlastRadiusIn(resource_id="db-1", depth=2, at=datetime(2026, 8, 1, tzinfo=UTC)),
        ctx=_ctx(),
    )
    assert out.source == "composite"


def test_shipped_skills_load_and_validate():
    skills = load_skills(SKILLS_DIR)
    assert sorted(s.manifest.name for s in skills) == ["change-forensics", "incident-impact"]


def _write_skill(tmp_path: Path, body: str, tool_refs: str = "[blast_radius]") -> Path:
    d = tmp_path / "s"
    d.mkdir()
    p = d / "SKILL.md"
    p.write_text(
        "---\n"
        f"name: s\nversion: '1.0'\ndescription: d\nscope: resgraph:read\ntool_refs: {tool_refs}\n"
        "---\n" + body
    )
    return p


_SECTIONS = "\n".join(
    f"{s}\n\ntext\n"
    for s in (
        "## Goal",
        "## When to use",
        "## Steps",
        "## Tools to call",
        "## Examples",
        "## Anti-patterns",
    )
)


def test_skill_with_unknown_tool_ref_fails_loudly(tmp_path: Path):
    p = _write_skill(tmp_path, _SECTIONS, tool_refs="[no_such_tool]")
    with pytest.raises(SkillError, match="no_such_tool"):
        load_skill(p)


def test_skill_missing_a_section_fails_loudly(tmp_path: Path):
    p = _write_skill(tmp_path, _SECTIONS.replace("## Anti-patterns", "## Antipatterns"))
    with pytest.raises(SkillError, match="Anti-patterns"):
        load_skill(p)


def test_skill_sections_out_of_order_fail_loudly(tmp_path: Path):
    swapped = (
        _SECTIONS.replace("## Goal", "## TMP")
        .replace("## Steps", "## Goal")
        .replace("## TMP", "## Steps")
    )
    p = _write_skill(tmp_path, swapped)
    with pytest.raises(SkillError, match="order"):
        load_skill(p)


def test_mcp_surface_derives_entirely_from_the_registry():
    from resgraph.mcp.server import build_server
    from resgraph.tools.registry import TOOL_REGISTRY

    server = build_server()

    async def surface():
        tools = await server.list_tools()
        prompts = await server.list_prompts()
        return tools, prompts

    tools, prompts = asyncio.run(surface())
    assert {t.name for t in tools} == {e.name for e in TOOL_REGISTRY if "mcp" in e.surfaces}
    assert {p.name for p in prompts} == {"incident-impact", "change-forensics"}
    br = next(t for t in tools if t.name == "blast_radius")
    assert br.annotations is not None and br.annotations.read_only_hint is True
    assert br.annotations.destructive_hint is False
    assert br.meta is not None and br.meta["timeout_s"] == 10.0
    assert set(br.input_schema["properties"]) == {"resource_id", "depth", "at", "filter", "offset"}


def test_http_surface_derives_entirely_from_the_registry():
    from fastapi.testclient import TestClient

    from resgraph.api.app import app
    from resgraph.tools.registry import TOOL_REGISTRY

    client = TestClient(app)
    paths = set(client.get("/openapi.json").json()["paths"])
    for entry in TOOL_REGISTRY:
        if "http" in entry.surfaces:
            assert f"/tools/{entry.name}" in paths
    assert client.post("/tools/blast_radius", json={}).status_code == 422
