const state = {
  terrain: [],
  plot: null,
  highlighted: new Set(),
};

async function fetchJSON(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.error || `Request failed: ${response.status}`);
  }
  return response.json();
}

function renderStats(items) {
  const clusterCount = new Set(items.map((item) => item.cluster_id)).size;
  const maxScore = Math.max(...items.map((item) => item.z));
  const topStar = Math.max(...items.map((item) => item.github_stars));
  const root = document.getElementById("stats");
  root.innerHTML = `
    <div class="stat"><span class="stat-label">Skills</span><span class="stat-value">${items.length}</span></div>
    <div class="stat"><span class="stat-label">Clusters</span><span class="stat-value">${clusterCount}</span></div>
    <div class="stat"><span class="stat-label">Max SkillRank</span><span class="stat-value">${maxScore.toFixed(2)}</span></div>
    <div class="stat"><span class="stat-label">Top GitHub Stars</span><span class="stat-value">${topStar.toLocaleString()}</span></div>
  `;
}

function clusterColor(clusterId) {
  const palette = ["#0d6e6e", "#c34a36", "#335c67", "#8f2d56", "#556b2f", "#3f88c5", "#7c4dff", "#b56b45", "#118ab2", "#ef476f"];
  return palette[Math.abs(clusterId) % palette.length];
}

function webglAvailable() {
  const canvas = document.createElement("canvas");
  return Boolean(canvas.getContext("webgl") || canvas.getContext("experimental-webgl"));
}

function plotTerrain(items) {
  const baseTrace = {
    x: items.map((item) => item.x),
    y: items.map((item) => item.y),
    text: items.map((item) => `${item.name}<br>${item.cluster_label}<br>SkillRank ${item.z.toFixed(3)}`),
    customdata: items.map((item) => item.skill_id),
    hovertemplate: "%{text}<extra></extra>",
    marker: {
      size: items.map((item) => {
        const size = 7 + item.z * 12;
        return state.highlighted.has(item.skill_id) ? size + 4 : size;
      }),
      color: items.map((item) => (state.highlighted.has(item.skill_id) ? "#f29e4c" : clusterColor(item.cluster_id))),
      opacity: items.map((item) => (state.highlighted.has(item.skill_id) ? 1 : 0.72)),
      line: {
        color: "rgba(255,255,255,0.28)",
        width: 0.5,
      },
    },
  };

  const layout = {
    margin: { l: 0, r: 0, b: 0, t: 0 },
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
  };

  const trace = webglAvailable()
    ? {
        ...baseTrace,
        type: "scatter3d",
        mode: "markers",
        z: items.map((item) => item.z),
      }
    : {
        ...baseTrace,
        type: "scatter",
        mode: "markers",
      };

  if (trace.type === "scatter3d") {
    layout.scene = {
      xaxis: { title: "PC1", gridcolor: "rgba(0,0,0,0.08)", zerolinecolor: "rgba(0,0,0,0.08)" },
      yaxis: { title: "PC2", gridcolor: "rgba(0,0,0,0.08)", zerolinecolor: "rgba(0,0,0,0.08)" },
      zaxis: { title: "SkillRank", gridcolor: "rgba(0,0,0,0.08)", zerolinecolor: "rgba(0,0,0,0.08)" },
      camera: {
        eye: { x: 1.3, y: 1.45, z: 0.9 },
      },
    };
  } else {
    layout.xaxis = { title: "PC1", gridcolor: "rgba(0,0,0,0.08)", zerolinecolor: "rgba(0,0,0,0.08)" };
    layout.yaxis = { title: "PC2", gridcolor: "rgba(0,0,0,0.08)", zerolinecolor: "rgba(0,0,0,0.08)" };
  }

  Plotly.react("terrain", [trace], layout, { responsive: true, displayModeBar: false });
}

function renderResults(items) {
  const root = document.getElementById("results");
  if (!items.length) {
    root.innerHTML = `<div class="empty">输入任务描述后，这里会显示按 SkillRank 重排后的推荐结果。</div>`;
    return;
  }
  root.innerHTML = items
    .map(
      (item, index) => `
      <article class="result-item">
        <div class="result-head">
          <div>
            <div class="result-rank">Top ${index + 1}</div>
            <div class="result-title">${item.name}</div>
          </div>
          <div class="result-rank">SR ${item.z.toFixed(3)}</div>
        </div>
        <div class="result-meta">${item.cluster_label} · ${item.github_repo} · ${item.github_stars.toLocaleString()} stars</div>
        <div class="result-desc">${item.description}</div>
      </article>
    `,
    )
    .join("");
}

async function loadTerrain() {
  const payload = await fetchJSON("/api/terrain");
  state.terrain = payload.items;
  renderStats(payload.items);
  plotTerrain(payload.items);
  renderResults([]);
}

function applyQueryParams() {
  const params = new URLSearchParams(window.location.search);
  const query = params.get("query");
  const k = params.get("k");
  if (query) {
    document.getElementById("query").value = query;
  }
  if (k) {
    const slider = document.getElementById("k");
    slider.value = k;
    document.getElementById("k-value").textContent = k;
  }
  return Boolean(query);
}

async function runQuery() {
  const query = document.getElementById("query").value.trim();
  const k = Number(document.getElementById("k").value);
  if (!query) {
    return;
  }
  const payload = await fetchJSON("/api/recommend", {
    method: "POST",
    body: JSON.stringify({ query, k }),
  });
  state.highlighted = new Set(payload.items.map((item) => item.skill_id));
  plotTerrain(state.terrain);
  renderResults(payload.items);
}

document.getElementById("k").addEventListener("input", (event) => {
  document.getElementById("k-value").textContent = event.target.value;
});

document.getElementById("run-query").addEventListener("click", () => {
  runQuery().catch((error) => {
    console.error(error);
    alert(error.message);
  });
});

loadTerrain()
  .then(async () => {
    if (applyQueryParams()) {
      await runQuery();
    }
  })
  .catch((error) => {
    console.error(error);
    document.getElementById("results").innerHTML = `<div class="empty">初始化失败：${error.message}</div>`;
  });
