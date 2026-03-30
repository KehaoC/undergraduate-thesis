from __future__ import annotations

import argparse
import hashlib
import io
import json
import random
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from tqdm import tqdm

from .github_api import GitHubAPI
from .parser import parse_skill_markdown


DEFAULT_QUERY = 'filename:SKILL.md "Use when"'

BINARY_EXTENSIONS = {
    ".7z",
    ".avi",
    ".bmp",
    ".class",
    ".dll",
    ".doc",
    ".docx",
    ".dylib",
    ".eot",
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".mov",
    ".mp3",
    ".mp4",
    ".otf",
    ".pdf",
    ".png",
    ".pyc",
    ".so",
    ".tar",
    ".ttf",
    ".wav",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
    ".xls",
    ".xlsx",
    ".zip",
}


@dataclass(frozen=True)
class SearchBand:
    low: int
    high: int

    @property
    def query_suffix(self) -> str:
        return f"size:{self.low}..{self.high}"


@dataclass(frozen=True)
class CandidateSkill:
    repo_full_name: str
    repo_url: str
    file_html_url: str
    skill_path: str
    blob_sha: str
    source_query: str

    @property
    def key(self) -> str:
        return f"{self.repo_full_name}:{self.skill_path}:{self.blob_sha}"


def build_size_bands(max_file_size: int, band_width: int) -> list[SearchBand]:
    bands: list[SearchBand] = []
    start = 0
    while start <= max_file_size:
        end = min(max_file_size, start + band_width - 1)
        bands.append(SearchBand(low=start, high=end))
        start = end + 1
    return bands


def discover_candidates(
    api: GitHubAPI,
    *,
    base_query: str,
    target_items: int,
    max_file_size: int,
    band_width: int,
    seed: int,
) -> list[CandidateSkill]:
    rng = random.Random(seed)
    bands = build_size_bands(max_file_size, band_width)
    rng.shuffle(bands)

    candidates: dict[str, CandidateSkill] = {}
    band_totals: dict[tuple[int, int], int] = {}

    for page in range(1, 11):
        made_progress = False
        for band in tqdm(bands, desc=f"Discovering skill files page={page}", leave=False):
            key = (band.low, band.high)
            total_count = band_totals.get(key)
            if total_count is not None and total_count <= (page - 1) * 100:
                continue

            query = f"{base_query} {band.query_suffix}"
            payload, _ = api.search_code(query, page=page, per_page=100)
            total_count = int(payload.get("total_count", 0))
            band_totals[key] = total_count
            if total_count == 0:
                continue

            made_progress = True
            for item in payload.get("items", []):
                repository = item["repository"]
                candidate = CandidateSkill(
                    repo_full_name=repository["full_name"],
                    repo_url=repository["html_url"],
                    file_html_url=item["html_url"],
                    skill_path=item["path"],
                    blob_sha=item["sha"],
                    source_query=query,
                )
                candidates[candidate.key] = candidate

            if len(candidates) >= target_items:
                return list(candidates.values())

        if not made_progress:
            break

    return list(candidates.values())


def chunked(values: Iterable[str], size: int) -> Iterable[list[str]]:
    chunk: list[str] = []
    for value in values:
        chunk.append(value)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def fetch_repo_stars(api: GitHubAPI, repo_names: list[str]) -> dict[str, dict[str, Any]]:
    repo_meta: dict[str, dict[str, Any]] = {}

    def task(repo_full_name: str) -> tuple[str, dict[str, Any] | None]:
        try:
            payload = api.fetch_repo_metadata(repo_full_name)
        except Exception:
            return repo_full_name, None

        return repo_full_name, {
            "github_url": payload["html_url"],
            "github_stars": int(payload["stargazers_count"]),
            "is_archived": bool(payload["archived"]),
            "is_disabled": bool(payload.get("disabled", False)),
            "default_branch": payload.get("default_branch") or "",
        }

    with ThreadPoolExecutor(max_workers=24) as executor:
        futures = [executor.submit(task, repo_full_name) for repo_full_name in repo_names]
        for future in tqdm(as_completed(futures), total=len(futures), desc="Fetching repo stars"):
            repo_full_name, payload = future.result()
            if payload:
                repo_meta[repo_full_name] = payload
    return repo_meta


def _fallback_name(repo_full_name: str, skill_path: str) -> str:
    stem = Path(skill_path).parent.name or Path(repo_full_name).name
    return stem.replace("-", " ").strip() or Path(repo_full_name).name


def _cache_has_default_branches(repo_meta: dict[str, dict[str, Any]]) -> bool:
    if not repo_meta:
        return False
    return all(bool(meta.get("default_branch")) for meta in repo_meta.values())


def _normalize_repo_root(path: PurePosixPath) -> str:
    normalized = path.as_posix()
    return "" if normalized == "." else normalized


def _is_under_root(path: str, package_root: PurePosixPath) -> bool:
    if package_root == PurePosixPath("."):
        return True
    try:
        PurePosixPath(path).relative_to(package_root)
        return True
    except ValueError:
        return False


def _relative_package_path(path: str, package_root: PurePosixPath) -> str:
    path_obj = PurePosixPath(path)
    if package_root == PurePosixPath("."):
        return path_obj.as_posix()
    return path_obj.relative_to(package_root).as_posix()


def _looks_binary_by_name(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in BINARY_EXTENSIONS


def _decode_text_bytes(raw: bytes) -> str | None:
    if not raw:
        return ""
    if b"\x00" in raw:
        return None

    text = raw.decode("utf-8", errors="replace")
    replacement_ratio = text.count("\ufffd") / max(1, len(text))
    control_ratio = sum(1 for char in text if ord(char) < 32 and char not in "\n\r\t\f\b") / max(1, len(text))
    if replacement_ratio > 0.02 or control_ratio > 0.02:
        return None

    return text.replace("\r\n", "\n").replace("\r", "\n")


def _repo_path_from_archive_member(member_name: str) -> str:
    parts = PurePosixPath(member_name).parts
    if len(parts) <= 1:
        return ""
    return PurePosixPath(*parts[1:]).as_posix()


def _matches_root(repo_path: str, package_root: str) -> bool:
    if not package_root:
        return True
    return repo_path == package_root or repo_path.startswith(f"{package_root}/")


def _build_repo_files_from_archive(
    archive_bytes: bytes,
    package_roots: set[str],
    *,
    max_inline_bytes: int,
) -> dict[str, dict[str, Any]]:
    repo_files: dict[str, dict[str, Any]] = {}
    include_all = "" in package_roots

    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        for member in archive:
            if not member.isfile():
                continue

            repo_path = _repo_path_from_archive_member(member.name)
            if not repo_path:
                continue
            if not include_all and not any(_matches_root(repo_path, root) for root in package_roots):
                continue

            entry: dict[str, Any] = {
                "size": int(member.size),
                "sha": None,
            }
            if _looks_binary_by_name(repo_path):
                entry["kind"] = "binary"
                repo_files[repo_path] = entry
                continue
            if member.size > max_inline_bytes:
                entry["kind"] = "omitted"
                entry["omitted_reason"] = "file_too_large"
                repo_files[repo_path] = entry
                continue

            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            raw = extracted.read()
            text = _decode_text_bytes(raw)
            if text is None:
                entry["kind"] = "binary"
            else:
                entry["kind"] = "text"
                entry["content"] = text
            repo_files[repo_path] = entry

    return repo_files


def _build_record_from_repo_files(
    candidate: CandidateSkill,
    meta: dict[str, Any],
    repo_files: dict[str, dict[str, Any]],
    parsed_skill: dict[str, Any],
) -> dict[str, Any] | None:
    if candidate.skill_path not in repo_files:
        return None

    package_root = PurePosixPath(candidate.skill_path).parent
    package_root_str = _normalize_repo_root(package_root)

    file_entries: list[dict[str, Any]] = []
    text_parts: list[str] = []
    text_file_count = 0
    binary_file_count = 0
    omitted_file_count = 0
    skill_rel_path = _relative_package_path(candidate.skill_path, package_root)

    for repo_path in sorted(repo_files):
        if not _is_under_root(repo_path, package_root):
            continue

        source_entry = repo_files[repo_path]
        relative_path = _relative_package_path(repo_path, package_root)
        entry: dict[str, Any] = {
            "path": relative_path,
            "size": source_entry["size"],
            "sha": source_entry.get("sha"),
            "kind": source_entry["kind"],
        }

        if source_entry["kind"] == "text":
            entry["content"] = parsed_skill["skill_body"] if relative_path == skill_rel_path else source_entry["content"]
            text_file_count += 1
            text_parts.append(f"[{relative_path}]\n{entry['content']}")
        elif source_entry["kind"] == "binary":
            binary_file_count += 1
        else:
            entry["omitted_reason"] = source_entry["omitted_reason"]
            omitted_file_count += 1

        file_entries.append(entry)

    if not any(entry["path"] == skill_rel_path for entry in file_entries):
        file_entries.append(
            {
                "path": skill_rel_path,
                "size": len(parsed_skill["skill_body"].encode("utf-8")),
                "sha": candidate.blob_sha,
                "kind": "text",
                "content": parsed_skill["skill_body"],
            }
        )
        text_file_count += 1
        text_parts.append(f"[{skill_rel_path}]\n{parsed_skill['skill_body']}")
        file_entries.sort(key=lambda item: item["path"])

    skill_id = hashlib.sha1(candidate.key.encode("utf-8")).hexdigest()[:16]
    content = {
        "root": package_root_str,
        "directories": _derive_directories([entry["path"] for entry in file_entries]),
        "files": file_entries,
        "file_count": len(file_entries),
        "text_file_count": text_file_count,
        "binary_file_count": binary_file_count,
        "omitted_file_count": omitted_file_count,
    }

    return {
        "skill_id": skill_id,
        "name": parsed_skill["name"],
        "description": parsed_skill["description"],
        "frontmatter": parsed_skill["frontmatter"],
        "content": content,
        "content_text": "\n\n".join(text_parts).strip(),
        "github_url": meta["github_url"],
        "github_file_url": candidate.file_html_url,
        "github_repo": candidate.repo_full_name,
        "github_stars": meta["github_stars"],
        "skill_path": candidate.skill_path,
    }


def build_records_from_archives(
    api: GitHubAPI,
    candidates: list[CandidateSkill],
    repo_meta: dict[str, dict[str, Any]],
    *,
    workers: int,
    max_inline_bytes: int,
) -> list[dict[str, Any]]:
    deduped_candidates: dict[tuple[str, str], CandidateSkill] = {}
    for candidate in candidates:
        deduped_candidates[(candidate.repo_full_name, candidate.skill_path)] = candidate

    grouped_candidates: dict[str, list[CandidateSkill]] = {}
    for candidate in deduped_candidates.values():
        grouped_candidates.setdefault(candidate.repo_full_name, []).append(candidate)

    def task_priority(item: tuple[str, list[CandidateSkill]]) -> tuple[int, int, str]:
        repo_full_name, repo_candidates = item
        has_root_skill = any(PurePosixPath(candidate.skill_path).parent == PurePosixPath(".") for candidate in repo_candidates)
        return (1 if has_root_skill else 0, len(repo_candidates), repo_full_name)

    def task(item: tuple[str, list[CandidateSkill]]) -> list[dict[str, Any]]:
        repo_full_name, repo_candidates = item
        meta = repo_meta.get(repo_full_name)
        if not meta or meta.get("github_stars", 0) <= 0:
            return []

        default_branch = meta.get("default_branch")
        if not default_branch:
            return []

        package_roots = {
            _normalize_repo_root(PurePosixPath(candidate.skill_path).parent)
            for candidate in repo_candidates
        }
        try:
            archive_bytes = api.fetch_repo_archive(repo_full_name, default_branch)
            repo_files = _build_repo_files_from_archive(
                archive_bytes,
                package_roots,
                max_inline_bytes=max_inline_bytes,
            )
        except Exception:
            return []

        records: list[dict[str, Any]] = []
        for candidate in repo_candidates:
            source_entry = repo_files.get(candidate.skill_path)
            if not source_entry or source_entry.get("kind") != "text":
                continue

            parsed = parse_skill_markdown(
                source_entry["content"],
                fallback_name=_fallback_name(candidate.repo_full_name, candidate.skill_path),
            )
            if not parsed:
                continue

            record = _build_record_from_repo_files(candidate, meta, repo_files, parsed)
            if record:
                records.append(record)

        return records

    records: list[dict[str, Any]] = []
    ordered_items = sorted(grouped_candidates.items(), key=task_priority)
    with ThreadPoolExecutor(max_workers=min(workers, 16)) as executor:
        futures = [executor.submit(task, item) for item in ordered_items]
        for future in tqdm(as_completed(futures), total=len(futures), desc="Building records from repo archives"):
            records.extend(future.result())

    return records


def fetch_repo_trees(
    api: GitHubAPI,
    repo_meta: dict[str, dict[str, Any]],
    *,
    workers: int,
) -> dict[str, dict[str, Any]]:
    repo_trees: dict[str, dict[str, Any]] = {}

    def task(item: tuple[str, dict[str, Any]]) -> tuple[str, dict[str, Any] | None]:
        repo_full_name, meta = item
        default_branch = meta.get("default_branch")
        if not default_branch:
            return repo_full_name, None
        try:
            return repo_full_name, api.fetch_repo_tree(repo_full_name, default_branch)
        except Exception:
            return repo_full_name, None

    items = list(repo_meta.items())
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(task, item) for item in items]
        for future in tqdm(as_completed(futures), total=len(futures), desc="Fetching repo trees"):
            repo_full_name, tree_payload = future.result()
            if tree_payload:
                repo_trees[repo_full_name] = tree_payload

    return repo_trees


def _derive_directories(file_paths: list[str]) -> list[str]:
    directories = {""}
    for path in file_paths:
        parent = PurePosixPath(path).parent
        while parent != PurePosixPath("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return sorted(directories)


def _build_package_file_entries(
    candidate: CandidateSkill,
    repo_tree: dict[str, Any],
    parsed_skill: dict[str, Any],
    *,
    max_inline_bytes: int,
) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
    package_root = PurePosixPath(candidate.skill_path).parent
    package_root_str = _normalize_repo_root(package_root)

    tree_items = repo_tree.get("tree", [])
    package_blobs = [
        item
        for item in tree_items
        if item.get("type") == "blob" and _is_under_root(item["path"], package_root)
    ]
    package_blobs.sort(key=lambda item: item["path"])

    file_entries: list[dict[str, Any]] = []
    fetch_requests: list[tuple[str, str, str]] = []
    skill_rel_path = _relative_package_path(candidate.skill_path, package_root)

    for item in package_blobs:
        repo_path = item["path"]
        relative_path = _relative_package_path(repo_path, package_root)
        size = int(item.get("size", 0) or 0)
        entry: dict[str, Any] = {
            "path": relative_path,
            "repo_path": repo_path,
            "size": size,
            "sha": item.get("sha"),
        }

        if relative_path == skill_rel_path:
            entry["kind"] = "text"
            entry["content"] = parsed_skill["skill_body"]
        elif _looks_binary_by_name(repo_path):
            entry["kind"] = "binary"
        elif size > max_inline_bytes:
            entry["kind"] = "omitted"
            entry["omitted_reason"] = "file_too_large"
        else:
            entry["kind"] = "text"
            fetch_requests.append((candidate.repo_full_name, repo_path, relative_path))

        file_entries.append(entry)

    if not any(entry["path"] == skill_rel_path for entry in file_entries):
        file_entries.append(
            {
                "path": skill_rel_path,
                "repo_path": candidate.skill_path,
                "size": len(parsed_skill["skill_body"].encode("utf-8")),
                "sha": candidate.blob_sha,
                "kind": "text",
                "content": parsed_skill["skill_body"],
            }
        )
        file_entries.sort(key=lambda entry: entry["path"])

    content = {
        "root": package_root_str,
        "directories": _derive_directories([entry["path"] for entry in file_entries]),
        "files": file_entries,
    }
    return content, fetch_requests


def build_skill_skeletons(
    api: GitHubAPI,
    candidates: list[CandidateSkill],
    repo_meta: dict[str, dict[str, Any]],
    repo_trees: dict[str, dict[str, Any]],
    *,
    workers: int,
    max_inline_bytes: int,
) -> tuple[list[dict[str, Any]], list[tuple[str, str, str, str]]]:
    skeletons: list[dict[str, Any]] = []
    deduped: dict[str, dict[str, Any]] = {}

    def task(candidate: CandidateSkill) -> dict[str, Any] | None:
        meta = repo_meta.get(candidate.repo_full_name)
        repo_tree = repo_trees.get(candidate.repo_full_name)
        if not meta or not repo_tree or meta["github_stars"] <= 0:
            return None

        default_branch = meta.get("default_branch")
        if not default_branch:
            return None

        try:
            markdown = api.fetch_raw_from_repo_path(candidate.repo_full_name, default_branch, candidate.skill_path)
            parsed = parse_skill_markdown(markdown, fallback_name=_fallback_name(candidate.repo_full_name, candidate.skill_path))
        except Exception:
            return None

        if not parsed:
            return None

        content, fetch_requests = _build_package_file_entries(
            candidate,
            repo_tree,
            parsed,
            max_inline_bytes=max_inline_bytes,
        )
        skill_id = hashlib.sha1(candidate.key.encode("utf-8")).hexdigest()[:16]

        return {
            "skill_id": skill_id,
            "name": parsed["name"],
            "description": parsed["description"],
            "frontmatter": parsed["frontmatter"],
            "content": content,
            "github_url": meta["github_url"],
            "github_file_url": candidate.file_html_url,
            "github_repo": candidate.repo_full_name,
            "github_stars": meta["github_stars"],
            "skill_path": candidate.skill_path,
            "default_branch": default_branch,
            "_fetch_requests": fetch_requests,
        }

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(task, candidate) for candidate in candidates]
        for future in tqdm(as_completed(futures), total=len(futures), desc="Building skill file trees"):
            record = future.result()
            if not record:
                continue
            dedupe_key = (record["github_repo"], record["skill_path"])
            deduped[str(dedupe_key)] = record

    skeletons = list(deduped.values())

    unique_fetches: dict[tuple[str, str, str], tuple[str, str, str, str]] = {}
    for record in skeletons:
        repo_full_name = record["github_repo"]
        default_branch = record["default_branch"]
        for _, repo_path, relative_path in record.pop("_fetch_requests"):
            key = (repo_full_name, default_branch, repo_path)
            unique_fetches[key] = (repo_full_name, default_branch, repo_path, relative_path)

    return skeletons, list(unique_fetches.values())


def _batch_text_fetch_requests(
    fetch_requests: list[tuple[str, str, str, str]],
    *,
    batch_size: int,
) -> list[tuple[str, str, list[str]]]:
    grouped: dict[tuple[str, str], set[str]] = {}
    for repo_full_name, default_branch, repo_path, _ in fetch_requests:
        grouped.setdefault((repo_full_name, default_branch), set()).add(repo_path)

    batches: list[tuple[str, str, list[str]]] = []
    for (repo_full_name, default_branch), repo_paths in grouped.items():
        for repo_path_chunk in chunked(sorted(repo_paths), batch_size):
            batches.append((repo_full_name, default_branch, repo_path_chunk))
    return batches


def fetch_text_file_contents(
    api: GitHubAPI,
    fetch_requests: list[tuple[str, str, str, str]],
    *,
    workers: int,
    batch_size: int = 40,
) -> dict[tuple[str, str, str], str | None]:
    file_contents: dict[tuple[str, str, str], str | None] = {}

    def task(batch: tuple[str, str, list[str]]) -> dict[tuple[str, str, str], str | None]:
        repo_full_name, default_branch, repo_paths = batch
        owner, repo = repo_full_name.split("/", 1)
        fields = []
        for index, repo_path in enumerate(repo_paths):
            expression = f"{default_branch}:{repo_path}"
            fields.append(
                f"blob_{index}: object(expression: {json.dumps(expression)}) "
                "{ ... on Blob { byteSize isBinary isTruncated text } }"
            )

        query = (
            "query {\n"
            f"  repository(owner: {json.dumps(owner)}, name: {json.dumps(repo)}) {{\n"
            f"    {' '.join(fields)}\n"
            "  }\n"
            "}"
        )

        resolved: dict[tuple[str, str, str], str | None] = {}
        try:
            payload = api.post_graphql(query).get("repository") or {}
        except Exception:
            payload = None

        for index, repo_path in enumerate(repo_paths):
            key = (repo_full_name, default_branch, repo_path)
            blob_payload = payload.get(f"blob_{index}") if payload else None
            if blob_payload:
                if blob_payload.get("isBinary"):
                    resolved[key] = None
                    continue

                text = blob_payload.get("text")
                if text is not None and not blob_payload.get("isTruncated"):
                    resolved[key] = text.replace("\r\n", "\n").replace("\r", "\n")
                    continue

            try:
                raw = api.fetch_bytes_from_repo_path(repo_full_name, default_branch, repo_path)
            except Exception:
                resolved[key] = None
                continue

            resolved[key] = _decode_text_bytes(raw)

        return resolved

    batches = _batch_text_fetch_requests(fetch_requests, batch_size=batch_size)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(task, batch) for batch in batches]
        for future in tqdm(as_completed(futures), total=len(futures), desc="Fetching package file contents"):
            file_contents.update(future.result())

    return file_contents


def finalize_skill_records(
    skeletons: list[dict[str, Any]],
    file_contents: dict[tuple[str, str, str], str | None],
) -> list[dict[str, Any]]:
    finalized: list[dict[str, Any]] = []

    for record in skeletons:
        repo_full_name = record["github_repo"]
        default_branch = record.pop("default_branch")

        text_parts: list[str] = []
        text_file_count = 0
        binary_file_count = 0
        omitted_file_count = 0

        cleaned_files: list[dict[str, Any]] = []
        for file_entry in record["content"]["files"]:
            file_copy = dict(file_entry)
            repo_path = file_copy.pop("repo_path")

            if file_copy["kind"] == "text" and "content" not in file_copy:
                key = (repo_full_name, default_branch, repo_path)
                resolved = file_contents.get(key)
                if resolved is None:
                    file_copy["kind"] = "omitted"
                    file_copy["omitted_reason"] = "non_text_or_fetch_failed"
                else:
                    file_copy["content"] = resolved

            if file_copy["kind"] == "text":
                text_file_count += 1
                text_parts.append(f"[{file_copy['path']}]\n{file_copy.get('content', '')}")
            elif file_copy["kind"] == "binary":
                binary_file_count += 1
            else:
                omitted_file_count += 1

            cleaned_files.append(file_copy)

        record["content"]["files"] = cleaned_files
        record["content"]["file_count"] = len(cleaned_files)
        record["content"]["text_file_count"] = text_file_count
        record["content"]["binary_file_count"] = binary_file_count
        record["content"]["omitted_file_count"] = omitted_file_count
        record["content_text"] = "\n\n".join(text_parts).strip()

        finalized.append(record)

    return finalized


def export_datasets(records: list[dict[str, Any]], output_dir: Path, *, final_counts: list[int], seed: int) -> None:
    ordered = sorted(records, key=lambda item: item["skill_id"])
    rng = random.Random(seed)
    shuffled = ordered[:]
    rng.shuffle(shuffled)

    full_path = output_dir / "skills_full.json"
    full_path.write_text(json.dumps(shuffled, ensure_ascii=False, indent=2), encoding="utf-8")

    for count in final_counts:
        if len(shuffled) < count:
            raise RuntimeError(f"Only built {len(shuffled)} clean skills, not enough for skills_{count}.json")
        subset = shuffled[:count]
        subset_path = output_dir / f"skills_{count}.json"
        subset_path.write_text(json.dumps(subset, ensure_ascii=False, indent=2), encoding="utf-8")

    text_file_counts = [item["content"]["text_file_count"] for item in shuffled]
    summary = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_clean_skills": len(shuffled),
        "unique_repos": len({item["github_repo"] for item in shuffled}),
        "final_counts": final_counts,
        "median_star": sorted(item["github_stars"] for item in shuffled)[len(shuffled) // 2],
        "median_text_files_per_skill": sorted(text_file_counts)[len(text_file_counts) // 2] if text_file_counts else 0,
        "total_text_files": sum(item["content"]["text_file_count"] for item in shuffled),
        "total_binary_files": sum(item["content"]["binary_file_count"] for item in shuffled),
        "total_omitted_files": sum(item["content"]["omitted_file_count"] for item in shuffled),
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build clean GitHub-backed skill datasets for the thesis experiments.")
    parser.add_argument("--output-dir", type=Path, default=Path("data"))
    parser.add_argument("--base-query", default=DEFAULT_QUERY)
    parser.add_argument("--raw-target", type=int, default=15000)
    parser.add_argument("--final-counts", default="100,1000,10000")
    parser.add_argument("--max-file-size", type=int, default=60000)
    parser.add_argument("--band-width", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=20260330)
    parser.add_argument("--workers", type=int, default=48)
    parser.add_argument("--max-inline-bytes", type=int, default=2_000_000)
    parser.add_argument("--refresh", action="store_true", help="Ignore cached intermediate artifacts and refetch everything.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    final_counts = [int(part) for part in args.final_counts.split(",") if part]
    output_dir = args.output_dir
    cache_dir = output_dir / "cache"
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    api = GitHubAPI()

    candidates_path = cache_dir / "candidates.json"
    repo_meta_path = cache_dir / "repo_meta_v2.json"
    cleaned_path = cache_dir / "cleaned_skills_v3.json"

    if candidates_path.exists() and not args.refresh:
        candidates = [CandidateSkill(**item) for item in load_json(candidates_path)]
    else:
        candidates = discover_candidates(
            api,
            base_query=args.base_query,
            target_items=args.raw_target,
            max_file_size=args.max_file_size,
            band_width=args.band_width,
            seed=args.seed,
        )
        save_json(candidates_path, [asdict(candidate) for candidate in candidates])

    repo_names = sorted({candidate.repo_full_name for candidate in candidates})
    if repo_meta_path.exists() and not args.refresh:
        repo_meta = load_json(repo_meta_path)
        if not _cache_has_default_branches(repo_meta):
            repo_meta = fetch_repo_stars(api, repo_names)
            save_json(repo_meta_path, repo_meta)
    else:
        repo_meta = fetch_repo_stars(api, repo_names)
        save_json(repo_meta_path, repo_meta)

    if cleaned_path.exists() and not args.refresh:
        cleaned_records = load_json(cleaned_path)
    else:
        filtered_candidates = [
            candidate
            for candidate in candidates
            if repo_meta.get(candidate.repo_full_name, {}).get("github_stars", 0) > 0
        ]
        cleaned_records = build_records_from_archives(
            api,
            filtered_candidates,
            repo_meta,
            workers=args.workers,
            max_inline_bytes=args.max_inline_bytes,
        )
        save_json(cleaned_path, cleaned_records)

    export_datasets(cleaned_records, output_dir, final_counts=final_counts, seed=args.seed)


if __name__ == "__main__":
    main()
