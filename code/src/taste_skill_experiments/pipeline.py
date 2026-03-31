from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import faiss
import numpy as np
from scipy import sparse
from scipy.stats import spearmanr
from sentence_transformers import SentenceTransformer
from sklearn.cluster import MiniBatchKMeans
from sklearn.decomposition import PCA
from sklearn.feature_extraction.text import TfidfVectorizer


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"


@dataclass(frozen=True)
class ExperimentConfig:
    model_name: str = "sentence-transformers/all-MiniLM-L6-v2"
    batch_size: int = 48
    description_chars: int = 480
    content_chars: int = 2400
    graph_k: int = 12
    damping: float = 0.82
    nprobe: int = 8
    pagerank_weight: float = 0.3
    max_iter: int = 60
    tol: float = 1e-6
    similarity_floor: float = 0.10
    candidate_multiplier: int = 6
    random_seed: int = 42


def _stable_hash(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:12]


def _clean_text(text: str, *, max_chars: int) -> str:
    normalized = " ".join((text or "").split())
    return normalized[:max_chars]


def _load_dataset(size: int) -> list[dict[str, Any]]:
    if size == 10028:
        path = DATA_DIR / "skills_full.json"
    else:
        path = DATA_DIR / f"skills_{size}.json"
    return json.loads(path.read_text())


def _cluster_count(size: int) -> int:
    return max(5, min(24, size // 50))


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_or_compute_embeddings(
    model: SentenceTransformer,
    texts: list[str],
    *,
    cache_path: Path,
    batch_size: int,
) -> np.ndarray:
    if cache_path.exists():
        return np.load(cache_path)

    embeddings = model.encode(
        texts,
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=True,
        convert_to_numpy=True,
    ).astype("float32")
    np.save(cache_path, embeddings)
    return embeddings


def _run_kmeans(embeddings: np.ndarray, n_clusters: int, seed: int) -> np.ndarray:
    n_clusters = max(2, min(n_clusters, len(embeddings)))
    model = MiniBatchKMeans(
        n_clusters=n_clusters,
        random_state=seed,
        batch_size=min(1024, len(embeddings)),
        n_init=10,
        max_iter=200,
        reassignment_ratio=0.02,
    )
    return model.fit_predict(embeddings)


def _cluster_centroid_scores(embeddings: np.ndarray, labels: np.ndarray) -> np.ndarray:
    scores = np.zeros(len(labels), dtype="float32")
    for label in sorted(set(int(v) for v in labels)):
        mask = labels == label
        cluster = embeddings[mask]
        centroid = cluster.mean(axis=0)
        norm = np.linalg.norm(centroid)
        if norm > 0:
            centroid = centroid / norm
        scores[mask] = cluster @ centroid
    return scores


def _random_scores(size: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.random(size=size, dtype=np.float32)


def _length_scores(records: list[dict[str, Any]]) -> np.ndarray:
    return np.asarray([len(item["content"]) for item in records], dtype="float32")


def _build_exact_knn(embeddings: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    k = max(1, min(k, len(embeddings) - 1))
    sims = embeddings @ embeddings.T
    np.fill_diagonal(sims, -np.inf)
    topk_idx = np.argpartition(sims, -k, axis=1)[:, -k:]
    topk_sims = np.take_along_axis(sims, topk_idx, axis=1)
    order = np.argsort(topk_sims, axis=1)[:, ::-1]
    indices = np.take_along_axis(topk_idx, order, axis=1)
    scores = np.take_along_axis(topk_sims, order, axis=1)
    return indices.astype("int32"), scores.astype("float32")


def _build_faiss_knn(embeddings: np.ndarray, k: int, nprobe: int) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    k = max(1, min(k, len(embeddings) - 1))
    dimension = embeddings.shape[1]
    if len(embeddings) <= 64:
        index = faiss.IndexFlatIP(dimension)
        index.add(embeddings)
        raw_scores, raw_indices = index.search(embeddings, min(k + 1, len(embeddings)))
        nlist = 1
        effective_nprobe = 1
    else:
        nlist = max(2, min(int(round(math.sqrt(len(embeddings)))), len(embeddings)))
        quantizer = faiss.IndexFlatIP(dimension)
        index = faiss.IndexIVFFlat(quantizer, dimension, nlist, faiss.METRIC_INNER_PRODUCT)
        index.train(embeddings)
        index.add(embeddings)
        index.nprobe = min(max(1, nprobe), nlist)
        raw_scores, raw_indices = index.search(embeddings, min(k + 1, len(embeddings)))
        effective_nprobe = int(index.nprobe)

    clean_indices: list[np.ndarray] = []
    clean_scores: list[np.ndarray] = []
    for row_idx, (row_indices, row_scores) in enumerate(zip(raw_indices, raw_scores, strict=True)):
        pairs = [(int(idx), float(score)) for idx, score in zip(row_indices, row_scores, strict=True) if idx >= 0 and idx != row_idx]
        pairs = pairs[:k]
        while len(pairs) < k:
            pairs.append((row_idx, 0.0))
        clean_indices.append(np.asarray([idx for idx, _ in pairs], dtype="int32"))
        clean_scores.append(np.asarray([score for _, score in pairs], dtype="float32"))

    return (
        np.vstack(clean_indices),
        np.vstack(clean_scores),
        {"nlist": nlist, "nprobe": effective_nprobe},
    )


def _build_transition(indices: np.ndarray, scores: np.ndarray, similarity_floor: float) -> sparse.csr_matrix:
    rows: list[int] = []
    cols: list[int] = []
    data: list[float] = []
    size = len(indices)
    for row_idx, (neighbors, weights) in enumerate(zip(indices, scores, strict=True)):
        clipped = np.clip(weights - similarity_floor, 0.0, None)
        if float(clipped.sum()) <= 0:
            clipped = np.clip(weights, 0.0, None)
        total = float(clipped.sum())
        if total <= 0:
            continue
        normalized = clipped / total
        for col_idx, weight in zip(neighbors, normalized, strict=True):
            if row_idx == int(col_idx):
                continue
            rows.append(row_idx)
            cols.append(int(col_idx))
            data.append(float(weight))
    return sparse.csr_matrix((data, (rows, cols)), shape=(size, size), dtype="float32")


def _pagerank(transition: sparse.csr_matrix, damping: float, max_iter: int, tol: float) -> tuple[np.ndarray, int]:
    size = transition.shape[0]
    scores = np.full(size, 1.0 / size, dtype="float32")
    teleport = np.full(size, (1.0 - damping) / size, dtype="float32")
    transposed = transition.transpose().tocsr()
    for iteration in range(1, max_iter + 1):
        updated = teleport + damping * (transposed @ scores)
        if np.linalg.norm(updated - scores, ord=1) < tol:
            scores = updated
            break
        scores = updated
    else:
        iteration = max_iter
    normalized = (scores - float(scores.min())) / max(float(scores.max() - scores.min()), 1e-8)
    return normalized.astype("float32"), iteration


def _weighted_spearman(scores: np.ndarray, stars: np.ndarray, labels: np.ndarray) -> tuple[float, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    weighted_values: list[tuple[float, int]] = []
    for label in sorted(set(int(v) for v in labels)):
        mask = labels == label
        if int(mask.sum()) < 4:
            continue
        rho, p_value = spearmanr(scores[mask], stars[mask])
        if np.isnan(rho):
            continue
        row = {
            "cluster_id": label,
            "size": int(mask.sum()),
            "spearman_rho": float(rho),
            "p_value": float(p_value) if not np.isnan(p_value) else None,
            "mean_star": float(np.mean(stars[mask])),
        }
        rows.append(row)
        weighted_values.append((float(rho), int(mask.sum())))
    denominator = sum(weight for _, weight in weighted_values) or 1
    weighted_rho = sum(rho * weight for rho, weight in weighted_values) / denominator
    return float(weighted_rho), rows


def _label_clusters(records: list[dict[str, Any]], labels: np.ndarray) -> dict[int, str]:
    texts = [f"{row['name']} {row['description']}" for row in records]
    vectorizer = TfidfVectorizer(stop_words="english", max_features=2000, ngram_range=(1, 2))
    matrix = vectorizer.fit_transform(texts)
    terms = np.asarray(vectorizer.get_feature_names_out())
    cluster_labels: dict[int, str] = {}
    for label in sorted(set(int(v) for v in labels)):
        mask = labels == label
        cluster_matrix = matrix[mask]
        scores = np.asarray(cluster_matrix.mean(axis=0)).ravel()
        top_indices = scores.argsort()[-3:][::-1]
        top_terms = [term for term in terms[top_indices] if term.strip()]
        cluster_labels[label] = " / ".join(top_terms) if top_terms else f"cluster-{label}"
    return cluster_labels


def _summarize_dataset(records: list[dict[str, Any]]) -> dict[str, Any]:
    stars = [int(row["github_stars"]) for row in records]
    description_lengths = [len(row["description"]) for row in records]
    content_lengths = [len(row["content"]) for row in records]
    return {
        "skill_count": len(records),
        "repo_count": len({row["github_repo"] for row in records}),
        "star_min": int(min(stars)),
        "star_max": int(max(stars)),
        "star_median": float(statistics.median(stars)),
        "star_mean": float(round(statistics.mean(stars), 2)),
        "description_mean_chars": float(round(statistics.mean(description_lengths), 2)),
        "content_mean_chars": float(round(statistics.mean(content_lengths), 2)),
    }


def _evaluate_ranker(
    *,
    content_embeddings: np.ndarray,
    content_prior_scores: np.ndarray,
    stars: np.ndarray,
    labels: np.ndarray,
    config: ExperimentConfig,
    use_faiss: bool,
) -> tuple[np.ndarray, dict[str, Any]]:
    skillrank_scores = np.zeros(len(content_embeddings), dtype="float32")
    graph_time = 0.0
    pagerank_time = 0.0
    total_edges = 0
    iteration_values: list[int] = []
    faiss_meta_rows: list[dict[str, Any]] = []

    for label in sorted(set(int(value) for value in labels)):
        cluster_indices = np.where(labels == label)[0]
        if len(cluster_indices) <= 1:
            continue

        cluster_embeddings = content_embeddings[cluster_indices]
        cluster_start = time.perf_counter()
        if use_faiss:
            indices, sims, faiss_meta = _build_faiss_knn(cluster_embeddings, config.graph_k, config.nprobe)
            faiss_meta_rows.append({"cluster_id": label, **faiss_meta})
        else:
            indices, sims = _build_exact_knn(cluster_embeddings, config.graph_k)
        graph_time += time.perf_counter() - cluster_start

        transition = _build_transition(indices, sims, config.similarity_floor)
        score_start = time.perf_counter()
        cluster_scores, iterations = _pagerank(transition, config.damping, config.max_iter, config.tol)
        pagerank_time += time.perf_counter() - score_start
        total_edges += int(transition.nnz)
        iteration_values.append(iterations)
        skillrank_scores[cluster_indices] = cluster_scores

    final_scores = (1.0 - config.pagerank_weight) * content_prior_scores + config.pagerank_weight * skillrank_scores
    rho, cluster_rows = _weighted_spearman(final_scores, stars, labels)
    metrics = {
        "rho": rho,
        "graph_build_seconds": round(graph_time, 4),
        "pagerank_seconds": round(pagerank_time, 4),
        "iterations": round(float(np.mean(iteration_values)), 2) if iteration_values else 0,
        "edge_count": total_edges,
        "faiss": faiss_meta_rows,
        "cluster_rows": cluster_rows,
        "pagerank_weight": config.pagerank_weight,
    }
    return final_scores.astype("float32"), metrics


def _benchmark_pair(
    content_embeddings: np.ndarray,
    content_prior_scores: np.ndarray,
    stars: np.ndarray,
    labels: np.ndarray,
    config: ExperimentConfig,
) -> dict[str, Any]:
    approx_scores, approx_metrics = _evaluate_ranker(
        content_embeddings=content_embeddings,
        content_prior_scores=content_prior_scores,
        stars=stars,
        labels=labels,
        config=config,
        use_faiss=True,
    )
    exact_scores, exact_metrics = _evaluate_ranker(
        content_embeddings=content_embeddings,
        content_prior_scores=content_prior_scores,
        stars=stars,
        labels=labels,
        config=config,
        use_faiss=False,
    )
    return {
        "faiss_seconds": round(approx_metrics["graph_build_seconds"] + approx_metrics["pagerank_seconds"], 4),
        "exact_seconds": round(exact_metrics["graph_build_seconds"] + exact_metrics["pagerank_seconds"], 4),
        "speedup": round((exact_metrics["graph_build_seconds"] + exact_metrics["pagerank_seconds"]) / max(approx_metrics["graph_build_seconds"] + approx_metrics["pagerank_seconds"], 1e-6), 2),
        "faiss_rho": approx_metrics["rho"],
        "exact_rho": exact_metrics["rho"],
        "rho_loss": round(exact_metrics["rho"] - approx_metrics["rho"], 4),
        "faiss_iterations": approx_metrics["iterations"],
        "exact_iterations": exact_metrics["iterations"],
        "faiss_scores": approx_scores,
    }


def _serialize_trace_row(round_idx: int, parameter: str, before: Any, after: Any, rho: float, accepted: bool) -> dict[str, Any]:
    return {
        "round": round_idx,
        "parameter": parameter,
        "before": before,
        "after": after,
        "rho": round(rho, 4),
        "accepted": accepted,
    }


def _neighbor_values(value: Any, candidates: list[Any]) -> list[Any]:
    try:
        index = candidates.index(value)
    except ValueError:
        return candidates
    values: list[Any] = []
    if index > 0:
        values.append(candidates[index - 1])
    if index < len(candidates) - 1:
        values.append(candidates[index + 1])
    return values


def _tune_config(
    *,
    content_embeddings: np.ndarray,
    content_prior_scores: np.ndarray,
    stars: np.ndarray,
    labels: np.ndarray,
    base_config: ExperimentConfig,
    max_rounds: int = 8,
) -> tuple[ExperimentConfig, list[dict[str, Any]]]:
    search_space = {
        "graph_k": [6, 8, 10, 12, 15, 18],
        "damping": [0.72, 0.78, 0.82, 0.85, 0.88],
        "nprobe": [2, 4, 6, 8, 12],
        "pagerank_weight": [0.1, 0.3, 0.5, 0.7, 1.0],
        "similarity_floor": [0.00, 0.05, 0.10, 0.15, 0.20],
    }

    current = base_config
    _, metrics = _evaluate_ranker(
        content_embeddings=content_embeddings,
        content_prior_scores=content_prior_scores,
        stars=stars,
        labels=labels,
        config=current,
        use_faiss=True,
    )
    current_rho = metrics["rho"]
    trace = [_serialize_trace_row(0, "baseline", None, None, current_rho, True)]

    for round_idx in range(1, max_rounds + 1):
        best_candidate = None
        best_rho = current_rho
        best_parameter = None
        best_before = None
        for parameter, candidates in search_space.items():
            current_value = getattr(current, parameter)
            for candidate_value in _neighbor_values(current_value, candidates):
                candidate_config = ExperimentConfig(**{**asdict(current), parameter: candidate_value})
                _, candidate_metrics = _evaluate_ranker(
                    content_embeddings=content_embeddings,
                    content_prior_scores=content_prior_scores,
                    stars=stars,
                    labels=labels,
                    config=candidate_config,
                    use_faiss=True,
                )
                rho = candidate_metrics["rho"]
                if rho > best_rho + 1e-4:
                    best_rho = rho
                    best_candidate = candidate_config
                    best_parameter = parameter
                    best_before = current_value

        if best_candidate is None:
            trace.append(_serialize_trace_row(round_idx, "stop", None, None, current_rho, False))
            break

        current = best_candidate
        current_rho = best_rho
        trace.append(_serialize_trace_row(round_idx, best_parameter or "", best_before, getattr(current, best_parameter or ""), current_rho, True))

    return current, trace


def _case_study_rows(
    records: list[dict[str, Any]],
    labels: np.ndarray,
    scores: np.ndarray,
    stars: np.ndarray,
    cluster_names: dict[int, str],
) -> dict[str, Any]:
    sizes = {label: int((labels == label).sum()) for label in sorted(set(int(v) for v in labels))}
    focus_cluster = max(sizes, key=sizes.get)
    mask = labels == focus_cluster
    cluster_indices = np.where(mask)[0]
    ordered = cluster_indices[np.argsort(scores[cluster_indices])[::-1]]
    stars_rank = np.argsort(stars[cluster_indices])[::-1]
    star_rank_lookup = {int(cluster_indices[idx]): rank + 1 for rank, idx in enumerate(stars_rank)}

    rows = []
    for idx in list(ordered[:5]) + list(ordered[-3:]):
        record = records[int(idx)]
        rows.append(
            {
                "skill_id": record["skill_id"],
                "name": record["name"],
                "cluster_id": focus_cluster,
                "cluster_label": cluster_names[focus_cluster],
                "skillrank_score": round(float(scores[int(idx)]), 4),
                "github_stars": int(record["github_stars"]),
                "star_rank": int(star_rank_lookup[int(idx)]),
                "github_repo": record["github_repo"],
            }
        )
    return {
        "cluster_id": int(focus_cluster),
        "cluster_label": cluster_names[focus_cluster],
        "cluster_size": sizes[focus_cluster],
        "rows": rows,
    }


def _cluster_sensitivity(
    *,
    description_embeddings: np.ndarray,
    content_embeddings: np.ndarray,
    content_prior_scores: np.ndarray,
    stars: np.ndarray,
    base_clusters: int,
    config: ExperimentConfig,
    seed: int,
) -> list[dict[str, Any]]:
    rows = []
    for n_clusters in sorted({max(4, base_clusters - 4), base_clusters, base_clusters + 4}):
        labels = _run_kmeans(description_embeddings, n_clusters=n_clusters, seed=seed)
        _, metrics = _evaluate_ranker(
            content_embeddings=content_embeddings,
            content_prior_scores=content_prior_scores,
            stars=stars,
            labels=labels,
            config=config,
            use_faiss=True,
        )
        cluster_sizes = [int((labels == label).sum()) for label in sorted(set(int(v) for v in labels))]
        rows.append(
            {
                "n_clusters": int(n_clusters),
                "max_cluster_size": int(max(cluster_sizes)),
                "min_cluster_size": int(min(cluster_sizes)),
                "rho": round(metrics["rho"], 4),
            }
        )
    return rows


def _terrain_rows(
    records: list[dict[str, Any]],
    coords: np.ndarray,
    labels: np.ndarray,
    scores: np.ndarray,
    cluster_names: dict[int, str],
) -> list[dict[str, Any]]:
    rows = []
    for idx, record in enumerate(records):
        rows.append(
            {
                "skill_id": record["skill_id"],
                "name": record["name"],
                "description": record["description"],
                "github_stars": int(record["github_stars"]),
                "github_repo": record["github_repo"],
                "github_url": record["github_url"],
                "cluster_id": int(labels[idx]),
                "cluster_label": cluster_names[int(labels[idx])],
                "x": round(float(coords[idx, 0]), 6),
                "y": round(float(coords[idx, 1]), 6),
                "z": round(float(scores[idx]), 6),
            }
        )
    return rows


def run_experiment_suite(
    *,
    sizes: list[int],
    output_dir: Path,
    config: ExperimentConfig,
) -> dict[str, Any]:
    output_dir = _ensure_dir(output_dir)
    artifact_dir = _ensure_dir(output_dir / "artifacts")
    logs_dir = _ensure_dir(output_dir / "logs")

    summary: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sizes": sizes,
        "config": asdict(config),
        "datasets": {},
        "autoresearch_trace": [],
    }

    model = SentenceTransformer(config.model_name)
    model.max_seq_length = 256

    tuned_config = config
    cached_payloads: dict[int, dict[str, Any]] = {}

    for size in sizes:
        records = _load_dataset(size)
        dataset_dir = _ensure_dir(artifact_dir / str(size))
        dataset_log = logs_dir / f"dataset_{size}.json"

        desc_texts = [_clean_text(item["description"], max_chars=config.description_chars) for item in records]
        content_texts = [_clean_text(item["content"], max_chars=config.content_chars) for item in records]

        desc_cache = dataset_dir / f"description_embeddings_{_stable_hash({'size': size, 'chars': config.description_chars})}.npy"
        content_cache = dataset_dir / f"content_embeddings_{_stable_hash({'size': size, 'chars': config.content_chars})}.npy"
        desc_embeddings = _load_or_compute_embeddings(model, desc_texts, cache_path=desc_cache, batch_size=config.batch_size)
        content_embeddings = _load_or_compute_embeddings(model, content_texts, cache_path=content_cache, batch_size=config.batch_size)

        labels = _run_kmeans(desc_embeddings, _cluster_count(size), config.random_seed)
        cluster_names = _label_clusters(records, labels)
        coords = PCA(n_components=2, random_state=config.random_seed).fit_transform(desc_embeddings)
        stars = np.asarray([int(item["github_stars"]) for item in records], dtype="float32")
        content_prior_scores = _cluster_centroid_scores(content_embeddings, labels)

        payload = {
            "records": records,
            "desc_embeddings": desc_embeddings,
            "content_embeddings": content_embeddings,
            "content_prior_scores": content_prior_scores,
            "labels": labels,
            "cluster_names": cluster_names,
            "coords": coords,
            "stars": stars,
        }
        cached_payloads[size] = payload

        summary["datasets"][str(size)] = {
            "dataset_stats": _summarize_dataset(records),
            "cluster_count": int(len(set(int(v) for v in labels))),
            "cluster_labels": cluster_names,
        }
        dataset_log.write_text(json.dumps(summary["datasets"][str(size)], ensure_ascii=False, indent=2))

    tuning_size = 1000 if 1000 in cached_payloads else max(sizes)
    tuned_config, trace = _tune_config(
        content_embeddings=cached_payloads[tuning_size]["content_embeddings"],
        content_prior_scores=cached_payloads[tuning_size]["content_prior_scores"],
        stars=cached_payloads[tuning_size]["stars"],
        labels=cached_payloads[tuning_size]["labels"],
        base_config=config,
    )
    summary["tuned_config"] = asdict(tuned_config)
    summary["autoresearch_trace"] = trace
    (logs_dir / "autoresearch_trace.json").write_text(json.dumps(trace, ensure_ascii=False, indent=2))

    for size in sizes:
        payload = cached_payloads[size]
        records = payload["records"]
        labels = payload["labels"]
        cluster_names = payload["cluster_names"]
        coords = payload["coords"]
        stars = payload["stars"]
        content_embeddings = payload["content_embeddings"]
        description_embeddings = payload["desc_embeddings"]
        content_prior_scores = payload["content_prior_scores"]

        random_rho, _ = _weighted_spearman(_random_scores(len(records), config.random_seed), stars, labels)
        length_rho, _ = _weighted_spearman(_length_scores(records), stars, labels)
        desc_rho, _ = _weighted_spearman(_cluster_centroid_scores(description_embeddings, labels), stars, labels)
        content_centroid_rho, _ = _weighted_spearman(content_prior_scores, stars, labels)

        initial_scores, initial_metrics = _evaluate_ranker(
            content_embeddings=content_embeddings,
            content_prior_scores=content_prior_scores,
            stars=stars,
            labels=labels,
            config=config,
            use_faiss=True,
        )
        tuned_scores, tuned_metrics = _evaluate_ranker(
            content_embeddings=content_embeddings,
            content_prior_scores=content_prior_scores,
            stars=stars,
            labels=labels,
            config=tuned_config,
            use_faiss=True,
        )
        benchmarks = _benchmark_pair(content_embeddings, content_prior_scores, stars, labels, tuned_config)
        sensitivity = _cluster_sensitivity(
            description_embeddings=description_embeddings,
            content_embeddings=content_embeddings,
            content_prior_scores=content_prior_scores,
            stars=stars,
            base_clusters=_cluster_count(size),
            config=tuned_config,
            seed=config.random_seed,
        )
        case_study = _case_study_rows(records, labels, tuned_scores, stars, cluster_names)

        dataset_summary = summary["datasets"][str(size)]
        dataset_summary["ranking_comparison"] = {
            "random": round(random_rho, 4),
            "content_length": round(length_rho, 4),
            "description_centroid": round(desc_rho, 4),
            "content_centroid": round(content_centroid_rho, 4),
            "skillrank_initial": round(initial_metrics["rho"], 4),
            "skillrank_tuned": round(tuned_metrics["rho"], 4),
        }
        dataset_summary["initial_metrics"] = initial_metrics
        dataset_summary["tuned_metrics"] = tuned_metrics
        dataset_summary["benchmarks"] = {
            key: value
            for key, value in benchmarks.items()
            if key != "faiss_scores"
        }
        dataset_summary["cluster_sensitivity"] = sensitivity
        dataset_summary["case_study"] = case_study

        terrain_rows = _terrain_rows(records, coords, labels, tuned_scores, cluster_names)
        dataset_dir = artifact_dir / str(size)
        (dataset_dir / "terrain.json").write_text(json.dumps(terrain_rows, ensure_ascii=False, indent=2))
        (dataset_dir / "ranking_summary.json").write_text(json.dumps(dataset_summary, ensure_ascii=False, indent=2))
        np.save(dataset_dir / "description_embeddings_for_demo.npy", description_embeddings.astype("float32"))
        np.save(dataset_dir / "skillrank_scores_for_demo.npy", tuned_scores.astype("float32"))

    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Taste thesis experiments.")
    parser.add_argument("--sizes", default="100,1000", help="Comma-separated dataset sizes to evaluate.")
    parser.add_argument("--output-dir", default=str(ROOT / "results" / "nightly-20260330"), help="Output directory for artifacts and logs.")
    args = parser.parse_args()

    sizes = [int(value) for value in args.sizes.split(",") if value.strip()]
    summary = run_experiment_suite(sizes=sizes, output_dir=Path(args.output_dir), config=ExperimentConfig())
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
