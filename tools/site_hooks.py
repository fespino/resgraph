"""Site build hooks: generate the writing index, per-tag pages, and post taglines.

Wired via `hooks:` in mkdocs.yml so the blog needs no plugin dependency —
everything below is stdlib + yaml against the engine's documented hook API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from properdocs.config.defaults import ProperDocsConfig
from properdocs.structure.files import File, Files
from properdocs.structure.pages import Page

POSTS_PREFIX = "blog/posts/"

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)
_H1 = re.compile(r"^# (.+)$", re.MULTILINE)
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")


@dataclass
class Post:
    src_uri: str
    title: str
    date: str
    tags: list[str]
    excerpt: str


def _slug(tag: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", tag.lower()).strip("-")


def _parse_post(src_uri: str, path: str) -> Post | None:
    text = Path(path).read_text(encoding="utf-8")
    fm = _FRONTMATTER.match(text)
    if not fm:
        return None
    meta = yaml.safe_load(fm.group(1)) or {}
    body = fm.group(2)
    h1 = _H1.search(body)
    title = h1.group(1).strip() if h1 else Path(src_uri).stem
    after = body[h1.end() :] if h1 else body
    paras = [p.strip() for p in after.split("\n\n") if p.strip() and not p.startswith("#")]
    excerpt = paras[0].replace("\n", " ") if paras else ""
    # links in an excerpt would resolve relative to the wrong page — keep the text only
    excerpt = _MD_LINK.sub(r"\1", excerpt)
    return Post(
        src_uri=src_uri,
        title=title,
        date=str(meta.get("date", "")),
        tags=[str(t) for t in meta.get("tags", [])],
        excerpt=excerpt,
    )


def _collect_posts(files: Files) -> list[Post]:
    posts = []
    for f in files:
        if f.src_uri.startswith(POSTS_PREFIX) and f.src_uri.endswith(".md") and f.abs_src_path:
            post = _parse_post(f.src_uri, f.abs_src_path)
            if post:
                posts.append(post)
    posts.sort(key=lambda p: (p.date, p.src_uri), reverse=True)
    return posts


def _writing_page(posts: list[Post]) -> str:
    lines = ["# Writing", "", "Build-in-public notes, newest first.", ""]
    for p in posts:
        lines += [
            f"### [{p.title}](../{p.src_uri})",
            "",
            f'<small class="post-date">{p.date}</small>',
            "",
            p.excerpt,
            "",
        ]
    return "\n".join(lines)


def _tag_page(tag: str, posts: list[Post]) -> str:
    lines = [f"# {tag}", "", f'Posts filed under "{tag}", newest first.', ""]
    for p in posts:
        lines.append(f"- [{p.title}](../../{p.src_uri}) — {p.date}")
    lines.append("")
    return "\n".join(lines)


def on_files(files: Files, config: ProperDocsConfig) -> Files:
    posts = _collect_posts(files)
    files.append(File.generated(config, "writing/index.md", content=_writing_page(posts)))
    tags: dict[str, list[Post]] = {}
    for p in posts:
        for t in p.tags:
            tags.setdefault(t, []).append(p)
    for tag, tagged in sorted(tags.items()):
        files.append(
            File.generated(config, f"tags/{_slug(tag)}/index.md", content=_tag_page(tag, tagged))
        )
    return files


def on_page_markdown(markdown: str, page: Page, config: ProperDocsConfig, files: Files) -> str:
    if not page.file.src_uri.startswith(POSTS_PREFIX):
        return markdown
    tags = [str(t) for t in page.meta.get("tags", [])]
    if not tags:
        return markdown
    links = ", ".join(f"[{t}](../../tags/{_slug(t)}/index.md)" for t in tags)
    return f"{markdown}\n\n---\n\n<small>filed under: {links}</small>\n"
