"""Telegraph publishing helpers using the official API and native Node structure."""
from __future__ import annotations

import json
import re
from typing import Any

MAX_CONTENT_BYTES = 64 * 1024
API_BASE = "https://api.telegra.ph"
USER_AGENT = "TgFarm/1.0 (+https://github.com/rpauts2/Tggruup)"


def _inline_nodes(value: str) -> list[str | dict[str, Any]]:
    tick = chr(96)
    code_pattern = re.escape(tick) + r"[^" + re.escape(tick) + r"\n]+" + re.escape(tick)
    token = re.compile(
        r"(!\[[^\]]*\]\(https?://[^)\s]+\)|"
        r"\[[^\]]+\]\(https?://[^)\s]+\)|"
        r"\*\*.+?\*\*|(?<!\*)\*[^*\n]+\*|" + code_pattern + r")"
    )
    result: list[str | dict[str, Any]] = []
    cursor = 0
    for match in token.finditer(value):
        if match.start() > cursor:
            result.append(value[cursor:match.start()])
        item = match.group(0)
        image = re.fullmatch(r"!\[([^\]]*)\]\((https?://[^)\s]+)\)", item)
        link = re.fullmatch(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", item)
        if image:
            result.append({"tag": "img", "attrs": {"src": image.group(2)}})
        elif link:
            result.append({"tag": "a", "attrs": {"href": link.group(2)}, "children": [link.group(1)]})
        elif item.startswith("**") and item.endswith("**"):
            result.append({"tag": "strong", "children": [item[2:-2]]})
        elif item.startswith("*") and item.endswith("*"):
            result.append({"tag": "em", "children": [item[1:-1]]})
        elif item.startswith(tick) and item.endswith(tick):
            result.append({"tag": "code", "children": [item[1:-1]]})
        else:
            result.append(item)
        cursor = match.end()
    if cursor < len(value):
        result.append(value[cursor:])
    return result or [""]


def build_telegraph_content(title: str, body: str) -> list[dict[str, Any]]:
    """Convert TgFarm Markdown into Telegraph Node objects within the 64 KiB limit."""
    if not str(title or "").strip() or len(str(title).strip()) > 256:
        raise ValueError("Telegraph title must contain between 1 and 256 characters.")
    text = str(body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise ValueError("Telegraph article body is empty.")

    lines = text.split("\n")
    nodes: list[dict[str, Any]] = []
    paragraph: list[str] = []
    list_type: str | None = None
    list_items: list[dict[str, Any]] = []
    in_code = False
    code_lines: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            value = " ".join(part.strip() for part in paragraph).strip()
            if value:
                nodes.append({"tag": "p", "children": _inline_nodes(value)})
            paragraph = []

    def flush_list() -> None:
        nonlocal list_type, list_items
        if list_type and list_items:
            nodes.append({"tag": list_type, "children": list_items})
        list_type = None
        list_items = []

    for line in lines:
        if line.strip().startswith(tick * 3):
            flush_paragraph()
            flush_list()
            if in_code:
                nodes.append({"tag": "pre", "children": ["\n".join(code_lines)]})
                code_lines = []
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not line.strip():
            flush_paragraph()
            flush_list()
            continue
        if re.fullmatch(r"\s*(?:---+|\*\*\*+|___+)\s*", line):
            flush_paragraph()
            flush_list()
            nodes.append({"tag": "hr"})
            continue
        heading = re.match(r"^\s*(#{1,4})\s+(.+)$", line)
        if heading:
            flush_paragraph()
            flush_list()
            tag = "h3" if len(heading.group(1)) <= 2 else "h4"
            nodes.append({"tag": tag, "children": _inline_nodes(heading.group(2).strip())})
            continue
        quote = re.match(r"^\s*>\s?(.*)$", line)
        if quote:
            flush_paragraph()
            flush_list()
            nodes.append({"tag": "blockquote", "children": [{"tag": "p", "children": _inline_nodes(quote.group(1))}]})
            continue
        unordered = re.match(r"^\s*[-*+]\s+(.+)$", line)
        ordered = re.match(r"^\s*\d+[.)]\s+(.+)$", line)
        if unordered or ordered:
            flush_paragraph()
            desired = "ul" if unordered else "ol"
            if list_type and list_type != desired:
                flush_list()
            list_type = desired
            value = (unordered or ordered).group(1).strip()
            list_items.append({"tag": "li", "children": _inline_nodes(value)})
            continue
        flush_list()
        paragraph.append(line)

    flush_paragraph()
    flush_list()
    if in_code:
        nodes.append({"tag": "pre", "children": ["\n".join(code_lines)]})
    if not nodes:
        nodes = [{"tag": "p", "children": [text]}]

    size = len(json.dumps(nodes, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if size > MAX_CONTENT_BYTES:
        raise ValueError(f"Telegraph article content is {size} bytes; the API limit is 64 KB.")
    return nodes
