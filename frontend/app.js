/**
 * Knowledge Graph UI — live sync with Neo4j via REST API.
 */

const API = "";
const TYPE_COLORS = {
  UserStory: "#a78bfa",
  Feature: "#fbbf24",
  APIEndpoint: "#fb7185",
  APIResponseSchema: "#f472b6",
  TestCase: "#4ade80",
};

/** Top-to-bottom entity layers (matches schema hierarchy). */
const TYPE_LAYER_ORDER = ["UserStory", "Feature", "APIEndpoint", "TestCase"];

const LAYER_LAYOUT = {
  gapY: 130,
  gapX: 150,
  paddingX: 80,
};

const ADD_ENDPOINTS = {
  user_story: "/api/nodes/stories",
  feature: "/api/nodes/features",
  endpoint: "/api/nodes/endpoints",
  test_case: "/api/nodes/testcases",
};

const DELETE_TYPES = {
  UserStory: "user_story",
  Feature: "feature",
  APIEndpoint: "api_endpoint",
  APIResponseSchema: "api_response_schema",
  TestCase: "test_case",
};

let cy = null;
let selectedNode = null;
let lastGraph = { nodes: [], edges: [], scoped_to_story: false };
const savedPositions = new Map();
let highlightNodeId = null;
let pendingFile = null;
let uploadVersionConfirmed = false;
let currentLayoutMode = "layered";
let layoutRunning = false;
let inventoryData = { nodes: [], total: 0, by_type: {} };
let lastStoryFlowDelta = null;
const graphCache = new Map();
let graphRequestSeq = 0;
let graphLoadingActiveSeq = 0;

const UPLOAD_TYPE_LABELS = {
  user_story: "User Story",
  feature: "Feature",
  test_case: "Test Case",
  api_spec: "API Spec",
  api_endpoint: "API Endpoint",
  bundle: "Bundle (API + features + story)",
};

const $ = (id) => document.getElementById(id);

function selectedStoryNodeId() {
  return $("storySelect").value || "";
}

function selectedStoryBaseId() {
  const opt = $("storySelect").selectedOptions[0];
  return opt?.dataset?.baseId || "";
}

/** Upload/manual APIs use story base_id (US1), not version node_id. */
function storyParam() {
  const baseId = selectedStoryBaseId();
  return baseId ? `?story_id=${encodeURIComponent(baseId)}` : "";
}

function storyGraphParams() {
  const nodeId = selectedStoryNodeId();
  const baseId = selectedStoryBaseId();
  const p = new URLSearchParams();
  if (nodeId) p.set("story_node_id", nodeId);
  if (baseId) p.set("story_id", baseId);
  const q = p.toString();
  return q ? `?${q}` : "";
}

function showToast(msg, type = "success") {
  const el = $("toast");
  el.textContent = msg;
  el.className = `toast ${type}`;
  setTimeout(() => el.classList.add("hidden"), 3200);
}

async function api(path, opts = {}) {
  const url = `${API}${path}`;
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(formatApiError(data, res.statusText));
  return data;
}

async function apiForm(path, formData) {
  const res = await fetch(`${API}${path}`, { method: "POST", body: formData });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (res.status === 409 && data.detail?.code === "duplicate" && data.detail.duplicates?.length) {
      const err = new Error(data.detail.duplicates[0].message || "Duplicate upload");
      err.isDuplicate = true;
      err.duplicates = data.detail.duplicates;
      throw err;
    }
    throw new Error(formatApiError(data, res.statusText));
  }
  return data;
}

function formatApiError(data, fallback) {
  if (data.detail?.code === "duplicate" && data.detail.duplicates?.length) {
    return data.detail.duplicates[0].message;
  }
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail)) return data.detail.map((d) => d.msg || d).join("; ");
  return data.message || fallback;
}

function showDuplicateModal(duplicates) {
  const modal = $("duplicateModal");
  const list = $("duplicateList");
  if (!modal || !list) return;
  list.innerHTML = duplicates
    .map((d) => {
      const label = escapeHtml(d.type_label || d.entity_type || "Node");
      const name = escapeHtml(d.name || d.base_id || "—");
      const ver = d.version != null ? ` <span class="hint">v${d.version}</span>` : "";
      return `<li><strong>${label}</strong>: ${name}${ver} — ${escapeHtml(d.message || "Already exists")}</li>`;
    })
    .join("");
  modal.classList.remove("hidden");
}

function hideDuplicateModal() {
  $("duplicateModal")?.classList.add("hidden");
}

function hideUploadConfirmModal() {
  $("uploadConfirmModal")?.classList.add("hidden");
}

function showUploadConfirmModal(preview) {
  const modal = $("uploadConfirmModal");
  const text = $("uploadConfirmText");
  if (!modal) return;
  const id = preview.version_target?.base_id || preview.preview?.assigned_id || "this item";
  const delta = formatChangePreview(preview.change_preview);
  if (text) {
    text.textContent =
      `Upload will create a new version of ${id}. The previous version remains in the graph for history.` +
      (delta ? ` Changes: ${delta}.` : "");
  }
  modal.classList.remove("hidden");
}

function resetUploadConfirmState() {
  uploadVersionConfirmed = false;
  hideUploadConfirmModal();
}

function uploadQueryParams() {
  const params = new URLSearchParams();
  const baseId = selectedStoryBaseId();
  if (baseId) params.set("story_id", baseId);
  const forced = $("uploadType").value;
  if (forced && forced !== "auto") params.set("entity_type", forced);
  if (selectedNode) {
    params.set("parent_type", selectedNode.type);
    params.set("parent_base_id", selectedNode.base_id);
  }
  const q = params.toString();
  return q ? `?${q}` : "";
}

function updateUploadHint() {
  const hint = $("uploadHint");
  if (!selectedNode) {
    hint.textContent =
      "Drop JSON/YAML — bundle, user story, feature, testcase, or OpenAPI (no flow files).";
    return;
  }
  const tips = {
    UserStory: `Upload features/testcases — story flows: ${(selectedNode.properties?.flows || []).join(" → ")}`,
    Feature: "Upload testcase with linked_to this feature id or name.",
  };
  hint.textContent = tips[selectedNode.type] || `New nodes will appear beside ${selectedNode.label}.`;
}

function formatChangePreview(cp) {
  if (!cp?.has_changes) return "";
  const parts = [];
  const names = (list) => (list || []).map((f) => f.name || f).filter(Boolean).join(", ");
  const add = names(cp.added);
  const mod = names(cp.modified);
  const rem = names(cp.removed);
  if (add) parts.push(`Added: ${add}`);
  if (mod) parts.push(`Modified: ${mod}`);
  if (rem) parts.push(`Removed: ${rem}`);
  if (!parts.length && cp.change_type === "content") parts.push("Content changed");
  return parts.join(" · ");
}

function renderUploadPreview(data) {
  const box = $("uploadPreview");
  const p = data.preview || {};
  const typeLabel = UPLOAD_TYPE_LABELS[data.entity_type] || data.entity_type;
  const isAuto = $("uploadType").value === "auto";
  let rows = "";

  if (data.entity_type === "api_spec") {
    rows = `
      <dt>Title</dt><dd>${escapeHtml(p.title || "—")}</dd>
      <dt>Version</dt><dd>${escapeHtml(String(p.version || "—"))}</dd>
      <dt>Endpoints</dt><dd>${p.endpoint_count || 0}</dd>`;
    if (p.endpoints?.length) {
      rows += `<dt>Sample</dt><dd>${p.endpoints.map(escapeHtml).join("<br>")}</dd>`;
    }
  } else if (data.entity_type === "bundle") {
    rows = `
      <dt>Bundle</dt><dd>${escapeHtml(p.title || "—")}</dd>
      <dt>API endpoints</dt><dd>${p.endpoint_count || 0}</dd>
      <dt>Features</dt><dd>${p.feature_count || 0}</dd>
      <dt>Stories</dt><dd>${p.story_count || 0}</dd>
      <dt>Test cases</dt><dd>${p.test_case_count || 0}</dd>`;
  } else {
    const entries = Object.entries(p).filter(([k]) => k !== "content_preview");
    rows = entries.map(([k, v]) => {
      const val = Array.isArray(v) ? v.join(", ") : String(v ?? "");
      return `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(val)}</dd>`;
    }).join("");
    if (p.content_preview) {
      rows += `<dt>content</dt><dd>${escapeHtml(p.content_preview)}…</dd>`;
    }
    if (p.assigned_id) {
      rows += `<dt>Assigned ID</dt><dd>${escapeHtml(p.assigned_id)}</dd>`;
    }
  }

  box.innerHTML = `
    ${isAuto ? `<span class="detect-badge">Auto-detected: ${escapeHtml(typeLabel)}</span>` : `<span class="type-badge">${escapeHtml(typeLabel)}</span>`}
    ${data.item_count > 1 ? `<div class="hint">${data.item_count} items in file</div>` : ""}
    <dl>${rows}</dl>`;
  box.classList.remove("hidden");
}

function clearPendingFile() {
  pendingFile = null;
  $("fileChip").classList.add("hidden");
  $("uploadPreview").classList.add("hidden");
  $("btnUpload").disabled = true;
  $("fileInput").value = "";
  hideDuplicateModal();
  resetUploadConfirmState();
}

async function handleFileSelected(file) {
  if (!file) return;
  pendingFile = file;
  $("fileChip").classList.remove("hidden");
  $("fileChip").innerHTML = `
    <span class="name" title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</span>
    <button type="button" aria-label="Remove file">×</button>`;
  $("fileChip").querySelector("button").onclick = clearPendingFile;

  const fd = new FormData();
  fd.append("file", file);
  $("btnUpload").disabled = true;
  $("uploadPreview").innerHTML = '<span class="hint">Parsing…</span>';
  $("uploadPreview").classList.remove("hidden");

  try {
    const preview = await apiForm(`/api/upload/preview${uploadQueryParams()}`, fd);
    renderUploadPreview(preview);
    if (preview.has_duplicates && preview.duplicates?.length) {
      showDuplicateModal(preview.duplicates);
      $("btnUpload").disabled = true;
      return;
    }
    hideDuplicateModal();
    if (preview.will_create_version) {
      const delta = formatChangePreview(preview.change_preview);
      const id = preview.version_target?.base_id || preview.preview?.assigned_id || "entity";
      $("uploadPreview").innerHTML +=
        `<div class="hint" style="margin-top:.5rem">` +
        `New version for <strong>${escapeHtml(id)}</strong> — previous version stays in history` +
        `${delta ? ` (${escapeHtml(delta)})` : ""}. Click <strong>Continue</strong> to proceed.</div>`;
      uploadVersionConfirmed = false;
      showUploadConfirmModal(preview);
      $("btnUpload").disabled = true;
    } else {
      resetUploadConfirmState();
      $("btnUpload").disabled = false;
    }
  } catch (err) {
    $("uploadPreview").innerHTML = `<span class="hint" style="color:var(--danger)">${escapeHtml(err.message)}</span>`;
    $("btnUpload").disabled = true;
  }
}

async function uploadPendingFile() {
  if (!pendingFile) return;
  const confirmModal = $("uploadConfirmModal");
  if (confirmModal && !confirmModal.classList.contains("hidden") && !uploadVersionConfirmed) {
    return;
  }
  const fd = new FormData();
  fd.append("file", pendingFile);
  $("btnUpload").disabled = true;
  $("btnUpload").textContent = "Uploading…";

  try {
    const res = await apiForm(`/api/upload${uploadQueryParams()}`, fd);
    const newId = res.node_id;
    const storyNodeId = res.node_id || selectedStoryNodeId() || null;
    if (res.story_flow_delta) {
      lastStoryFlowDelta = res.story_flow_delta;
    }
    await reloadDashboard(newId, storyNodeId);
    const edgeMsg = res.edges_created?.length ? ` · ${res.edges_created.length} edge(s)` : "";
    const countMsg = res.count > 1 ? `${res.count} items` : res.base_id;
    const idMeta = res.identity?.[0];
    const deltaHint = idMeta?.delta_summary ? ` · ${idMeta.delta_summary}` : "";
    const verHint = idMeta?.is_version_update ? " (new version)" : "";
    const baseToast = `Uploaded ${countMsg}${verHint} from file${edgeMsg}${deltaHint}`;
    showToast(res.story_flow_delta?.has_changes ? res.message || baseToast : baseToast);
    clearPendingFile();
  } catch (err) {
    if (err.isDuplicate && err.duplicates?.length) {
      showDuplicateModal(err.duplicates);
    }
    showToast(err.message, "error");
    try {
      await reloadDashboard();
    } catch {
      /* Neo4j may still have the node from a partial upload */
    }
  } finally {
    $("btnUpload").disabled = !pendingFile;
    $("btnUpload").textContent = "Upload to Neo4j";
  }
}

function initUpload() {
  const dropZone = $("dropZone");
  const fileInput = $("fileInput");

  $("btnBrowse").addEventListener("click", (e) => {
    e.stopPropagation();
    fileInput.click();
  });
  dropZone.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => handleFileSelected(fileInput.files[0]));

  dropZone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropZone.classList.add("dragover");
  });
  dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragover"));
  dropZone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropZone.classList.remove("dragover");
    const file = e.dataTransfer.files[0];
    if (file) handleFileSelected(file);
  });

  $("btnUpload").addEventListener("click", uploadPendingFile);
  $("btnDuplicateClose")?.addEventListener("click", hideDuplicateModal);
  $("duplicateModal")?.addEventListener("click", (e) => {
    if (e.target.id === "duplicateModal") hideDuplicateModal();
  });

  $("btnUploadConfirmCancel")?.addEventListener("click", () => {
    resetUploadConfirmState();
    $("btnUpload").disabled = true;
  });
  $("btnUploadConfirmContinue")?.addEventListener("click", () => {
    uploadVersionConfirmed = true;
    hideUploadConfirmModal();
    if (pendingFile) $("btnUpload").disabled = false;
  });
  $("uploadConfirmModal")?.addEventListener("click", (e) => {
    if (e.target.id === "uploadConfirmModal") {
      resetUploadConfirmState();
      $("btnUpload").disabled = true;
    }
  });
  $("uploadType").addEventListener("change", (e) => {
    const sel = e.target;
    sel.dataset.manualLock = sel.value === "auto" ? "" : "true";
    if (pendingFile) handleFileSelected(pendingFile);
  });

  $("uploadHint").textContent =
    "Drop a file — type is auto-detected (story, feature, testcase, or OpenAPI).";

  $("toggleManual").addEventListener("click", () => {
    const btn = $("toggleManual");
    const section = $("manualSection");
    const open = btn.getAttribute("aria-expanded") === "true";
    btn.setAttribute("aria-expanded", open ? "false" : "true");
    section.classList.toggle("hidden", open);
  });
}

async function checkHealth() {
  await updatePersistenceStatus();
}

async function loadStories() {
  const { stories } = await api("/api/graph/stories");
  const sel = $("storySelect");
  const cur = sel.value || sessionStorage.getItem("kg_story_focus") || "";
  sel.innerHTML = '<option value="">All nodes (full graph)</option>';
  for (const s of stories) {
    const opt = document.createElement("option");
    opt.value = s.node_id;
    opt.dataset.baseId = s.base_id;
    const archived = s.is_current === false;
    opt.textContent = `${s.base_id} — ${s.title} v${s.version}${archived ? " (archived)" : ""}`;
    sel.appendChild(opt);
  }
  if (cur && [...sel.options].some((o) => o.value === cur)) sel.value = cur;
}

async function updatePersistenceStatus() {
  const pill = $("connStatus");
  try {
    const h = await api("/api/graph/health");
    const inv = await api("/api/graph/nodes");
    const n = inv.total ?? 0;
    if (h.connected) {
      pill.textContent = n ? `Neo4j · ${n} node(s) saved` : "Neo4j connected · empty";
      pill.className = "status-pill ok";
      pill.title = "Data is stored in your Neo4j database (persists across page reloads)";
    } else {
      pill.textContent = h.error || "Neo4j offline";
      pill.className = "status-pill err";
      pill.title = "Cannot reach Neo4j — uploads will not persist";
    }
  } catch (e) {
    pill.textContent = "API unreachable";
    pill.className = "status-pill err";
    pill.title = "";
  }
}

/** Full graph when no story selected; story subgraph when a story is chosen in the dropdown. */
async function fetchGraph() {
  const q = storyGraphParams();
  return api(`/api/graph${q}`);
}

function graphCacheKey() {
  return storyGraphParams() || "__all__";
}

function cloneGraph(graph) {
  if (typeof structuredClone === "function") return structuredClone(graph);
  return JSON.parse(JSON.stringify(graph));
}

function invalidateGraphCache() {
  graphCache.clear();
}

function setGraphLoading(loading, message = "Loading graph…") {
  const overlay = $("graphLoading");
  const text = $("graphLoadingText");
  const storySelect = $("storySelect");
  if (overlay) {
    overlay.classList.toggle("hidden", !loading);
  }
  if (text && message) {
    text.textContent = message;
  }
  if (storySelect) storySelect.disabled = loading;
}

async function fetchGraphCached({ force = false } = {}) {
  const key = graphCacheKey();
  if (!force && graphCache.has(key)) return cloneGraph(graphCache.get(key));
  const graph = await fetchGraph();
  graphCache.set(key, graph);
  return cloneGraph(graph);
}

function applyStoryVersionDelta(delta) {
  const banner = $("storyDeltaBanner");
  if (!cy) return;

  cy.elements().removeClass("version-added version-modified version-removed version-inactive");

  if (!delta?.has_changes) {
    banner?.classList.add("hidden");
    if (banner) banner.innerHTML = "";
    return;
  }

  const mark = (list, cls) => {
    for (const f of list || []) {
      if (!f?.node_id) continue;
      const el = cy.getElementById(f.node_id);
      if (el.length) el.addClass(cls);
    }
  };

  mark(delta.added, "version-added");
  mark(delta.modified, "version-modified");
  mark(delta.removed, "version-removed version-inactive");

  if (banner) {
    const parts = [];
    const add = (delta.added || []).map((f) => f.name).filter(Boolean);
    const mod = (delta.modified || []).map((f) => f.name).filter(Boolean);
    const rem = (delta.removed || []).map((f) => f.name).filter(Boolean);
    if (add.length) {
      parts.push(`<span class="delta-pill delta-add">+ Added: ${escapeHtml(add.join(", "))}</span>`);
    }
    if (mod.length) {
      parts.push(`<span class="delta-pill delta-modify">◎ Modified: ${escapeHtml(mod.join(", "))}</span>`);
    }
    if (rem.length) {
      parts.push(
        `<span class="delta-pill delta-remove">− Removed (inactive): ${escapeHtml(rem.join(", "))}</span>`,
      );
    }
    const ver =
      delta.previous_version != null
        ? `v${delta.previous_version} → v${delta.version}`
        : `v${delta.version}`;
    banner.innerHTML = `<span class="delta-title">Story flow change (${escapeHtml(ver)})</span>${parts.join("")}`;
    banner.classList.remove("hidden");
  }
}

/** Edge types that belong to a story's product graph (not cross-story/version links). */
const STORY_FOCUS_REL_TYPES = new Set([
  "HAS_FEATURE",
  "USES_API",
  "HAS_TEST_CASE",
  "DEPENDS_ON",
  "HAS_RESPONSE_SCHEMA",
  "VALIDATES_AGAINST",
  "PREVIOUS_VERSION",
]);

/**
 * Nodes/edges for one story: bounded BFS on product relationships only.
 * Does not walk BLOCKS (cross-story coupling); PREVIOUS_VERSION is version metadata only.
 */
function storyFocusCollection(storyNodes) {
  const storyNodeIds = new Set(storyNodes.map((n) => n.id()));

  const allowNode = (n) => {
    if (n.data("type") !== "UserStory") return true;
    return storyNodeIds.has(n.id());
  };

  const relOk = (e) => STORY_FOCUS_REL_TYPES.has(e.data("rel_type"));

  let focusNodes = storyNodes;
  let frontier = storyNodes;
  const maxDepth = 5;

  for (let d = 0; d < maxDepth; d++) {
    const stepEdges = frontier
      .connectedEdges()
      .filter(relOk)
      .filter((e) => allowNode(e.source()) && allowNode(e.target()));

    const stepNodes = stepEdges.connectedNodes().filter(allowNode);
    const newNodes = stepNodes.not(focusNodes);
    if (!newNodes.length) break;

    focusNodes = focusNodes.union(newNodes);
    frontier = newNodes;
  }

  const focusEdges = focusNodes
    .connectedEdges()
    .filter(relOk)
    .filter((e) => focusNodes.contains(e.source()) && focusNodes.contains(e.target()));

  return focusNodes.union(focusEdges);
}

function focusStoryView(storyNodeId) {
  if (!cy || !cy.nodes().length) return;
  cy.elements().removeClass("dimmed");
  if (!storyNodeId) {
    cy.fit(cy.elements(), 48);
    applyStoryVersionDelta(null);
    return;
  }
  const storyBaseId = selectedStoryBaseId();
  if (lastGraph?.scoped_to_story) {
    cy.fit(cy.elements(), 56);
    if (lastStoryFlowDelta?.story_id === storyBaseId) {
      applyStoryVersionDelta(lastStoryFlowDelta);
    }
    return;
  }
  const story = cy.nodes().filter((n) => n.data("type") === "UserStory" && n.id() === storyNodeId);
  if (!story.length) {
    cy.fit(cy.elements(), 48);
    return;
  }
  let hood = storyFocusCollection(story);
  if (lastStoryFlowDelta?.story_id === storyBaseId && lastStoryFlowDelta?.has_changes) {
    const deltaIds = [
      ...(lastStoryFlowDelta.added || []),
      ...(lastStoryFlowDelta.modified || []),
      ...(lastStoryFlowDelta.removed || []),
    ]
      .map((f) => f.node_id)
      .filter(Boolean);
    for (const id of deltaIds) {
      const n = cy.getElementById(id);
      if (n.length) hood = hood.union(n);
    }
  }
  const focusNodes = hood.nodes();
  const focusEdges = hood.edges();
  cy.nodes().not(focusNodes).addClass("dimmed");
  cy.edges().not(focusEdges).addClass("dimmed");
  if (focusNodes.length) cy.fit(hood.nonempty() ? hood : story, 56);
  if (lastStoryFlowDelta?.story_id === storyBaseId) {
    applyStoryVersionDelta(lastStoryFlowDelta);
  }
}

function clearStoryFocus() {
  if (cy) cy.elements().removeClass("dimmed");
}

function registerDagreLayout() {
  const ext = window.cytoscapeDagre || window.cytoscapeDagre?.default;
  if (ext) {
    cytoscape.use(ext);
    return true;
  }
  return false;
}

function resizeGraphViewport() {
  if (!cy) return;
  cy.resize();
}

function initGraphViewport() {
  const wrap = document.querySelector(".graph-wrap");
  if (!wrap) return;
  const ro = new ResizeObserver(() => {
    requestAnimationFrame(resizeGraphViewport);
  });
  ro.observe(wrap);
  window.addEventListener("resize", resizeGraphViewport);
}

function initCytoscape() {
  registerDagreLayout();
  if (cy) cy.destroy();
  cy = cytoscape({
    container: $("cy"),
    style: [
      {
        selector: "node",
        style: {
          label: "data(caption)",
          "text-valign": "bottom",
          "text-halign": "center",
          "text-margin-y": 8,
          "font-size": "11px",
          "font-family": "DM Sans, sans-serif",
          color: "#e8ecf4",
          "text-wrap": "wrap",
          "text-max-width": "130px",
          "text-background-color": "#0c0e14",
          "text-background-opacity": 0.92,
          "text-background-padding": "3px",
          "text-background-shape": "roundrectangle",
          "text-border-opacity": 0,
          width: 42,
          height: 42,
          "background-color": "data(color)",
          "border-width": 2.5,
          "border-color": "#1a2030",
          shape: "ellipse",
        },
      },
      {
        selector: "node:selected",
        style: {
          "border-width": 3.5,
          "border-color": "#fff",
          "overlay-opacity": 0.08,
          width: 46,
          height: 46,
        },
      },
      {
        selector: "node.dimmed",
        style: { opacity: 0.35 },
      },
      {
        selector: "node.archived-story, node.archived-version",
        style: {
          opacity: 0.78,
          "border-style": "dashed",
          "border-color": "#8b95ad",
          "border-width": 3,
        },
      },
      {
        selector: "node.version-added",
        style: {
          "border-color": "#3ecf8e",
          "border-width": 4,
          width: 48,
          height: 48,
        },
      },
      {
        selector: "node.version-modified",
        style: {
          "border-color": "#f5a623",
          "border-width": 4,
          width: 48,
          height: 48,
        },
      },
      {
        selector: "node.version-removed",
        style: {
          opacity: 0.42,
          "border-color": "#ef5f6b",
          "border-width": 3,
          "border-style": "dashed",
        },
      },
      {
        selector: "node.version-inactive",
        style: {
          "background-color": "#3a3f4a",
          color: "#8b95ad",
        },
      },
      {
        selector: "edge.dimmed",
        style: { opacity: 0.2 },
      },
      {
        selector: "node.new-highlight",
        style: {
          "border-color": "#fff",
          "border-width": 4,
          width: 50,
          height: 50,
        },
      },
      {
        selector: "edge",
        style: {
          width: 2,
          "line-color": "#4a5568",
          "target-arrow-color": "#6b7a99",
          "target-arrow-shape": "triangle",
          "curve-style": "bezier",
          label: "data(rel_type)",
          "font-size": "9px",
          "font-family": "JetBrains Mono, monospace",
          color: "#a8b4cc",
          "text-rotation": "autorotate",
          "text-margin-y": -14,
          "text-halign": "center",
          "text-background-color": "#0c0e14",
          "text-background-opacity": 0.9,
          "text-background-padding": "2px",
          "text-background-shape": "roundrectangle",
          "text-border-opacity": 0,
        },
      },
    ],
    layout: { name: "preset" },
    wheelSensitivity: 0.2,
    minZoom: 0.15,
    maxZoom: 3,
    boxSelectionEnabled: false,
  });

  resizeGraphViewport();

  cy.on("tap", "node", (evt) => selectNode(evt.target.data()));
  cy.on("tap", (evt) => {
    if (evt.target === cy) {
      selectedNode = null;
      updatePanel();
    }
  });

  cy.on("drag", "node", (evt) => {
    if (currentLayoutMode !== "layered") return;
    const node = evt.target;
    node.position({ x: node.position("x"), y: layerYForType(node.data("type")) });
  });

  cy.on("dragfree", "node", (evt) => {
    const node = evt.target;
    if (currentLayoutMode === "layered") {
      snapNodeToLayer(node);
      return;
    }
    const pos = node.position();
    savedPositions.set(node.id(), { x: pos.x, y: pos.y });
  });
}

function hierarchicalLayoutRoots() {
  if (!cy) return undefined;
  const stories = cy.nodes().filter((n) => n.data("type") === "UserStory");
  if (stories.length) return stories;
  return cy.nodes().filter((n) => n.indegree(false).length === 0);
}

function hierarchicalLayoutOptions() {
  if (registerDagreLayout()) {
    return {
      name: "dagre",
      rankDir: "TB",
      nodeSep: 55,
      rankSep: 95,
      edgesep: 20,
      ranker: "network-simplex",
      animate: true,
      animationDuration: 450,
      fit: true,
      padding: 48,
      randomize: true,
      nodeDimensionsIncludeLabels: true,
    };
  }
  return {
    name: "breadthfirst",
    directed: true,
    circle: false,
    spacingFactor: 1.35,
    animate: true,
    animationDuration: 450,
    fit: true,
    padding: 48,
    roots: hierarchicalLayoutRoots(),
  };
}

function forceLayoutOptions() {
  return {
    name: "cose",
    animate: true,
    animationDuration: 500,
    fit: true,
    padding: 48,
    nodeRepulsion: 9000,
    idealEdgeLength: 110,
    edgeElasticity: 0.45,
    gravity: 0.3,
    numIter: 1000,
  };
}

function nodesByTypeLayer() {
  const layers = TYPE_LAYER_ORDER.map(() => []);
  const other = [];
  cy.nodes().forEach((node) => {
    const t = node.data("type") || "";
    const idx = TYPE_LAYER_ORDER.indexOf(t);
    if (idx >= 0) layers[idx].push(node);
    else other.push(node);
  });
  if (other.length) layers.push(other);
  return layers;
}

function layerYForType(nodeType) {
  const idx = TYPE_LAYER_ORDER.indexOf(nodeType);
  const layerIdx = idx >= 0 ? idx : TYPE_LAYER_ORDER.length;
  return layerIdx * LAYER_LAYOUT.gapY;
}

function snapNodeToLayer(node) {
  const y = layerYForType(node.data("type"));
  const x = node.position("x");
  const pos = { x, y };
  node.position(pos);
  savedPositions.set(node.id(), { ...pos });
}

function orderLayerNodes(nodes) {
  return [...nodes].sort((a, b) =>
    String(a.data("label") || a.id()).localeCompare(String(b.data("label") || b.id()))
  );
}

/** Place nodes on one row; keep saved X when reset=false (manual drag within layer). */
function placeTypeLayer(nodes, y, { reset = false } = {}) {
  if (!nodes.length) return;
  const ordered = orderLayerNodes(nodes);
  const fixedXs = [];
  const autoNodes = [];

  ordered.forEach((node) => {
    if (!reset && savedPositions.has(node.id())) {
      fixedXs.push(savedPositions.get(node.id()).x);
      const pos = { x: savedPositions.get(node.id()).x, y };
      node.position(pos);
      savedPositions.set(node.id(), { ...pos });
    } else {
      autoNodes.push(node);
    }
  });

  if (!autoNodes.length) return;

  let x = fixedXs.length ? Math.max(...fixedXs) + LAYER_LAYOUT.gapX : 0;
  if (!fixedXs.length) {
    const width = (autoNodes.length - 1) * LAYER_LAYOUT.gapX;
    x = -width / 2;
  }
  const used = new Set(fixedXs);
  autoNodes.forEach((node) => {
    while (used.has(x)) x += LAYER_LAYOUT.gapX;
    const pos = { x, y };
    node.position(pos);
    savedPositions.set(node.id(), { ...pos });
    used.add(x);
    x += LAYER_LAYOUT.gapX;
  });
}

function runLayeredTypeLayout({ reset = false } = {}) {
  if (!cy || cy.nodes().length === 0) return;

  const layers = nodesByTypeLayer();
  let y = 0;
  layers.forEach((nodes) => {
    if (!nodes.length) return;
    placeTypeLayer(nodes, y, { reset });
    y += LAYER_LAYOUT.gapY;
  });

  cy.fit(cy.elements(), 48);
}

function setActiveLayoutMode(mode) {
  currentLayoutMode = mode;
  document.querySelectorAll(".layout-option").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.layout === mode);
  });
}

function runGraphLayout(mode = currentLayoutMode, { reset = false } = {}) {
  if (!cy || cy.nodes().length === 0) {
    showToast("No nodes to arrange", "error");
    return;
  }
  if (layoutRunning) return;

  layoutRunning = true;
  if (reset) savedPositions.clear();
  setActiveLayoutMode(mode);
  $("layoutMenu")?.classList.add("hidden");
  $("btnLayoutMenu")?.setAttribute("aria-expanded", "false");

  const finishLayout = () => {
    cy.nodes().forEach((node) => {
      if (mode === "layered") snapNodeToLayer(node);
      else savedPositions.set(node.id(), { ...node.position() });
    });
    const sid = selectedStoryNodeId();
    if (sid) focusStoryView(sid);
    else cy.fit(cy.elements(), 48);
    layoutRunning = false;
  };

  if (mode === "layered") {
    runLayeredTypeLayout({ reset });
    finishLayout();
    return;
  }

  const opts = mode === "force" ? forceLayoutOptions() : hierarchicalLayoutOptions();
  const layout = cy.layout({ ...opts, eles: cy.elements() });
  layout.one("layoutstop", finishLayout);
  layout.run();
}

function runHierarchicalLayout() {
  runGraphLayout("hierarchical");
}

function initLayoutControls() {
  const menu = $("layoutMenu");
  const fab = $("btnLayoutMenu");

  fab?.addEventListener("click", (e) => {
    e.stopPropagation();
    menu.classList.toggle("hidden");
    fab.setAttribute("aria-expanded", menu.classList.contains("hidden") ? "false" : "true");
  });

  menu?.querySelectorAll(".layout-option").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      runGraphLayout(btn.dataset.layout, { reset: true });
    });
  });

  document.addEventListener("click", () => {
    menu?.classList.add("hidden");
    fab?.setAttribute("aria-expanded", "false");
  });
  menu?.addEventListener("click", (e) => e.stopPropagation());
}

function positionNearParent(parentId, index = 0) {
  const parent = savedPositions.get(parentId);
  if (!parent && cy) {
    const el = cy.getElementById(parentId);
    if (el.length) {
      const p = el.position();
      savedPositions.set(parentId, { x: p.x, y: p.y });
      return { x: p.x + 140 + index * 24, y: p.y + (index % 2 === 0 ? -50 : 50) };
    }
  }
  if (parent) {
    return { x: parent.x + 140 + index * 20, y: parent.y + (index % 2 === 0 ? -55 : 55) };
  }
  return { x: 80 + Math.random() * 200, y: 80 + Math.random() * 200 };
}

function applyGraph(graph, newNodeId = null) {
  lastGraph = graph;
  if (!cy) initCytoscape();

  const elements = [];
  const parentForNew = selectedNode?.id;

  graph.nodes.forEach((n, i) => {
    let pos = savedPositions.get(n.id);
    if (pos && currentLayoutMode === "layered") {
      pos = { x: pos.x, y: layerYForType(n.type) };
      savedPositions.set(n.id, pos);
    }
    if (!pos && newNodeId === n.id && parentForNew) {
      pos = positionNearParent(parentForNew, i);
      if (currentLayoutMode === "layered") {
        pos.y = layerYForType(n.type);
      }
      savedPositions.set(n.id, pos);
    }
    const classes = [];
    if (n.is_current === false) {
      if (n.type === "UserStory") classes.push("archived-story");
      else classes.push("archived-version");
    }
    if (n.orphan_removed) {
      classes.push("version-removed", "version-inactive", "orphan-removed");
    }
    elements.push({
      group: "nodes",
      classes: classes.join(" ") || undefined,
      data: {
        id: n.id,
        label: n.label,
        caption: `${n.label}\nv${n.version}${n.is_current === false ? "\n(archived)" : ""}`,
        color: TYPE_COLORS[n.type] || "#888",
        ...n,
      },
      position: pos,
    });
  });

  graph.edges.forEach((e) => {
    elements.push({
      group: "edges",
      data: {
        id: e.id,
        source: e.source,
        target: e.target,
        rel_type: e.rel_type,
      },
    });
  });

  cy.elements().remove();
  cy.add(elements);

  const layoutNeeded = elements.filter((e) => e.group === "nodes" && !e.position).length;
  if (layoutNeeded > 0) {
    runGraphLayout(currentLayoutMode, { reset: false });
  } else if (currentLayoutMode === "layered") {
    cy.nodes().forEach(snapNodeToLayer);
  } else {
    resizeGraphViewport();
  }

  const focusNodeId = graph.focus_story_node_id || $("storySelect").value;
  if (graph.story_flow_delta?.has_changes) {
    lastStoryFlowDelta = graph.story_flow_delta;
  } else if (graph.story_flow_delta) {
    lastStoryFlowDelta = null;
  } else {
    lastStoryFlowDelta = null;
  }
  applyStoryVersionDelta(lastStoryFlowDelta);
  if (focusNodeId) focusStoryView(focusNodeId);

  if (newNodeId) {
    highlightNodeId = newNodeId;
    const el = cy.getElementById(newNodeId);
    if (el.length) {
      el.addClass("new-highlight");
      cy.animate({ center: { eles: el }, zoom: 1.2 }, { duration: 400 });
      setTimeout(() => el.removeClass("new-highlight"), 2500);
      const n = graph.nodes.find((x) => x.id === newNodeId);
      if (n) selectNode({ ...n, id: n.id });
    }
  }
}

async function refreshGraph(newNodeId = null, { force = false, loadingLabel = "Loading graph…" } = {}) {
  const reqSeq = ++graphRequestSeq;
  graphLoadingActiveSeq = reqSeq;
  setGraphLoading(true, loadingLabel);
  try {
    const graph = await fetchGraphCached({ force });
    if (reqSeq !== graphRequestSeq) return;
    applyGraph(graph, newNodeId);
  } finally {
    if (reqSeq === graphLoadingActiveSeq) {
      setGraphLoading(false);
    }
  }
}

/** Reload story list + fetch latest graph from Neo4j (use after any mutation). */
async function reloadDashboard(highlightNodeId = null, storyNodeId = null) {
  invalidateGraphCache();
  await loadStories();
  if (storyNodeId) {
    const sel = $("storySelect");
    if ([...sel.options].some((o) => o.value === storyNodeId)) {
      sel.value = storyNodeId;
      sessionStorage.setItem("kg_story_focus", storyNodeId);
    }
  }
  await loadNodeInventory();
  await updatePersistenceStatus();
  await refreshGraph(highlightNodeId, { force: true, loadingLabel: "Reloading graph…" });
}

async function loadNodeInventory() {
  try {
    inventoryData = await api("/api/graph/nodes");
    renderNodeInventory();
  } catch (e) {
    $("nodeInventory").innerHTML = `<p class="hint" style="color:var(--danger)">${escapeHtml(e.message)}</p>`;
  }
}

function renderNodeInventory() {
  const container = $("nodeInventory");
  const filter = $("nodeFilter").value;
  $("nodeCount").textContent = String(inventoryData.total || 0);

  let nodes = inventoryData.nodes || [];
  if (filter) nodes = nodes.filter((n) => n.type === filter);

  if (!nodes.length) {
    container.innerHTML = `<p class="hint">${inventoryData.total ? "No nodes match this filter." : "No nodes in Neo4j yet. Upload a file above."}</p>`;
    return;
  }

  container.innerHTML = nodes
    .map((n) => {
      const sel = selectedNode?.id === n.id ? " selected" : "";
      const live = n.is_current !== false;
      const restoreBtn = live
        ? `<span class="live-badge" title="This is the live version">live</span>`
        : `<button type="button" class="btn-make-live" title="Make this version live (restore this flow)">↩</button>`;
      return `
      <div class="node-row${sel}${live ? "" : " archived-row"}" data-base-id="${escapeHtml(n.base_id)}" data-entity-type="${escapeHtml(n.entity_type)}" data-node-id="${escapeHtml(n.id)}" data-is-current="${live}">
        <div class="node-row-main">
          <div class="node-row-type">${escapeHtml(n.type)}</div>
          <div class="node-row-label">${escapeHtml(n.label)}</div>
          <div class="node-row-meta">${escapeHtml(n.base_id)} · v${n.version}${live ? "" : " · archived"}</div>
        </div>
        <div class="node-row-actions">
          <button type="button" class="btn-focus" title="Show on graph">◎</button>
          ${restoreBtn}
          <button type="button" class="btn-del-node" title="Delete this version only (other versions kept)">×</button>
        </div>
      </div>`;
    })
    .join("");

  container.querySelectorAll(".node-row").forEach((row) => {
    row.addEventListener("click", (e) => {
      if (e.target.closest(".btn-del-node, .btn-make-live, .btn-focus")) return;
      focusInventoryNode(row.dataset.nodeId);
    });
  });
  container.querySelectorAll(".btn-focus").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      focusInventoryNode(btn.closest(".node-row").dataset.nodeId);
    });
  });
  container.querySelectorAll(".btn-del-node").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const row = btn.closest(".node-row");
      deleteNodeVersion(
        row.dataset.nodeId,
        row.dataset.entityType,
        row.dataset.baseId,
        row.dataset.isCurrent === "true",
      );
    });
  });
  container.querySelectorAll(".btn-make-live").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const row = btn.closest(".node-row");
      makeVersionLive(row.dataset.nodeId, row.dataset.entityType, row.dataset.baseId);
    });
  });
}

async function makeVersionLive(nodeId, entityType, baseId) {
  const label = baseId || nodeId;
  if (
    !confirm(
      `Make this version live?\n\n${label} will become the active version. The current live version will be archived (not deleted).`,
    )
  ) {
    return;
  }
  try {
    const res = await api(`/api/graph/versions/${encodeURIComponent(nodeId)}/make-current`, {
      method: "POST",
    });
    showToast(res.message || "Version is now live");
    if (entityType === "user_story") {
      sessionStorage.setItem("kg_story_focus", nodeId);
    }
    await reloadDashboard(nodeId, entityType === "user_story" ? nodeId : null);
  } catch (err) {
    showToast(err.message, "error");
  }
}

function focusInventoryNode(nodeId) {
  const n = (inventoryData.nodes || []).find((x) => x.id === nodeId);
  if (!n) return;
  const graphNode = lastGraph.nodes.find((x) => x.id === nodeId);
  if (graphNode) {
    selectNode({ ...graphNode, id: nodeId });
    if (cy) {
      const el = cy.getElementById(nodeId);
      if (el.length) cy.animate({ center: { eles: el }, zoom: 1.3 }, { duration: 300 });
    }
  }
}

async function deleteNodeVersion(nodeId, entityType, baseId, isLive) {
  const msg = isLive
    ? `Delete only this live version of "${baseId}"?\n\nOlder archived versions will stay in Neo4j — you can restore them with ↩. If this is the only version, it will be removed entirely.`
    : `Delete this archived version of "${baseId}"?\n\nOther versions (including the live one) will not be removed.`;
  if (!confirm(msg)) return;
  try {
    const res = await api(`/api/graph/versions/${encodeURIComponent(nodeId)}`, { method: "DELETE" });
    if (selectedNode?.id === nodeId) {
      selectedNode = null;
      updatePanel();
    }
    showToast(res.message || `Deleted version`);
    const focusId = res.promoted_to_live || null;
    await reloadDashboard(focusId, entityType === "user_story" ? focusId : null);
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function clearKnowledgeGraph() {
  if (!confirm("Delete the ENTIRE knowledge graph? All nodes and relationships will be removed.")) return;
  if (!confirm("This cannot be undone. Type OK in the next dialog is not required — confirm again.")) return;
  try {
    await api("/api/graph?confirm=yes", { method: "DELETE" });
    selectedNode = null;
    savedPositions.clear();
    updatePanel();
    showToast("Knowledge graph cleared");
    await reloadDashboard();
  } catch (err) {
    showToast(err.message, "error");
  }
}

function storyIdFromUpload(res) {
  if (res.entity_type === "user_story" && res.base_id) return res.base_id;
  if (res.preview?.story_id) return res.preview.story_id;
  if (res.graph?.story_id) return res.graph.story_id;
  return null;
}

function selectNode(data) {
  selectedNode = data;
  updatePanel();
  updateAddFormDefaults();
  renderNodeInventory();
  if (cy) {
    cy.$("node:selected").unselect();
    const el = cy.getElementById(data.id);
    if (el.length) el.select();
  }
}

function updatePanel() {
  const details = $("nodeDetails");
  updateUploadHint();
  if (!selectedNode) {
    details.classList.add("hidden");
    $("panelTitle").textContent = "Selected node";
    $("panelHint").textContent =
      "Click a node to inspect. ↩ makes an archived version live; × deletes that version only (others kept).";
    return;
  }

  $("panelTitle").textContent = selectedNode.label;
  $("panelHint").textContent = `${selectedNode.type} · ${selectedNode.base_id}`;
  details.classList.remove("hidden");
  const props = selectedNode.properties || {};
  const isLive = selectedNode.is_current !== false;
  const rows = [
    ["ID", selectedNode.base_id],
    ["node_id", selectedNode.id],
    ["Version", `v${selectedNode.version}${isLive ? " (live)" : " (archived)"}`],
    ...Object.entries(props)
      .filter(([k]) => !["base_id", "version", "status"].includes(k))
      .slice(0, 8)
      .map(([k, v]) => [k, Array.isArray(v) ? v.join(", ") : String(v)]),
  ];

  const restoreBtn = isLive
    ? ""
    : `<button type="button" class="btn sm primary" id="btnMakeLive">Make this version live</button>`;

  details.innerHTML =
    rows.map(([l, v]) => `<div class="row"><span class="label">${l}</span><div class="val">${escapeHtml(v)}</div></div>`).join("") +
    `<div class="actions">
      <button type="button" class="btn sm ghost" id="btnEdit"${isLive ? "" : " disabled title='Edit applies to the live version only'"}>Edit</button>
      ${restoreBtn}
    </div>`;

  $("btnEdit")?.addEventListener("click", openEditModal);
  $("btnMakeLive")?.addEventListener("click", () => {
    const et =
      selectedNode.entity_type ||
      { UserStory: "user_story", Feature: "feature", APIEndpoint: "api_endpoint", TestCase: "test_case" }[
        selectedNode.type
      ];
    makeVersionLive(selectedNode.id, et, selectedNode.base_id);
  });
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

function updateAddFormDefaults() {
  const type = $("addType").value;
  const hint = $("addHint");
  if (!selectedNode) {
    hint.textContent = "New nodes appear next to the selected parent when you select one first.";
    return;
  }
  const pType = selectedNode.type;
  const hints = {
    user_story: "",
    feature: pType === "UserStory" ? `Feature can link to story ${selectedNode.base_id} via flows[]` : "",
    test_case: pType === "Feature" ? `Set linked_to: ${selectedNode.base_id}` : "",
    endpoint: "",
  };
  hint.textContent = hints[type] || `Placed beside ${selectedNode.label}`;
  renderAddFields();
}

const ADD_FIELD_TEMPLATES = {
  user_story: `
    <label>Title <input name="title" required placeholder="Plan Change" /></label>
    <label>Story ID <span class="hint">(optional — auto-assigned)</span> <input name="story_id" placeholder="US1" /></label>
    <label>Content <textarea name="content"></textarea></label>
    <label>Flows (comma-separated, optional — auto-derived if empty) <input name="flows" placeholder="Login, PlanFetch, Payment" /></label>`,
  feature: `
    <label>Name <input name="name" required placeholder="Login" /></label>
    <label>Feature ID <span class="hint">(optional)</span> <input name="feature_id" placeholder="Login" /></label>
    <label>Description <textarea name="description"></textarea></label>
    <label>API paths (comma-separated) <input name="apis_used" placeholder="/auth/login" /></label>`,
  endpoint: `
    <label>Method <select name="method"><option>GET</option><option>POST</option><option>PUT</option><option>DELETE</option><option>PATCH</option></select></label>
    <label>Path <input name="path" required placeholder="/auth/login" /></label>
    <label>Summary <input name="summary" /></label>`,
  test_case: `
    <label>Title <input name="title" required /></label>
    <label>Linked to (feature name, story title, or METHOD:path) <input name="linked_to" required placeholder="Login" /></label>
    <label>Test case ID <span class="hint">(optional)</span> <input name="tc_id" placeholder="TC-login-001" /></label>
    <label>Title <input name="title" required /></label>
    <label>Type <select name="type"><option value="positive">positive</option><option value="negative">negative</option></select></label>
    <label>Expected result <input name="expected_result" /></label>
    <label>Steps (one per line) <textarea name="steps"></textarea></label>`,
};

function renderAddFields() {
  const type = $("addType").value;
  $("addFields").innerHTML = ADD_FIELD_TEMPLATES[type] || "";

  if (!selectedNode) return;
  const form = $("addForm");
  if (type === "test_case" && selectedNode.type === "Feature") {
    form.linked_to.value = selectedNode.base_id;
  }
  if (type === "feature" && selectedNode.type === "UserStory") {
    form.feature_id.placeholder = "NewFeature";
  }
}

function collectFormData(form, type) {
  const fd = new FormData(form);
  const body = Object.fromEntries(fd.entries());
  if (body.apis_used) body.apis_used = body.apis_used.split(",").map((s) => s.trim()).filter(Boolean);
  if (body.flows) body.flows = body.flows.split(",").map((s) => s.trim()).filter(Boolean);
  if (body.depends_on) body.depends_on = body.depends_on.split(",").map((s) => s.trim()).filter(Boolean);
  if (body.steps) body.steps = body.steps.split("\n").map((s) => s.trim()).filter(Boolean);
  return body;
}

$("addType").addEventListener("change", () => {
  renderAddFields();
  updateAddFormDefaults();
});

$("addForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const type = $("addType").value;
  const body = collectFormData(e.target, type);
  let path = ADD_ENDPOINTS[type];
  const params = new URLSearchParams();
  const baseId = selectedStoryBaseId();
  if (baseId) params.set("story_id", baseId);
  if (params.toString()) path += `?${params}`;

  try {
    const res = await api(path, { method: "POST", body: JSON.stringify(body) });
    const storyNodeId = res.node_id || selectedStoryNodeId() || null;
    await reloadDashboard(res.node_id, storyNodeId);
    const edgeMsg = res.edges_created?.length ? ` · ${res.edges_created.length} edge(s) linked` : "";
    showToast(`${res.message === "created" ? "Added" : "Updated"} ${res.base_id}${edgeMsg}`);
  } catch (err) {
    showToast(err.message, "error");
  }
});

$("btnClearKG").addEventListener("click", clearKnowledgeGraph);
$("nodeFilter").addEventListener("change", renderNodeInventory);

function openEditModal() {
  if (!selectedNode) return;
  const p = selectedNode.properties || {};
  const type = selectedNode.entity_type || DELETE_TYPES[selectedNode.type];
  let fields = "";

  if (type === "user_story") {
    fields = `
      <input type="hidden" name="story_id" value="${selectedNode.base_id}" />
      <label>Title <input name="title" value="${escapeAttr(p.title || selectedNode.label)}" required /></label>
      <label>Content <textarea name="content">${escapeAttr(p.content || "")}</textarea></label>`;
  } else if (type === "feature") {
    fields = `
      <input type="hidden" name="feature_id" value="${selectedNode.base_id}" />
      <label>Name <input name="name" value="${escapeAttr(p.name || "")}" required /></label>
      <label>Description <textarea name="description">${escapeAttr(p.description || "")}</textarea></label>
      <label>API paths <input name="apis_used" value="${escapeAttr((p.apis_used || []).join(", "))}" /></label>`;
  } else if (type === "api_endpoint") {
    fields = `
      <label>Method <input name="method" value="${escapeAttr(p.method || "GET")}" /></label>
      <label>Path <input name="path" value="${escapeAttr(p.path || "")}" required /></label>
      <label>Summary <input name="summary" value="${escapeAttr(p.summary || "")}" /></label>`;
  } else if (type === "test_case") {
    fields = `
      <input type="hidden" name="tc_id" value="${selectedNode.base_id}" />
      <label>Linked to <input name="linked_to" value="${escapeAttr(p.linked_to || "")}" required /></label>
      <label>Title <input name="title" value="${escapeAttr(p.title || selectedNode.label)}" required /></label>
      <label>Type <input name="type" value="${escapeAttr(p.type || "positive")}" /></label>
      <label>Expected result <input name="expected_result" value="${escapeAttr(p.expected_result || "")}" /></label>`;
  }

  $("editForm").innerHTML = fields;
  $("editModal").classList.remove("hidden");
}

function escapeAttr(s) {
  return String(s).replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

$("btnCancelEdit").addEventListener("click", () => $("editModal").classList.add("hidden"));

$("editForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!selectedNode) return;
  const type = selectedNode.entity_type || DELETE_TYPES[selectedNode.type];
  const endpoint = ADD_ENDPOINTS[type === "api_endpoint" ? "endpoint" : type];
  if (!endpoint) return;

  const body = collectFormData(e.target, type);
  try {
    const res = await api(endpoint + storyParam(), { method: "POST", body: JSON.stringify(body) });
    $("editModal").classList.add("hidden");
    await reloadDashboard(res.node_id, selectedStoryNodeId() || null);
    showToast(`Updated to v${res.version} in Neo4j`);
  } catch (err) {
    showToast(err.message, "error");
  }
});

$("storySelect").addEventListener("change", async () => {
  const sid = $("storySelect").value;
  if (sid) sessionStorage.setItem("kg_story_focus", sid);
  else sessionStorage.removeItem("kg_story_focus");
  clearStoryFocus();
  try {
    await refreshGraph(null, { loadingLabel: "Switching story…" });
  } catch (e) {
    showToast("Failed to switch story: " + e.message, "error");
  }
});
$("btnRefresh").addEventListener("click", async () => {
  try {
    await api("/api/graph/relink", { method: "POST" });
    showToast("Re-linked all nodes in Neo4j");
  } catch (e) {
    showToast(e.message, "error");
  }
  invalidateGraphCache();
  await reloadDashboard();
});
async function boot() {
  renderAddFields();
  initUpload();
  initLayoutControls();
  initGraphViewport();
  updateUploadHint();
  initCytoscape();
  await checkHealth();
  try {
    await loadStories();
    await loadNodeInventory();
    await refreshGraph();
    const sid = selectedStoryNodeId();
    if (sid) focusStoryView(sid);
  } catch (e) {
    showToast("Load graph failed: " + e.message, "error");
  }
}

boot();
