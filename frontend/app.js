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
const TYPE_LAYER_ORDER = [
  "UserStory",
  "Feature",
  "APIEndpoint",
  "APIResponseSchema",
  "TestCase",
];

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
let lastGraph = { nodes: [], edges: [] };
const savedPositions = new Map();
let highlightNodeId = null;
let pendingFile = null;
let pendingVersionMode = null;
let currentLayoutMode = "layered";
let layoutRunning = false;
let inventoryData = { nodes: [], total: 0, by_type: {} };

const UPLOAD_TYPE_LABELS = {
  user_story: "User Story",
  feature: "Feature",
  test_case: "Test Case",
  api_spec: "API Spec",
  api_endpoint: "API Endpoint",
};

const $ = (id) => document.getElementById(id);

function storyParam() {
  const sid = $("storySelect").value;
  return sid ? `?story_id=${encodeURIComponent(sid)}` : "";
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

function uploadQueryParams() {
  const params = new URLSearchParams();
  const sid = $("storySelect").value;
  if (sid) params.set("story_id", sid);
  const forced = $("uploadType").value;
  if (forced && forced !== "auto") params.set("entity_type", forced);
  if (selectedNode) {
    params.set("parent_type", selectedNode.type);
    params.set("parent_base_id", selectedNode.base_id);
  }
  if (pendingVersionMode) params.set("version_mode", pendingVersionMode);
  const q = params.toString();
  return q ? `?${q}` : "";
}

function updateUploadHint() {
  const hint = $("uploadHint");
  if (!selectedNode) {
    hint.textContent =
      "Drop JSON/YAML — user story, feature, testcase, or OpenAPI spec (no flow files).";
    return;
  }
  const tips = {
    UserStory: `Upload features/testcases — story flows: ${(selectedNode.properties?.flows || []).join(" → ")}`,
    Feature: "Upload testcase with linked_to this feature id or name.",
  };
  hint.textContent = tips[selectedNode.type] || `New nodes will appear beside ${selectedNode.label}.`;
}

function askVersionMode(preview) {
  return new Promise((resolve) => {
    const modal = $("versionModeModal");
    const text = $("versionModeText");
    const id = preview?.version_target?.base_id || preview?.preview?.assigned_id || "this entity";
    if (text) {
      text.textContent = `A previous version exists for ${id}. Deprecate keeps history and links old→new. Delete permanently removes old versions and stores this upload as v1.`;
    }
    const finish = (mode) => {
      modal?.classList.add("hidden");
      resolve(mode);
    };
    $("btnVersionModeDeprecate").onclick = () => finish("deprecate");
    $("btnVersionModeDelete").onclick = () => finish("delete");
    $("btnVersionModeCancel").onclick = () => finish(null);
    modal?.classList.remove("hidden");
  });
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
  pendingVersionMode = null;
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
    if (preview.needs_version_decision) {
      const mode = await askVersionMode(preview);
      if (!mode) {
        $("btnUpload").disabled = true;
        return;
      }
      pendingVersionMode = mode;
      const note = mode === "delete" ? "Delete old permanently" : "Deprecate old version";
      $("uploadPreview").innerHTML += `<div class="hint" style="margin-top:.5rem">Version policy: ${escapeHtml(note)}</div>`;
    } else {
      pendingVersionMode = null;
    }
    hideDuplicateModal();
    $("btnUpload").disabled = false;
  } catch (err) {
    $("uploadPreview").innerHTML = `<span class="hint" style="color:var(--danger)">${escapeHtml(err.message)}</span>`;
    $("btnUpload").disabled = true;
  }
}

async function uploadPendingFile() {
  if (!pendingFile) return;
  const fd = new FormData();
  fd.append("file", pendingFile);
  $("btnUpload").disabled = true;
  $("btnUpload").textContent = "Uploading…";

  try {
    const res = await apiForm(`/api/upload${uploadQueryParams()}`, fd);
    const newId = res.node_id;
    const storyBaseId = storyIdFromUpload(res) || $("storySelect").value || null;
    await reloadDashboard(newId, storyBaseId);
    const edgeMsg = res.edges_created?.length ? ` · ${res.edges_created.length} edge(s)` : "";
    const countMsg = res.count > 1 ? `${res.count} items` : res.base_id;
    const idMeta = res.identity?.[0];
    const deltaHint = idMeta?.delta_summary ? ` · ${idMeta.delta_summary}` : "";
    const verHint = idMeta?.is_version_update ? " (new version)" : "";
    showToast(`Uploaded ${countMsg}${verHint} from file${edgeMsg}${deltaHint}`);
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
    opt.value = s.base_id;
    opt.textContent = `${s.base_id} — ${s.title}`;
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

/** Always load every node from Neo4j — story filter only focuses the view, never hides data. */
async function fetchGraph() {
  return api("/api/graph");
}

/** Edge types that belong to a story's product graph (not cross-story/version links). */
const STORY_FOCUS_REL_TYPES = new Set([
  "HAS_FEATURE",
  "USES_API",
  "HAS_TEST_CASE",
  "NEXT_STEP",
  "HAS_RESPONSE_SCHEMA",
  "VALIDATES_AGAINST",
]);

/**
 * Nodes/edges for one story: bounded BFS on product relationships only.
 * Does not walk DEPENDS_ON / BLOCKS / PREVIOUS_VERSION (those pull in other stories).
 */
function storyFocusCollection(storyNodes) {
  const storyBaseIds = new Set(
    storyNodes
      .filter((n) => n.data("type") === "UserStory")
      .map((n) => n.data("base_id"))
      .filter(Boolean),
  );

  const allowNode = (n) => {
    if (n.data("type") !== "UserStory") return true;
    return storyBaseIds.has(n.data("base_id"));
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

function focusStoryView(storyBaseId) {
  if (!cy || !storyBaseId) {
    if (cy && cy.nodes().length) cy.fit(cy.elements(), 48);
    return;
  }
  const story = cy.nodes().filter(
    (n) => n.data("type") === "UserStory" && n.data("base_id") === storyBaseId,
  );
  if (!story.length) {
    cy.fit(cy.elements(), 48);
    return;
  }
  const hood = storyFocusCollection(story);
  const focusNodes = hood.nodes();
  const focusEdges = hood.edges();
  cy.elements().removeClass("dimmed");
  cy.nodes().not(focusNodes).addClass("dimmed");
  cy.edges().not(focusEdges).addClass("dimmed");
  if (focusNodes.length) cy.fit(hood.nonempty() ? hood : story, 56);
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
    const sid = $("storySelect").value;
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
    elements.push({
      group: "nodes",
      data: {
        id: n.id,
        label: n.label,
        caption: `${n.label}\nv${n.version}`,
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
    const sid = $("storySelect").value;
    if (sid) focusStoryView(sid);
    else if (cy.nodes().length) cy.fit(cy.elements(), 48);
  }

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

async function refreshGraph(newNodeId = null) {
  const graph = await fetchGraph();
  applyGraph(graph, newNodeId);
}

/** Reload story list + fetch latest graph from Neo4j (use after any mutation). */
async function reloadDashboard(highlightNodeId = null, storyBaseId = null) {
  await loadStories();
  if (storyBaseId) {
    const sel = $("storySelect");
    if ([...sel.options].some((o) => o.value === storyBaseId)) {
      sel.value = storyBaseId;
      sessionStorage.setItem("kg_story_focus", storyBaseId);
    }
  }
  await loadNodeInventory();
  await updatePersistenceStatus();
  await refreshGraph(highlightNodeId);
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
      const sel = selectedNode?.base_id === n.base_id && selectedNode?.type === n.type ? " selected" : "";
      return `
      <div class="node-row${sel}" data-base-id="${escapeHtml(n.base_id)}" data-entity-type="${escapeHtml(n.entity_type)}" data-node-id="${escapeHtml(n.id)}">
        <div class="node-row-main">
          <div class="node-row-type">${escapeHtml(n.type)}</div>
          <div class="node-row-label">${escapeHtml(n.label)}</div>
          <div class="node-row-meta">${escapeHtml(n.base_id)} · v${n.version}</div>
        </div>
        <div class="node-row-actions">
          <button type="button" class="btn-focus" title="Show on graph">◎</button>
          <button type="button" class="btn-del-node" title="Delete node">×</button>
        </div>
      </div>`;
    })
    .join("");

  container.querySelectorAll(".node-row").forEach((row) => {
    row.addEventListener("click", (e) => {
      if (e.target.closest(".btn-del-node")) return;
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
      deleteNodeById(row.dataset.entityType, row.dataset.baseId);
    });
  });
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

async function deleteNodeById(entityType, baseId) {
  if (!confirm(`Delete "${baseId}" and all its relationships from Neo4j?`)) return;
  try {
    await api(`/api/nodes/${entityType}/${encodeURIComponent(baseId)}${storyParam()}`, { method: "DELETE" });
    if (selectedNode?.base_id === baseId) {
      selectedNode = null;
      updatePanel();
    }
    showToast(`Deleted ${baseId}`);
    await reloadDashboard();
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
    $("panelHint").textContent = "Click a node to inspect or edit. Delete via × in Uploaded nodes.";
    return;
  }

  $("panelTitle").textContent = selectedNode.label;
  $("panelHint").textContent = `${selectedNode.type} · ${selectedNode.base_id}`;
  details.classList.remove("hidden");
  const props = selectedNode.properties || {};
  const rows = [
    ["ID", selectedNode.base_id],
    ["node_id", selectedNode.id],
    ["Version", `v${selectedNode.version}`],
    ...Object.entries(props)
      .filter(([k]) => !["base_id", "version", "status"].includes(k))
      .slice(0, 8)
      .map(([k, v]) => [k, Array.isArray(v) ? v.join(", ") : String(v)]),
  ];

  details.innerHTML =
    rows.map(([l, v]) => `<div class="row"><span class="label">${l}</span><div class="val">${escapeHtml(v)}</div></div>`).join("") +
    `<div class="actions">
      <button type="button" class="btn sm ghost" id="btnEdit">Edit</button>
    </div>`;

  $("btnEdit")?.addEventListener("click", openEditModal);
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
  const sid = $("storySelect").value;
  if (sid) params.set("story_id", sid);
  if (params.toString()) path += `?${params}`;

  try {
    const res = await api(path, { method: "POST", body: JSON.stringify(body) });
    const storyBaseId = body.story_id || $("storySelect").value || null;
    await reloadDashboard(res.node_id, storyBaseId);
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
    await reloadDashboard(res.node_id, $("storySelect").value || null);
    showToast(`Updated to v${res.version} in Neo4j`);
  } catch (err) {
    showToast(err.message, "error");
  }
});

$("storySelect").addEventListener("change", () => {
  const sid = $("storySelect").value;
  if (sid) sessionStorage.setItem("kg_story_focus", sid);
  else sessionStorage.removeItem("kg_story_focus");
  clearStoryFocus();
  if (sid) focusStoryView(sid);
  else if (cy && cy.nodes().length) cy.fit(cy.elements(), 48);
});
$("btnRefresh").addEventListener("click", async () => {
  try {
    await api("/api/graph/relink", { method: "POST" });
    showToast("Re-linked all nodes in Neo4j");
  } catch (e) {
    showToast(e.message, "error");
  }
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
    const sid = $("storySelect").value;
    if (sid) focusStoryView(sid);
  } catch (e) {
    showToast("Load graph failed: " + e.message, "error");
  }
}

boot();
