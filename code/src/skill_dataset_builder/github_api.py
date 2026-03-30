from __future__ import annotations

import json
import os
import subprocess
import time
import urllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import PurePosixPath
from typing import Any


class GitHubAPI:
    def __init__(
        self,
        token: str | None = None,
        user_agent: str = "taste-skill-dataset-builder",
        api_timeout: int = 60,
        raw_timeout: int = 20,
    ) -> None:
        self.token = token or self._resolve_token()
        self.user_agent = user_agent
        self.api_timeout = api_timeout
        self.raw_timeout = raw_timeout

    @staticmethod
    def _resolve_token() -> str:
        for key in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT"):
            value = os.environ.get(key)
            if value:
                return value
        try:
            return subprocess.check_output(["gh", "auth", "token"], text=True).strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(
                "GitHub token not found. Please export GITHUB_TOKEN or run `gh auth login` first."
            ) from exc

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": self.user_agent,
        }
        if extra:
            headers.update(extra)
        return headers

    def _request(
        self,
        url: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        retries: int = 4,
    ) -> urllib.response.addinfourl:
        request = urllib.request.Request(url, data=data, headers=self._headers(headers))
        for attempt in range(retries + 1):
            try:
                return urllib.request.urlopen(request, timeout=self.api_timeout)
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 429):
                    reset_at = exc.headers.get("X-RateLimit-Reset")
                    wait_seconds = max(1, int(reset_at) - int(time.time()) + 1) if reset_at else 30
                    time.sleep(wait_seconds)
                    continue
                if exc.code >= 500 and attempt < retries:
                    time.sleep(min(2**attempt, 30))
                    continue
                raise
            except urllib.error.URLError:
                if attempt >= retries:
                    raise
                time.sleep(min(2**attempt, 30))

        raise RuntimeError(f"Unreachable retry loop for GitHub request: {url}")

    def get_json(self, url: str) -> tuple[dict[str, Any], dict[str, str]]:
        with self._request(url) as response:
            headers = dict(response.info())
            payload = json.load(response)
        return payload, headers

    def post_graphql(self, query: str) -> dict[str, Any]:
        body = json.dumps({"query": query}).encode("utf-8")
        with self._request(
            "https://api.github.com/graphql",
            data=body,
            headers={"Content-Type": "application/json"},
        ) as response:
            payload = json.load(response)
        if payload.get("errors"):
            raise RuntimeError(f"GitHub GraphQL error: {payload['errors']}")
        return payload["data"]

    def search_code(self, query: str, *, page: int = 1, per_page: int = 100) -> tuple[dict[str, Any], dict[str, str]]:
        params = urllib.parse.urlencode({"q": query, "page": page, "per_page": per_page})
        return self.get_json(f"https://api.github.com/search/code?{params}")

    def search_code_count(self, query: str) -> int:
        payload, _ = self.search_code(query, page=1, per_page=1)
        return int(payload.get("total_count", 0))

    def fetch_repo_metadata(self, repo_full_name: str) -> dict[str, Any]:
        owner, repo = repo_full_name.split("/", 1)
        return self.get_json(f"https://api.github.com/repos/{owner}/{repo}")[0]

    def fetch_repo_tree(self, repo_full_name: str, ref: str) -> dict[str, Any]:
        owner, repo = repo_full_name.split("/", 1)
        ref_quoted = urllib.parse.quote(ref, safe="")
        return self.get_json(f"https://api.github.com/repos/{owner}/{repo}/git/trees/{ref_quoted}?recursive=1")[0]

    def fetch_repo_archive(self, repo_full_name: str, ref: str) -> bytes:
        ref_quoted = urllib.parse.quote(ref, safe="")
        archive_url = f"https://codeload.github.com/{repo_full_name}/tar.gz/refs/heads/{ref_quoted}"
        with self._request(
            archive_url,
            headers={"Accept": "application/octet-stream"},
        ) as response:
            return response.read()

    def fetch_bytes_from_repo_path(self, repo_full_name: str, ref: str, path: str) -> bytes:
        path_quoted = urllib.parse.quote(str(PurePosixPath(path)), safe="/")
        ref_quoted = urllib.parse.quote(ref, safe="")
        raw_url = f"https://raw.githubusercontent.com/{repo_full_name}/{ref_quoted}/{path_quoted}"
        request = urllib.request.Request(raw_url, headers={"User-Agent": self.user_agent})
        with urllib.request.urlopen(request, timeout=self.raw_timeout) as response:
            return response.read()

    def fetch_raw_from_repo_path(self, repo_full_name: str, ref: str, path: str) -> str:
        return self.fetch_bytes_from_repo_path(repo_full_name, ref, path).decode("utf-8", errors="replace")

    def fetch_raw_from_html_url(self, html_url: str) -> str:
        if "/blob/" not in html_url:
            raise ValueError(f"Unexpected GitHub blob URL: {html_url}")
        raw_url = html_url.replace("https://github.com/", "https://raw.githubusercontent.com/").replace("/blob/", "/")
        request = urllib.request.Request(raw_url, headers={"User-Agent": self.user_agent})
        with urllib.request.urlopen(request, timeout=self.raw_timeout) as response:
            return response.read().decode("utf-8", errors="replace")
