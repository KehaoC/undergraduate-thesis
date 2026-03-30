from __future__ import annotations

import re
from typing import Any

import yaml


FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
H1_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)
DESCRIPTION_RE = re.compile(r"^\s*description\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def _normalize_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    return value


def _normalize_yaml_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.split())
    return " ".join(str(value).split())


def _extract_frontmatter(markdown: str) -> tuple[dict[str, Any], str]:
    match = FRONTMATTER_RE.match(markdown)
    if not match:
        return {}, markdown
    metadata = yaml.safe_load(match.group(1)) or {}
    if not isinstance(metadata, dict):
        metadata = {}
    body = markdown[match.end() :]
    return metadata, body


def parse_skill_markdown(markdown: str, *, fallback_name: str) -> dict[str, Any] | None:
    markdown = _normalize_text(markdown)
    if not markdown:
        return None

    frontmatter, body = _extract_frontmatter(markdown)
    body = _normalize_text(body)

    heading_match = H1_RE.search(body)
    description_match = DESCRIPTION_RE.search(markdown)

    name = _normalize_yaml_scalar(frontmatter.get("name")) or (heading_match.group(1).strip() if heading_match else fallback_name)
    description = _normalize_yaml_scalar(frontmatter.get("description")) or (
        " ".join(description_match.group(1).split()) if description_match else ""
    )

    lowered = markdown.lower()
    looks_like_skill = bool(name and description and body) and (
        "use when" in lowered or "when to use" in lowered or "agent skill" in lowered
    )

    if not looks_like_skill:
        return None

    return {
        "name": name,
        "description": description,
        "frontmatter": frontmatter,
        "skill_body": body,
    }
