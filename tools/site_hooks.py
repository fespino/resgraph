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
    chapter: str
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
    stem = Path(src_uri).stem
    chapter = stem.split("-", 1)[0] if stem[:2].isdigit() else ""
    return Post(
        src_uri=src_uri,
        chapter=chapter,
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
    # chapter order: the devlog reads like a book, oldest first
    posts.sort(key=lambda p: p.src_uri)
    return posts


def _entry_title(p: Post) -> str:
    return f'<span class="chapno">#{p.chapter}</span>{p.title}' if p.chapter else p.title


def _post_url(p: Post, prefix: str = "") -> str:
    # raw-HTML hrefs are not rewritten by the engine, so emit the final
    # directory URL directly
    return prefix + p.src_uri[: -len(".md")] + "/"


def _post_list(posts: list[Post], prefix: str) -> str:
    items = "\n".join(
        f'<li><a href="{_post_url(p, prefix)}">{_entry_title(p)}</a></li>' for p in posts
    )
    return f'<ul class="devlog-list">\n{items}\n</ul>'


def _writing_page(posts: list[Post]) -> str:
    lines = ["# Devlog", "", "The build log, chapter by chapter — read it in order.", ""]
    for p in posts:
        lines += [
            f"### [{_entry_title(p)}](../{p.src_uri})",
            "",
            p.excerpt,
            "",
        ]
    return "\n".join(lines)


def _tag_page(tag: str, posts: list[Post]) -> str:
    lines = [f"# {tag}", "", f'Chapters filed under "{tag}", in order.', ""]
    lines.append(_post_list(posts, "../../"))
    lines.append("")
    return "\n".join(lines)


_POSTS_CACHE: list[Post] = []


def on_files(files: Files, config: ProperDocsConfig) -> Files:
    global _POSTS_CACHE
    posts = _collect_posts(files)
    _POSTS_CACHE = posts
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
    if page.file.src_uri == "index.md":
        # the home page's Devlog list is generated, never hand-maintained —
        # titles come from each post's H1 at build time
        return markdown.replace("<!-- posts:auto -->", _post_list(_POSTS_CACHE, ""))
    if not page.file.src_uri.startswith(POSTS_PREFIX):
        return markdown
    tags = [str(t) for t in page.meta.get("tags", [])]
    if not tags:
        return markdown
    links = ", ".join(f"[{t}](../../tags/{_slug(t)}/index.md)" for t in tags)
    return f"{markdown}\n\n---\n\n<small>filed under: {links}</small>\n"
