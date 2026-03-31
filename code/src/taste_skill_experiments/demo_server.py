from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer


ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = ROOT / "demo_static"


@dataclass
class DemoState:
    terrain_rows: list[dict]
    skillrank_scores: np.ndarray
    vectorizer: TfidfVectorizer
    tfidf_matrix: np.ndarray


class DemoHandler(SimpleHTTPRequestHandler):
    state: DemoState

    def __init__(self, *args, directory: str | None = None, **kwargs):
        super().__init__(*args, directory=directory or str(STATIC_DIR), **kwargs)

    def _send_json(self, payload: dict | list, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(encoded)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT.value)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/terrain":
            self._send_json({"items": self.state.terrain_rows})
            return
        if parsed.path == "/api/skills":
            params = parse_qs(parsed.query)
            skill_id = params.get("id", [""])[0]
            for row in self.state.terrain_rows:
                if row["skill_id"] == skill_id:
                    self._send_json(row)
                    return
            self._send_json({"error": "skill not found"}, status=HTTPStatus.NOT_FOUND)
            return
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/recommend":
            self._send_json({"error": "unsupported endpoint"}, status=HTTPStatus.NOT_FOUND)
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(content_length) or b"{}")
        query = " ".join(str(payload.get("query", "")).split())
        top_k = max(1, min(int(payload.get("k", 8)), 20))
        candidate_count = max(top_k * 6, 24)
        if not query:
            self._send_json({"error": "query is required"}, status=HTTPStatus.BAD_REQUEST)
            return

        query_vector = self.state.vectorizer.transform([query])
        scores = (self.state.tfidf_matrix @ query_vector.T).toarray().ravel()
        candidate_indices = np.argsort(scores)[::-1][: min(candidate_count, len(self.state.terrain_rows))]
        scored_rows = []
        for idx in candidate_indices:
            similarity = float(scores[int(idx)])
            row = dict(self.state.terrain_rows[int(idx)])
            row["query_similarity"] = round(float(similarity), 4)
            row["composite_score"] = round(float(0.45 * similarity + 0.55 * self.state.skillrank_scores[int(idx)]), 4)
            scored_rows.append(row)
        scored_rows.sort(key=lambda item: (item["composite_score"], item["z"]), reverse=True)
        self._send_json({"items": scored_rows[:top_k]})


def build_state(results_dir: Path, dataset_size: int) -> DemoState:
    dataset_dir = results_dir / "artifacts" / str(dataset_size)
    terrain_rows = json.loads((dataset_dir / "terrain.json").read_text())
    skillrank_scores = np.load(dataset_dir / "skillrank_scores_for_demo.npy")
    descriptions = [f"{row['name']} {row['description']} {row['cluster_label']}" for row in terrain_rows]
    vectorizer = TfidfVectorizer(stop_words="english", max_features=5000, ngram_range=(1, 2))
    tfidf_matrix = vectorizer.fit_transform(descriptions)
    return DemoState(
        terrain_rows=terrain_rows,
        skillrank_scores=skillrank_scores,
        vectorizer=vectorizer,
        tfidf_matrix=tfidf_matrix,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the Taste thesis demo.")
    parser.add_argument("--results-dir", default=str(ROOT / "results" / "nightly-20260330"))
    parser.add_argument("--dataset-size", type=int, default=1000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    DemoHandler.state = build_state(Path(args.results_dir), args.dataset_size)
    server = ThreadingHTTPServer((args.host, args.port), DemoHandler)
    print(f"Demo server running at http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
