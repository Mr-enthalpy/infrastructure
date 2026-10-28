(() => {
  "use strict";

  const tokenMeta = document.querySelector('meta[name="citespec-token"]');
  const token = tokenMeta ? tokenMeta.content : "";
  const state = {
    config: null,
    overview: null,
    my: null,
    queue: [],
    jobs: [],
    jobsRefreshedAt: 0,
    adminEnabled: false,
    projects: [],
    versions: [],
    envProject: "",
    legacySources: [],
    legacyClaims: [],
    adminLegacySources: [],
    adminLegacyClaims: [],
    offline: false,
    selectedGpus: new Set(),
    terminal: null,
    poll: null,
    clock: null,
  };

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));

  function esc(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function ownerLabel(item) {
    const display = String(item?.owner_display || item?.owner || "");
    const nickname = String(item?.owner_nickname || "");
    if (nickname && nickname !== display) {
      return display ? `${display} (${nickname})` : nickname;
    }
    return display;
  }

  function ownerContact(item) {
    return String(item?.owner_public_contact || "");
  }

  function formatRemaining(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    const total = Math.max(0, Math.floor(Number(seconds)));
    if (total <= 0) return "expired";
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    if (hours > 0) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
    const secs = total % 60;
    return `${minutes}m ${String(secs).padStart(2, "0")}s`;
  }

  function flash(message, kind = "ok", duration = 5000) {
    const banner = $("#flash");
    banner.textContent = message;
    banner.className = `banner ${kind}`;
    banner.hidden = false;
    window.clearTimeout(flash.timer);
    flash.timer = window.setTimeout(() => {
      banner.hidden = true;
    }, duration);
  }


  async function api(path, options = {}) {
    const init = { ...options, headers: { "X-CITESPEC-Token": token, ...(options.headers || {}) } };
    if (init.body && typeof init.body !== "string") {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(init.body);
    }
    const response = await fetch(path, init);
    let payload;
    try {
      payload = await response.json();
    } catch {
      throw new Error(`HTTP ${response.status}: invalid response`);
    }
    if (response.status === 403) {
      throw new Error(payload?.error?.message || "forbidden");
    }
    return payload;
  }

  function setConnection(online) {
    state.offline = !online;
    const pill = $("#connection-pill");
    pill.textContent = online ? "online" : "offline";
    pill.className = `pill ${online ? "online" : "offline"}`;
    $("#offline-banner").hidden = online;
  }

  function setMode(mode) {
    const pill = $("#mode-pill");
    pill.textContent = `mode ${mode || "?"}`;
    pill.className = `pill ${mode === "active" ? "active" : mode === "maintenance" ? "maintenance" : mode === "monitor" ? "monitor" : "unknown"}`;
  }

  function activateTab(name) {
    $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
    $$(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.id === `tab-${name}`));
    if (name === "allocations") refreshMy();
    if (name === "environments") refreshEnvironments();
    if (name === "admin" && state.adminEnabled) {
      refreshAdmin();
      refreshLegacyAdmin();
    }
  }

  async function loadConfig() {
    const payload = await api("/api/config");
    if (!payload.ok) throw new Error(payload?.error?.message || "config failed");
    state.config = payload.data;
    $("#server-name").textContent = payload.data.server_name;
    const compatBanner = $("#compat-banner");
    const compatWarning = String(payload.data.compatibility_warning || "");
    compatBanner.textContent = compatWarning;
    compatBanner.hidden = !compatWarning;
    if (payload.data.admin_enabled) {
      try {
        const admin = await api("/api/admin/users");
        if (admin.ok) {
          state.adminEnabled = true;
          void document.getElementById("membership-panel")?.removeAttribute("hidden");
          $("#admin-tab").hidden = false;
          renderAdminUsers(admin.data.users || []);
        }
      } catch {
        state.adminEnabled = false;
      }
    }
  }

  async function refresh() {
    try {
      const payload = await api("/api/overview");
      if (!payload.ok) throw new Error(payload?.error?.message || "list failed");
      state.overview = payload.data;
      setConnection(true);
      setMode(payload.data.mode);
      renderOverview();
      await refreshMy();
      if (state.adminEnabled && $("#tab-admin").classList.contains("active")) await refreshAdmin();
    } catch (error) {
      setConnection(false);
      flash(String(error.message || error), "error");
      if (state.overview) renderOverview();
    }
  }

  async function refreshMy() {
    try {
      const [statusPayload, queuePayload] = await Promise.all([
        api("/api/status"),
        api("/api/queue"),
      ]);
      if (!statusPayload.ok) throw new Error(statusPayload?.error?.message || "status failed");
      state.my = statusPayload.data;
      state.queue = queuePayload.ok ? (queuePayload.data.queue || []) : [];
      renderMyAllocations();
      renderMyQueue();
      renderJobStartChoices();
      // Job inspection costs one SSH/Docker round trip per active allocation.
      // Keep it off the 5-second status poll.
      if (!state.jobsRefreshedAt || Date.now() - state.jobsRefreshedAt >= 30000) {
        await refreshJobs();
      }
    } catch (error) {
      $("#my-allocations").className = "allocation-list empty";
      $("#my-allocations").textContent = `Unable to load: ${error.message || error}`;
    }
  }

  async function refreshJobs() {
    const allocations = (state.my?.allocations || []).filter((item) => item.status === "active");
    const target = $("#my-jobs");
    if (!allocations.length) {
      state.jobs = [];
      target.className = "allocation-list empty";
      target.textContent = "No active allocations.";
      return;
    }
    const results = await Promise.all(
      allocations.map(async (allocation) => {
        try {
          const payload = await api(`/api/jobs/${encodeURIComponent(allocation.id)}`);
          if (!payload.ok) return [];
          return (payload.data.jobs || []).map((job) => ({ ...job, allocation_id: allocation.id }));
        } catch {
          return [];
        }
      }),
    );
    state.jobs = results.flat();
    state.jobsRefreshedAt = Date.now();
    renderJobs();
  }

  function renderMyQueue() {
    const target = $("#my-queue");
    const requests = state.queue || [];
    if (!requests.length) {
      target.className = "allocation-list empty";
      target.textContent = "No queued requests.";
      return;
    }
    target.className = "allocation-list";
    target.innerHTML = requests.map((request) => `
      <div class="allocation-item">
        <div>
          <div class="id">${esc(request.queue_id)}</div>
          <div class="muted">position ${esc(request.position)} · ${statusBadge(request.status || "queued")}</div>
        </div>
        <div class="muted">GPU: <b>${esc((request.gpus || []).join(",") || "CPU-only")}</b><br>CPU/RAM: ${esc(request.cpus)} / ${esc(request.memory_gb)} GiB</div>
        <div class="muted">remaining ${formatRemaining(request.remaining_seconds)}</div>
        <div class="row-actions">
          <button data-action="queue-cancel" data-id="${esc(request.queue_id)}">cancel</button>
        </div>
      </div>
    `).join("");
    target.querySelectorAll('button[data-action="queue-cancel"]').forEach((button) => {
      button.addEventListener("click", () => cancelQueue(button.dataset.id));
    });
  }

  function renderJobStartChoices() {
    const select = $("#job-start-allocation");
    if (!select) return;
    const allocations = (state.my?.allocations || []).filter(
      (item) => item.status === "active",
    );
    const current = select.value;
    select.innerHTML = allocations.map((allocation) => `
      <option value="${esc(allocation.id)}">${esc(allocation.id.slice(0, 12))} · GPU ${esc((allocation.gpus || []).join(",") || "cpu")}</option>
    `).join("");
    if (allocations.some((allocation) => allocation.id === current)) {
      select.value = current;
    }
  }

  function renderJobs() {
    const target = $("#my-jobs");
    const jobs = state.jobs || [];
    if (!jobs.length) {
      target.className = "allocation-list empty";
      target.textContent = "No jobs.";
      return;
    }
    target.className = "allocation-list";
    target.innerHTML = jobs.map((job) => `
      <div class="allocation-item">
        <div><div class="id">${esc(job.name)}</div><div class="muted">${esc(job.allocation_id)}</div></div>
        <div class="muted">${esc(job.session)}<br>${esc(job.log || "")}</div>
        <div class="muted">${esc(job.log_bytes ?? 0)} bytes</div>
        <div class="row-actions">
          <button data-action="job-attach" data-id="${esc(job.allocation_id)}" data-job="${esc(job.name)}">attach</button>
          <button data-action="job-logs" data-id="${esc(job.allocation_id)}" data-job="${esc(job.name)}">logs</button>
          <button data-action="job-stop" data-id="${esc(job.allocation_id)}" data-job="${esc(job.name)}">stop</button>
        </div>
      </div>
    `).join("");
    target.querySelectorAll("button[data-action]").forEach((button) => {
      button.addEventListener("click", () => {
        if (button.dataset.action === "job-attach") openTerminal(button.dataset.id, button.dataset.job);
        if (button.dataset.action === "job-logs") openJobLogs(button.dataset.id, button.dataset.job);
        if (button.dataset.action === "job-stop") stopJob(button.dataset.id, button.dataset.job);
      });
    });
  }

  function renderOverview() {
    if (!state.overview) return;
    const data = state.overview;
    const gpus = data.gpus || [];
    const free = gpus.filter((gpu) => gpu.state === "free").length;
    const allocated = gpus.filter((gpu) => gpu.state === "allocated").length;
    const unmanaged = gpus.filter((gpu) => gpu.state === "unmanaged").length;
    const unknown = gpus.filter((gpu) => gpu.state === "unknown").length;
    const processes = data.unmanaged?.processes?.length || 0;
    $("#summary").innerHTML = `
      <div class="stat"><strong>${gpus.length}</strong><span>GPUs</span></div>
      <div class="stat"><strong>${free}</strong><span>free</span></div>
      <div class="stat"><strong>${allocated}</strong><span>allocated</span></div>
      <div class="stat"><strong>${unmanaged}</strong><span>unmanaged</span></div>
      <div class="stat"><strong>${unknown}</strong><span>unknown</span></div>
      <div class="stat"><strong>${processes}</strong><span>unmanaged processes</span></div>
    `;
    renderGpuGrid(gpus);
    renderApplyChoices(gpus);
    renderAllAllocations(data.allocations || []);
    renderUnmanaged(data.unmanaged || {});
  }

  function renderGpuGrid(gpus) {
    const grid = $("#gpu-grid");
    if (!gpus.length) {
      grid.innerHTML = '<div class="allocation-list empty">No GPU inventory is available.</div>';
      return;
    }
    grid.innerHTML = gpus.map((gpu) => {
      const gpuState = String(gpu.state || "unknown");
      const isFree = gpuState === "free";
      const stale = state.offline ? "stale" : "";
      const clickable = isFree ? "clickable" : "";
      const deadline = gpu.remaining_seconds !== null && gpu.remaining_seconds !== undefined
        ? Date.now() + Number(gpu.remaining_seconds) * 1000
        : null;
      let detail = '<span>available for apply</span>';
      if (gpuState === "allocated") {
        detail = `<span>owner <b>${esc(ownerLabel(gpu) || "?")}</b></span>
                  <span>alloc <b>${esc((gpu.allocation_id || "").slice(0, 12))}</b></span>
                  ${ownerContact(gpu) ? `<span>contact ${esc(ownerContact(gpu))}</span>` : ""}`;
      } else if (!isFree) {
        detail = `<span>${esc(gpu.state_reason || gpuState)}</span>`;
      }
      return `
        <article class="gpu-card ${gpuState} ${stale} ${clickable}" data-gpu-index="${gpu.index}">
          <div class="gpu-head">
            <span class="gpu-index">GPU ${gpu.index}</span>
            <span class="gpu-state ${gpuState}">${esc(gpuState)}</span>
          </div>
          <div class="gpu-meta">
            <span>${esc(gpu.name || "")} · ${esc(gpu.memory_mib || "")} MiB</span>
            ${detail}
            ${gpu.compute_pids?.length ? `<span>compute PIDs: <b>${esc(gpu.compute_pids.join(","))}</b></span>` : ""}
          </div>
          ${deadline !== null
            ? `<div class="gpu-remaining" data-deadline="${deadline}">${formatRemaining(gpu.remaining_seconds)}</div>`
            : ""}
        </article>
      `;
    }).join("");
    grid.querySelectorAll(".gpu-card.clickable").forEach((card) => {
      card.addEventListener("click", () => {
        const index = Number(card.dataset.gpuIndex);
        if (!Number.isInteger(index)) return;
        state.selectedGpus.add(index);
        activateTab("apply");
        renderApplyChoices(state.overview?.gpus || []);
        const box = document.querySelector(`#gpu-choices input[value="${index}"]`);
        if (box) box.checked = true;
      });
    });
  }

  function renderApplyChoices(gpus) {
    const container = $("#gpu-choices");
    if (!gpus.length) {
      container.innerHTML = '<span class="muted">GPU 清单不可用。</span>';
      return;
    }
    // Busy GPUs are selectable on purpose: applying for one creates a queued
    // request (never a bypass). Only "unknown" state is unselectable because
    // the broker refuses to allocate GPUs it cannot verify.
    const freeCount = gpus.filter((gpu) => gpu.state === "free").length;
    const note = freeCount
      ? ""
      : '<span class="muted">所有 GPU 均被占用：选择 GPU 将创建排队请求（不会插队），留空则申请 CPU-only。</span>';
    container.innerHTML = gpus.map((gpu) => {
      const gpuState = String(gpu.state || "unknown");
      const owner = gpuState === "allocated" && gpu.owner_display
        ? ` · ${esc(gpu.owner_display)}`
        : "";
      const unselectable = gpuState === "unknown";
      return `
        <label class="gpu-choice">
          <input type="checkbox" name="gpu" value="${gpu.index}"
            ${unselectable ? "disabled" : ""}
            ${state.selectedGpus.has(gpu.index) ? "checked" : ""}>
          GPU ${gpu.index} <span class="muted">${esc(gpuState)}${owner}</span>
        </label>
      `;
    }).join("") + note;
    container.querySelectorAll("input[name=gpu]").forEach((input) => {
      input.addEventListener("change", () => {
        const index = Number(input.value);
        if (input.checked) state.selectedGpus.add(index);
        else state.selectedGpus.delete(index);
      });
    });
  }

  function statusBadge(status) {
    return `<span class="badge ${esc(status)}">${esc(status)}</span>`;
  }

  function allocationItem(allocation, actions) {
    const gpus = (allocation.gpus || []).length ? allocation.gpus.join(",") : "CPU-only";
    const remainingId = `remaining-${allocation.id}`;
    const contact = ownerContact(allocation);
    const deadline = allocation.remaining_seconds !== null && allocation.remaining_seconds !== undefined
      ? Date.now() + Number(allocation.remaining_seconds) * 1000
      : null;
    return `
      <div class="allocation-item" data-allocation="${esc(allocation.id)}">
        <div>
          <div class="id">${esc(allocation.id)}</div>
          <div class="muted">${esc(ownerLabel(allocation))} · ${statusBadge(allocation.status || "")}
            ${contact ? `<br>contact: ${esc(contact)}` : ""}</div>
        </div>
        <div class="muted">GPU: <b>${esc(gpus)}</b><br>CPU/RAM: ${esc(allocation.cpus)} / ${esc(allocation.memory_gb)} GiB</div>
        <div class="muted">
          ${deadline !== null
            ? `剩余 <span class="gpu-remaining" id="${esc(remainingId)}" data-deadline="${deadline}">${formatRemaining(allocation.remaining_seconds)}</span>`
            : "—"}
          ${allocation.container_name ? `<br>${esc(allocation.container_name)}` : ""}
          ${allocation.note ? `<br>note: ${esc(allocation.note)}` : ""}
        </div>
        <div class="row-actions">${actions || ""}</div>
      </div>
    `;
  }

  function actionButtons(allocation) {
    const id = esc(allocation.id);
    const active = allocation.status === "active";
    const release = (allocation.status === "active" || allocation.status === "pending")
      ? `<button data-action="release" data-id="${id}">release</button>`
      : "";
    const shell = active ? `<button data-action="shell" data-id="${id}">shell</button>` : "";
    const logs = allocation.container_id ? `<button data-action="logs" data-id="${id}">logs</button>` : "";
    // Persistent job logs outlive the container; offer them for every status.
    const history = `<button data-action="history" data-id="${id}">history</button>`;
    const vscode = active && state.config?.vscode_enabled
      ? `<button data-action="vscode" data-id="${id}">VS Code</button>`
      : "";
    return `${shell}${vscode}${logs}${history}${release}`;
  }

  function bindActions(root) {
    root.querySelectorAll("button[data-action]").forEach((button) => {
      button.addEventListener("click", () => {
        const action = button.dataset.action;
        const id = button.dataset.id;
        if (!id) return;
        if (action === "release") releaseAllocation(id);
        if (action === "shell") openTerminal(id);
        if (action === "logs") openLogs(id);
        if (action === "history") openJobHistory(id);
        if (action === "vscode") openVscode(id);
      });
    });
  }

  function renderAllAllocations(allocations) {
    const target = $("#all-allocations");
    if (!allocations.length) {
      target.className = "allocation-list empty";
      target.textContent = "No allocations.";
      return;
    }
    target.className = "allocation-list";
    target.innerHTML = allocations.map((allocation) =>
      allocationItem(allocation, "")
    ).join("");
  }

  function renderMyAllocations() {
    const target = $("#my-allocations");
    const data = state.my || {};
    const allocations = data.allocations || [];
    if (!allocations.length) {
      target.className = "allocation-list empty";
      target.textContent = "No allocations.";
      return;
    }
    target.className = "allocation-list";
    target.innerHTML = allocations.map((allocation) =>
      allocationItem(allocation, actionButtons(allocation))
    ).join("");
    bindActions(target);
  }

  function renderUnmanaged(unmanaged) {
    const containers = unmanaged.containers || [];
    const processes = unmanaged.processes || [];
    const containerTarget = $("#unmanaged-containers");
    if (!containers.length) {
      containerTarget.className = "allocation-list empty";
      containerTarget.textContent = "None.";
    } else {
      containerTarget.className = "allocation-list";
      containerTarget.innerHTML = containers.map((container) => `
        <div class="allocation-item">
          <div><div class="id">${esc(container.name)}</div><div class="muted">${esc(container.id)}</div></div>
          <div class="muted">${esc(container.image)}</div>
          <div class="muted">${esc(container.status)}</div>
          <div class="muted">${container.gpus?.length ? `GPU ${esc(container.gpus.join(","))}` : "no GPU"} · ${container.owner ? `owner ${esc(ownerLabel(container))}` : "unmanaged"}</div>
        </div>
      `).join("");
    }
    const processTarget = $("#unmanaged-processes");
    if (!processes.length) {
      processTarget.className = "allocation-list empty";
      processTarget.textContent = "None.";
    } else {
      processTarget.className = "allocation-list";
      processTarget.innerHTML = processes.map((process) => `
        <div class="allocation-item">
          <div><div class="id">PID ${esc(process.pid)}</div><div class="muted">GPU ${esc(process.gpu_index ?? "?")}</div></div>
          <div class="muted">${esc(process.process_name || "?")}</div>
          <div class="muted">${esc(process.used_mib)} MiB<br>${esc(process.owner_display || process.username || process.uid || "unknown")}${process.owner_public_contact ? `<br>contact ${esc(process.owner_public_contact)}` : ""}</div>
          <div class="muted">${process.container_name ? `container ${esc(process.container_name)}` : "read-only"}${process.container_owner ? `<br>owner ${esc(process.container_owner_display || process.container_owner)}` : ""}</div>
        </div>
      `).join("");
    }
  }

  async function refreshEnvironments() {
    try {
      const payload = await api("/api/projects");
      if (!payload.ok) throw new Error(payload?.error?.message || "project list failed");
      state.projects = payload.data.projects || [];
      renderProjects();
      const active = state.projects.filter((project) => project.status === "active");
      const select = $("#env-project");
      const wanted = state.envProject || select.value;
      select.innerHTML = active.map((project) =>
        `<option value="${esc(project.name)}">${esc(project.name)}</option>`
      ).join("");
      if (active.some((project) => project.name === wanted)) select.value = wanted;
      state.envProject = select.value || "";
      renderProjectChoices();
      await refreshVersions();
    } catch (error) {
      $("#project-list").className = "allocation-list empty";
      $("#project-list").textContent = `无法加载项目：${error.message || error}`;
    }
    await refreshLegacy();
  }

  function renderProjects() {
    const target = $("#project-list");
    if (!state.projects.length) {
      target.className = "allocation-list empty";
      target.textContent = "还没有项目；先在上方创建一个。";
      return;
    }
    target.className = "allocation-list";
    target.innerHTML = state.projects.map((project) => `
      <div class="allocation-item">
        <div>
          <div class="id">${esc(project.name)}</div>
          <div class="muted">${statusBadge(project.status)} · ${esc(project.versions)} 个版本</div>
        </div>
        <div class="muted">latest: ${esc(project.latest || "—")}<br>created ${esc(project.created_at)}</div>
        <div class="row-actions">
          <button data-action="project-open" data-name="${esc(project.name)}">查看版本</button>
          ${project.status === "active"
            ? `<button data-action="project-archive" data-name="${esc(project.name)}">归档</button>`
            : ""}
        </div>
      </div>
    `).join("");
    target.querySelectorAll("button[data-action]").forEach((button) => {
      button.addEventListener("click", () => {
        if (button.dataset.action === "project-open") openProject(button.dataset.name);
        if (button.dataset.action === "project-archive") archiveProject(button.dataset.name);
      });
    });
  }

  function renderProjectChoices() {
    const active = state.projects.filter((project) => project.status === "active");
    const options = active.map((project) =>
      `<option value="${esc(project.name)}">${esc(project.name)}</option>`
    ).join("");
    const target = $("#legacy-project");
    if (!target) return;
    const wanted = target.value;
    target.innerHTML = options;
    if (active.some((project) => project.name === wanted)) target.value = wanted;
  }

  function openProject(name) {
    if (!name) return;
    state.envProject = name;
    const select = $("#env-project");
    if (select) select.value = name;
    refreshVersions();
  }

  async function archiveProject(name) {
    if (!window.confirm(`归档项目 ${name}？归档后不能再采集或构建，但历史版本仍可查看。`)) return;
    try {
      const payload = await api(`/api/projects/${encodeURIComponent(name)}/archive`, {
        method: "POST",
      });
      if (!payload.ok) throw new Error(`${payload.error?.code}: ${payload.error?.message}`);
      flash(`已归档 ${name}`, "ok");
      await refreshEnvironments();
    } catch (error) {
      flash(String(error.message || error), "error");
    }
  }

  async function refreshVersions() {
    const project = $("#env-project")?.value || "";
    state.envProject = project;
    const target = $("#version-list");
    renderEnvCaptureChoices();
    if (!project) {
      state.versions = [];
      target.className = "allocation-list empty";
      target.textContent = "先创建或选择一个项目。";
      return;
    }
    try {
      const payload = await api(`/api/environments?project=${encodeURIComponent(project)}`);
      if (!payload.ok) throw new Error(payload?.error?.message || "env list failed");
      state.versions = payload.data.versions || [];
      renderVersions();
    } catch (error) {
      state.versions = [];
      target.className = "allocation-list empty";
      target.textContent = `无法加载环境版本：${error.message || error}`;
    }
  }

  function renderEnvCaptureChoices() {
    const select = $("#env-capture-allocation");
    if (!select) return;
    const allocations = (state.my?.allocations || []).filter(
      (allocation) => allocation.status === "active",
    );
    const current = select.value;
    select.innerHTML = allocations.map((allocation) => `
      <option value="${esc(allocation.id)}">${esc(allocation.id.slice(0, 12))} · GPU ${esc((allocation.gpus || []).join(",") || "cpu")}</option>
    `).join("");
    if (!allocations.length) {
      select.innerHTML = '<option value="">没有 active allocation</option>';
    } else if (allocations.some((allocation) => allocation.id === current)) {
      select.value = current;
    }
  }

  function renderVersions() {
    const target = $("#version-list");
    const versions = state.versions || [];
    if (!versions.length) {
      target.className = "allocation-list empty";
      target.textContent = "这个项目还没有环境版本。";
      return;
    }
    target.className = "allocation-list";
    target.innerHTML = versions.map((version, index) => {
      const previous = versions[index + 1];
      const candidates = (state.my?.allocations || []).filter(
        (allocation) =>
          allocation.status === "active" && allocation.env_version_id === version.version_id,
      );
      const verify = version.verification === "built" && candidates.length
        ? `<select data-role="verify-allocation" data-ref="${esc(version.ref)}">${candidates.map((allocation) =>
            `<option value="${esc(allocation.id)}">${esc(allocation.id.slice(0, 12))}</option>`
          ).join("")}</select>
          <button data-action="env-verify" data-ref="${esc(version.ref)}">验证通过</button>`
        : "";
      return `
        <div class="allocation-item">
          <div>
            <div class="id">${esc(version.ref)}</div>
            <div class="muted">${statusBadge(version.capture_fidelity)} ${statusBadge(version.verification)} · ${esc(version.origin)}</div>
          </div>
          <div class="muted">created ${esc(version.created_at)}<br>sha256 ${esc(String(version.sha256 || "").slice(0, 12))}</div>
          <div class="muted">visibility ${esc(version.visibility)}</div>
          <div class="row-actions">
            <button data-action="env-show" data-ref="${esc(version.ref)}">manifest</button>
            ${previous
              ? `<button data-action="env-diff" data-ref="${esc(version.ref)}" data-other="${esc(previous.ref)}">对比 ${esc(previous.ref)}</button>`
              : ""}
            <button data-action="env-build" data-ref="${esc(version.ref)}">构建镜像</button>
            ${verify}
          </div>
        </div>
      `;
    }).join("");
    target.querySelectorAll("button[data-action]").forEach((button) => {
      button.addEventListener("click", () => {
        const ref = button.dataset.ref;
        if (button.dataset.action === "env-show") showEnvVersion(ref);
        if (button.dataset.action === "env-diff") diffEnvVersion(ref, button.dataset.other);
        if (button.dataset.action === "env-build") buildEnvVersion(ref);
        if (button.dataset.action === "env-verify") {
          const select = Array.from(
            target.querySelectorAll('select[data-role="verify-allocation"]'),
          ).find((item) => item.dataset.ref === ref);
          verifyEnvVersion(ref, select ? select.value : "");
        }
      });
    });
  }

  async function showEnvVersion(ref) {
    if (!ref) return;
    try {
      const payload = await api(`/api/environments/${encodeURIComponent(ref)}`);
      if (!payload.ok) throw new Error(`${payload.error?.code}: ${payload.error?.message}`);
      const version = payload.data.version || {};
      const manifest = payload.data.manifest || {};
      const base = manifest.base || {};
      const python = manifest.python || {};
      const system = manifest.system || {};
      const packages = (python.packages || []).map((item) =>
        `<li>${esc(item.name)}${item.version ? `==${esc(item.version)}` : ""}</li>`
      ).join("");
      const builds = (payload.data.builds || []).map((build) =>
        `<li>${esc(build.status)} · ${esc(build.image_tag || "")} ${esc(String(build.image_id || "").slice(0, 19))}</li>`
      ).join("");
      showDetail(`环境 ${version.ref || ref}`, `
        <p>fidelity <b>${esc(version.capture_fidelity)}</b> · verification <b>${esc(version.verification)}</b> · origin ${esc(version.origin)}</p>
        <p>base: ${esc(base.image || "")}${base.digest ? `<br>digest ${esc(base.digest)}` : ""}</p>
        <p>python: ${esc(python.version || "unknown")} (${esc(python.manager || "-")}) ·
          ${(python.packages || []).length} packages · system ${(system.packages || []).length} packages</p>
        ${packages ? `<h3>Python packages</h3><ul>${packages}</ul>` : ""}
        ${builds ? `<h3>构建记录</h3><ul>${builds}</ul>` : ""}
      `);
    } catch (error) {
      flash(String(error.message || error), "error");
    }
  }

  async function diffEnvVersion(ref, other) {
    if (!ref || !other) return;
    try {
      const payload = await api(
        `/api/environments/${encodeURIComponent(ref)}/diff?other=${encodeURIComponent(other)}`,
      );
      if (!payload.ok) throw new Error(`${payload.error?.code}: ${payload.error?.message}`);
      const diff = payload.data.diff || {};
      if (!diff.changed) {
        showDetail(`${other} → ${ref}`, "<p>两个环境版本内容相同。</p>");
        return;
      }
      const base = Object.entries(diff.base || {}).map(([key, change]) =>
        `<li>base.${esc(key)}: ${esc(change.from)} → ${esc(change.to)}</li>`
      ).join("");
      const python = diff.python || {};
      const packageLines = [
        ...(python.packages?.added || []).map((item) => `<li>+ ${esc(item.name)}==${esc(item.version || "?")}</li>`),
        ...(python.packages?.removed || []).map((item) => `<li>- ${esc(item.name)}==${esc(item.version || "?")}</li>`),
        ...(python.packages?.changed || []).map((item) =>
          `<li>~ ${esc(item.name)}: ${esc(item.from)} → ${esc(item.to)}</li>`
        ),
      ].join("");
      showDetail(`${other} → ${ref}`, `
        ${base ? `<h3>Base</h3><ul>${base}</ul>` : ""}
        ${python.version ? `<p>python: ${esc(python.version.from)} → ${esc(python.version.to)}</p>` : ""}
        ${packageLines ? `<h3>Python packages</h3><ul>${packageLines}</ul>` : ""}
      `);
    } catch (error) {
      flash(String(error.message || error), "error");
    }
  }

  async function buildEnvVersion(ref) {
    if (!ref) return;
    if (!window.confirm(`构建 ${ref} 的镜像？构建会占用 CPU 和磁盘，可能耗时数分钟。`)) return;
    try {
      const payload = await api(`/api/environments/${encodeURIComponent(ref)}/build`, {
        method: "POST",
      });
      if (!payload.ok) throw new Error(`${payload.error?.code}: ${payload.error?.message}`);
      const build = payload.data.build || {};
      flash(`构建 ${build.status || "完成"}：${build.detail || build.image_tag || ""}`, "ok");
      await refreshVersions();
    } catch (error) {
      flash(String(error.message || error), "error");
    }
  }

  async function verifyEnvVersion(ref, allocationId) {
    if (!ref || !allocationId) {
      flash("先选择一个用该环境启动的 active allocation", "error");
      return;
    }
    if (!window.confirm(`确认 ${allocationId} 已用 ${ref} 完成训练，并标记功能验证通过？`)) return;
    try {
      const payload = await api(`/api/environments/${encodeURIComponent(ref)}/verify`, {
        method: "POST",
        body: { allocation: allocationId },
      });
      if (!payload.ok) throw new Error(`${payload.error?.code}: ${payload.error?.message}`);
      flash(`验证通过：${payload.data.version?.ref || ref}`, "ok");
      await refreshVersions();
    } catch (error) {
      flash(String(error.message || error), "error");
    }
  }

  async function refreshLegacy() {
    const sourceTarget = $("#legacy-sources");
    const claimTarget = $("#legacy-claims");
    try {
      const [sources, claims] = await Promise.all([
        api("/api/legacy/sources"),
        api("/api/legacy/claims"),
      ]);
      if (!sources.ok) throw new Error(sources?.error?.message || "legacy list failed");
      state.legacySources = sources.data.sources || [];
      state.legacyClaims = claims.ok ? claims.data.claims || [] : [];
      renderLegacySources(sources.ok ? "" : claims?.error?.message || "");
      renderLegacyClaims(claims.ok ? "" : claims?.error?.message || "");
    } catch (error) {
      const message = String(error.message || error);
      state.legacySources = [];
      state.legacyClaims = [];
      sourceTarget.className = "allocation-list empty";
      sourceTarget.textContent = `旧容器认领不可用：${message}`;
      claimTarget.className = "allocation-list empty";
      claimTarget.textContent = "—";
    }
    renderLegacyChoices();
  }

  function renderLegacySources(notice) {
    const target = $("#legacy-sources");
    if (notice) {
      target.className = "allocation-list empty";
      target.textContent = notice;
      return;
    }
    const sources = state.legacySources || [];
    if (!sources.length) {
      target.className = "allocation-list empty";
      target.textContent = "没有可认领的旧容器；等管理员扫描后再刷新。";
      return;
    }
    target.className = "allocation-list";
    target.innerHTML = sources.map((source) => {
      const metadata = source.metadata || {};
      return `
        <div class="allocation-item">
          <div><div class="id">${esc(source.source_id)}</div><div class="muted">${esc(source.container)} · ${statusBadge(source.status)}</div></div>
          <div class="muted">state ${esc(source.state)} · image ${esc(metadata.image || source.image || "")}</div>
          <div class="muted">created ${esc(source.created_at || "—")}<br>scanned ${esc(source.scanned_at || "—")}</div>
        </div>
      `;
    }).join("");
  }

  function renderLegacyChoices() {
    const select = $("#legacy-source");
    if (!select) return;
    const sources = (state.legacySources || []).filter((source) => source.status === "available");
    const current = select.value;
    select.innerHTML = sources.map((source) =>
      `<option value="${esc(source.source_id)}">${esc(source.source_id)} · ${esc(source.container)}</option>`
    ).join("");
    if (!sources.length) {
      select.innerHTML = '<option value="">没有可认领的旧容器</option>';
    } else if (sources.some((source) => source.source_id === current)) {
      select.value = current;
    }
  }

  function renderLegacyClaims(notice) {
    const target = $("#legacy-claims");
    if (notice) {
      target.className = "allocation-list empty";
      target.textContent = notice;
      return;
    }
    const claims = state.legacyClaims || [];
    if (!claims.length) {
      target.className = "allocation-list empty";
      target.textContent = "还没有认领申请。";
      return;
    }
    target.className = "allocation-list";
    target.innerHTML = claims.map((claim) => {
      const version = claim.version || {};
      return `
        <div class="allocation-item">
          <div><div class="id">${esc(claim.claim_id)}</div><div class="muted">${statusBadge(claim.status)} · source ${esc(claim.source?.source_id || "")}</div></div>
          <div class="muted">${esc(claim.reason || "—")}<br>version: ${esc(version.ref || "—")}</div>
          <div class="muted">${esc(claim.review_note || "")}</div>
          <div class="row-actions">
            <button data-action="legacy-show" data-id="${esc(claim.claim_id)}">详情</button>
          </div>
        </div>
      `;
    }).join("");
    target.querySelectorAll("button[data-action]").forEach((button) => {
      button.addEventListener("click", () => showLegacyClaim(button.dataset.id));
    });
  }

  async function showLegacyClaim(claimId) {
    if (!claimId) return;
    try {
      const payload = await api(`/api/legacy/claims/${encodeURIComponent(claimId)}`);
      if (!payload.ok) throw new Error(`${payload.error?.code}: ${payload.error?.message}`);
      const claim = payload.data.claim || {};
      const source = claim.source || {};
      const version = claim.version || {};
      showDetail(`认领 ${claim.claim_id}`, `
        <p>status <b>${esc(claim.status)}</b></p>
        <p>source: ${esc(source.source_id)} · ${esc(source.container)} · state ${esc(source.state)}</p>
        <p>image: ${esc(source.image || "")}</p>
        <p>reason: ${esc(claim.reason || "—")}</p>
        <p>review: ${esc(claim.review_note || "—")}</p>
        ${version.ref ? `<p>version: ${esc(version.ref)} · fidelity ${esc(version.capture_fidelity)}</p>` : ""}
      `);
    } catch (error) {
      flash(String(error.message || error), "error");
    }
  }

  async function refreshLegacyAdmin() {
    if (!state.adminEnabled) return;
    try {
      const [sources, claims] = await Promise.all([
        api("/api/admin/legacy/sources"),
        api("/api/admin/legacy/claims"),
      ]);
      if (!sources.ok) throw new Error(sources?.error?.message || "legacy sources failed");
      state.adminLegacySources = sources.data.sources || [];
      state.adminLegacyClaims = claims.ok ? claims.data.claims || [] : [];
      renderAdminLegacySources(sources.ok ? "" : claims?.error?.message || "");
      renderAdminLegacyClaims(claims.ok ? "" : claims?.error?.message || "");
    } catch (error) {
      const message = String(error.message || error);
      state.adminLegacySources = [];
      state.adminLegacyClaims = [];
      $("#legacy-admin-sources").className = "allocation-list empty";
      $("#legacy-admin-sources").textContent = `旧容器审核不可用：${message}`;
      $("#legacy-admin-claims").className = "allocation-list empty";
      $("#legacy-admin-claims").textContent = "—";
    }
  }

  async function scanLegacySources() {
    if (!window.confirm("扫描主机上的非托管容器并登记为可认领来源？只读取元数据。")) return;
    try {
      const payload = await api("/api/admin/legacy/scan", { method: "POST" });
      if (!payload.ok) throw new Error(`${payload.error?.code}: ${payload.error?.message}`);
      flash(
        `扫描完成：${payload.data.scanned ?? 0} 个容器，${payload.data.dismissed ?? 0} 个下架`,
        "ok",
      );
      await refreshLegacyAdmin();
      await refreshLegacy();
    } catch (error) {
      flash(String(error.message || error), "error");
    }
  }

  function renderAdminLegacySources(notice) {
    const target = $("#legacy-admin-sources");
    if (notice) {
      target.className = "allocation-list empty";
      target.textContent = notice;
      return;
    }
    const sources = state.adminLegacySources || [];
    if (!sources.length) {
      target.className = "allocation-list empty";
      target.textContent = "没有登记旧容器来源。";
      return;
    }
    target.className = "allocation-list";
    target.innerHTML = sources.map((source) => `
      <div class="allocation-item">
        <div><div class="id">${esc(source.container_name || source.container_id)}</div><div class="muted">${esc(String(source.container_id || "").slice(0, 19))} · ${statusBadge(source.status)}</div></div>
        <div class="muted">state ${esc(source.state)} · image ${esc(source.image || "")}<br>${esc(source.image_id || "")}</div>
        <div class="muted">created ${esc(source.created_at || "—")}<br>scanned ${esc(source.scanned_at || "—")}</div>
        <div class="muted">${esc(source.server_id || "")}</div>
      </div>
    `).join("");
  }

  function renderAdminLegacyClaims(notice) {
    const target = $("#legacy-admin-claims");
    if (notice) {
      target.className = "allocation-list empty";
      target.textContent = notice;
      return;
    }
    const claims = state.adminLegacyClaims || [];
    if (!claims.length) {
      target.className = "allocation-list empty";
      target.textContent = "没有认领申请。";
      return;
    }
    target.className = "allocation-list";
    target.innerHTML = claims.map((claim) => {
      const version = claim.version || {};
      const actions = claim.status === "pending"
        ? `<button data-action="legacy-approve" data-id="${esc(claim.claim_id)}">批准</button>
           <button data-action="legacy-reject" data-id="${esc(claim.claim_id)}">驳回</button>`
        : (claim.status === "approved" && !claim.version_id
            ? `<button data-action="legacy-capture" data-id="${esc(claim.claim_id)}">只读采集</button>`
            : "");
      return `
        <div class="allocation-item">
          <div><div class="id">${esc(claim.claim_id)}</div><div class="muted">${statusBadge(claim.status)} · member ${esc(claim.member_id)}</div></div>
          <div class="muted">source ${esc(claim.source?.source_id || "")} · ${esc(claim.source?.container_name || claim.source?.container || "")}</div>
          <div class="muted">${esc(claim.reason || "—")}<br>version ${esc(version.ref || "—")}</div>
          <div class="row-actions">${actions}</div>
        </div>
      `;
    }).join("");
    target.querySelectorAll("button[data-action]").forEach((button) => {
      button.addEventListener("click", () => {
        if (button.dataset.action === "legacy-approve") decideLegacyClaim(button.dataset.id, true);
        if (button.dataset.action === "legacy-reject") decideLegacyClaim(button.dataset.id, false);
        if (button.dataset.action === "legacy-capture") captureLegacyClaim(button.dataset.id);
      });
    });
  }

  async function decideLegacyClaim(claimId, approve) {
    if (!claimId) return;
    const note = window.prompt(
      approve ? "批准说明（可选，会显示给申请人）" : "驳回原因（会显示给申请人）",
      "",
    );
    if (note === null) return;
    try {
      const payload = await api("/api/admin/legacy/decision", {
        method: "POST",
        body: { id: claimId, approve, note },
      });
      if (!payload.ok) throw new Error(`${payload.error?.code}: ${payload.error?.message}`);
      flash(`认领 ${claimId}：${payload.data.claim?.status || (approve ? "approved" : "rejected")}`, "ok");
      await refreshLegacyAdmin();
      await refreshLegacy();
    } catch (error) {
      flash(String(error.message || error), "error");
    }
  }

  async function captureLegacyClaim(claimId) {
    if (!claimId) return;
    if (!window.confirm("对该已批准认领做只读采集并生成环境版本？不会执行 docker commit。")) return;
    try {
      const payload = await api("/api/admin/legacy/capture", {
        method: "POST",
        body: { id: claimId },
      });
      if (!payload.ok) throw new Error(`${payload.error?.code}: ${payload.error?.message}`);
      const version = payload.data.version || {};
      flash(`已生成 ${version.ref} · fidelity ${version.capture_fidelity}`, "ok");
      await refreshLegacyAdmin();
      await refreshLegacy();
      await refreshEnvironments();
    } catch (error) {
      flash(String(error.message || error), "error");
    }
  }

  async function releaseAllocation(id) {
    if (!window.confirm(`Release ${id}?`)) return;
    try {
      const payload = await api("/api/release", { method: "POST", body: { id } });
      if (!payload.ok) throw new Error(`${payload.error?.code}: ${payload.error?.message}`);
      flash(`Released ${id}`, "ok");
      await refresh();
    } catch (error) {
      flash(String(error.message || error), "error");
    }
  }

  async function cancelQueue(id) {
    if (!window.confirm(`Cancel queued request ${id}?`)) return;
    try {
      const payload = await api("/api/queue/cancel", { method: "POST", body: { id } });
      if (!payload.ok) throw new Error(`${payload.error?.code}: ${payload.error?.message}`);
      flash(`Cancelled ${id}`, "ok");
      await refresh();
    } catch (error) {
      flash(String(error.message || error), "error");
    }
  }

  async function stopJob(allocationId, name) {
    if (!window.confirm(`Stop job ${name}?`)) return;
    try {
      const payload = await api(`/api/jobs/${encodeURIComponent(allocationId)}/stop`, {
        method: "POST",
        body: { name },
      });
      if (!payload.ok) throw new Error(`${payload.error?.code}: ${payload.error?.message}`);
      flash(`Stopped ${name}`, "ok");
      await refreshJobs();
    } catch (error) {
      flash(String(error.message || error), "error");
    }
  }

  async function startJob(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const errorTarget = $("#job-start-error");
    errorTarget.textContent = "";
    const data = new FormData(form);
    const allocationId = String(data.get("allocation") || "");
    const name = String(data.get("name") || "").trim();
    // Send the command verbatim: the container runs it via `bash -lc`, so
    // quotes and spaces survive (e.g. --output "experiment one").
    const command = String(data.get("command") || "").trim();
    if (!allocationId || !name || !command) {
      errorTarget.textContent = "allocation、任务名和命令都不能为空";
      return;
    }
    try {
      const payload = await api(`/api/jobs/${encodeURIComponent(allocationId)}/start`, {
        method: "POST",
        body: { name, command },
      });
      if (!payload.ok) throw new Error(`${payload.error?.code}: ${payload.error?.message}`);
      flash(`Job ${payload.data.name} started; log: ${payload.data.log}`, "ok");
      form.reset();
      await refreshJobs();
    } catch (error) {
      errorTarget.textContent = String(error.message || error);
    }
  }

  async function openVscode(id) {
    try {
      const payload = await api(`/api/vscode/${encodeURIComponent(id)}`, { method: "POST" });
      if (!payload.ok) throw new Error(`${payload.error?.code}: ${payload.error?.message}`);
      const uri = String(payload.data?.uri || "");
      const message = String(payload.data?.message || "");
      if (payload.data?.stage === "ready") {
        flash(`VS Code 已启动${uri ? `: ${uri}` : ""}`, "ok", 15000);
      } else {
        // The IDE window is not confirmed; surface the copyable URI instead.
        flash(
          `隧道已验证（容器 SSH 可达）${message ? ` — ${message}` : ""}${uri ? `。打开: ${uri}` : ""}`,
          "ok",
          30000,
        );
      }
    } catch (error) {
      flash(String(error.message || error), "error");
    }
  }

  async function openLogs(id) {
    closeTerminal();
    $("#terminal-modal").hidden = true;
    $("#logs-modal").hidden = false;
    $("#modal-backdrop").hidden = false;
    $("#logs-modal-title").textContent = "Logs";
    $("#logs-content").textContent = "loading…";
    try {
      const payload = await api(`/api/logs/${encodeURIComponent(id)}?tail=300`);
      if (!payload.ok) throw new Error(`${payload.error?.code}: ${payload.error?.message}`);
      $("#logs-content").textContent = payload.data.logs || "(empty)";
    } catch (error) {
      $("#logs-content").textContent = String(error.message || error);
    }
  }

  async function openJobLogs(id, name) {
    closeTerminal();
    $("#terminal-modal").hidden = true;
    $("#logs-modal").hidden = false;
    $("#modal-backdrop").hidden = false;
    $("#logs-modal-title").textContent = `Job logs · ${name}`;
    $("#logs-content").textContent = "loading…";
    try {
      const payload = await api(
        `/api/job-logs/${encodeURIComponent(id)}?name=${encodeURIComponent(name)}&tail=300`,
      );
      if (!payload.ok) throw new Error(`${payload.error?.code}: ${payload.error?.message}`);
      $("#logs-content").textContent = payload.data.logs || "(empty)";
    } catch (error) {
      $("#logs-content").textContent = String(error.message || error);
    }
  }

  async function openJobHistory(id) {
    closeTerminal();
    $("#terminal-modal").hidden = true;
    $("#logs-modal").hidden = true;
    $("#history-modal").hidden = false;
    $("#modal-backdrop").hidden = false;
    $("#history-modal-title").textContent = "Job history";
    const target = $("#history-list");
    target.textContent = "loading…";
    try {
      const payload = await api(`/api/job-history/${encodeURIComponent(id)}`);
      if (!payload.ok) throw new Error(`${payload.error?.code}: ${payload.error?.message}`);
      const files = payload.data.files || [];
      if (!files.length) {
        target.className = "allocation-list empty";
        target.textContent = "No log files.";
        return;
      }
      target.className = "allocation-list";
      target.innerHTML = files.map((file) => `
        <div class="allocation-item">
          <div><div class="id">${esc(file.name)}</div></div>
          <div class="muted">${esc(file.bytes ?? 0)} bytes</div>
          <div class="row-actions"><button data-log="${esc(String(file.name).replace(/\.log$/, ""))}">view</button></div>
        </div>
      `).join("");
      target.querySelectorAll("button[data-log]").forEach((button) => {
        button.addEventListener("click", () => openJobLogs(id, button.dataset.log));
      });
    } catch (error) {
      target.className = "allocation-list empty";
      target.textContent = String(error.message || error);
    }
  }

  function closeModal() {
    closeTerminal();
    $("#modal-backdrop").hidden = true;
    $("#logs-modal").hidden = true;
    $("#terminal-modal").hidden = true;
    $("#history-modal").hidden = true;
    $("#detail-modal").hidden = true;
    $("#logs-content").textContent = "";
    $("#history-list").textContent = "";
    $("#detail-content").textContent = "";
  }

  function showDetail(title, html) {
    closeTerminal();
    $("#logs-modal").hidden = true;
    $("#history-modal").hidden = true;
    $("#terminal-modal").hidden = true;
    $("#detail-modal").hidden = false;
    $("#modal-backdrop").hidden = false;
    $("#detail-title").textContent = title;
    $("#detail-content").innerHTML = html;
  }

  function openTerminal(id, job = "") {
    closeTerminal();
    if (!window.Terminal || !window.FitAddon) {
      flash("xterm.js assets are missing", "error");
      return;
    }
    $("#logs-modal").hidden = true;
    $("#terminal-modal").hidden = false;
    $("#modal-backdrop").hidden = false;
    $("#terminal-status").textContent = "connecting…";
    const element = $("#terminal");
    element.innerHTML = "";
    const term = new window.Terminal({
      cursorBlink: true,
      fontSize: 13,
      fontFamily: 'ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace',
      scrollback: 5000,
      theme: {
        background: "#050a14",
        foreground: "#e6edf7",
        cursor: "#5eead4",
        selectionBackground: "#334155",
        black: "#0b1120",
        brightBlack: "#64748b",
        green: "#34d399",
        brightGreen: "#6ee7b7",
        yellow: "#fbbf24",
        blue: "#38bdf8",
        cyan: "#5eead4",
        red: "#fb7185",
        white: "#e2e8f0",
      },
    });
    const fit = new window.FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(element);
    fit.fit();
    term.focus();
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const jobQuery = job ? `&job=${encodeURIComponent(job)}` : "";
    const url = `${protocol}//${window.location.host}/ws/shell/${encodeURIComponent(id)}?token=${encodeURIComponent(token)}&cols=${term.cols}&rows=${term.rows}${jobQuery}`;
    const socket = new WebSocket(url);
    socket.binaryType = "arraybuffer";
    state.terminal = { term, fit, socket };
    socket.onopen = () => {
      $("#terminal-status").textContent = "connected · close this window to disconnect";
      fit.fit();
      term.focus();
    };
    socket.onmessage = (event) => {
      if (typeof event.data === "string") {
        try {
          const message = JSON.parse(event.data);
          if (message.type === "error") term.write(`\r\n[error] ${message.message}\r\n`);
          else if (message.type === "exit") term.write(`\r\n[exit ${message.code}]\r\n`);
        } catch {
          term.write(event.data);
        }
      } else {
        term.write(new Uint8Array(event.data));
      }
    };
    socket.onerror = () => {
      $("#terminal-status").textContent = "terminal connection error";
    };
    socket.onclose = () => {
      $("#terminal-status").textContent = "session closed";
    };
    term.onData((data) => {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: "input", data }));
      }
    });
    term.onResize(({ cols, rows }) => {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: "resize", cols, rows }));
      }
    });
    window.setTimeout(() => {
      fit.fit();
      term.focus();
    }, 80);
  }

  function closeTerminal() {
    if (!state.terminal) return;
    const { term, socket } = state.terminal;
    state.terminal = null;
    try {
      if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
        socket.close();
      }
    } catch {
      // ignore
    }
    try {
      term.dispose();
    } catch {
      // ignore
    }
  }

  async function refreshMembership() {
    if (!state.adminEnabled) return;
    try {
      const [requests, members] = await Promise.all([
        api("/api/admin/membership/requests"),
        api("/api/admin/membership/members"),
      ]);
      if (requests.ok) renderMembershipRequests(requests.data || []);
      if (members.ok) renderMembershipMembers(members.data || []);
    } catch (error) {
      flash(String(error.message || error), "error");
    }
  }

  const memberStatusLabels = {
    provisioning: "开户中",
    active: "有效",
    suspended: "已暂停",
    expired: "已到期",
    revoked: "已撤销",
    provision_failed: "开户失败",
  };

  function requestVersion(request) {
    return String(request.version || "");
  }

  async function decideRequest(request, action) {
    const body = {
      request_id: request.request_id,
      expected_version: requestVersion(request),
    };
    if (action === "approve") {
      const validUntil = window.prompt(
        "成员资格有效期（ISO 时间，留空表示无固定期限）：",
        "",
      );
      if (validUntil === null) return;
      const quota = window.prompt("workspace 配额 GiB：", "200");
      if (quota === null) return;
      const quotaNumber = Number(quota);
      if (!Number.isInteger(quotaNumber) || quotaNumber <= 0) {
        flash("配额必须是正整数", "error");
        return;
      }
      const note = window.prompt("申请人可见说明：", "已批准，正在自动开户。");
      if (note === null) return;
      body.valid_until = validUntil.trim();
      body.quota_gb = quotaNumber;
      body.review_note = note.trim() || "已批准";
      if (!window.confirm(`批准 ${request.request_id}（版本 ${requestVersion(request).slice(0, 12)}…）？`)) return;
    } else {
      const label = action === "needs-info" ? "需要补充的材料（申请人可见）：" : "拒绝原因（申请人可见）：";
      const note = window.prompt(label);
      if (!note || !note.trim()) return;
      body.review_note = note.trim();
    }
    const payload = await api(`/api/admin/membership/requests/${action}`, {
      method: "POST",
      body,
    });
    if (!payload.ok) throw new Error(`${payload.error?.code}: ${payload.error?.message}`);
    flash(
      `申请 ${request.request_id}：${
        action === "approve" ? "已批准并提交开户" : action === "needs-info" ? "已要求补充" : "已拒绝"
      }`,
      "ok",
    );
    refreshMembership();
  }

  function renderMembershipRequests(data) {
    const target = $("#membership-requests");
    const requests = data.requests || [];
    if (!requests.length) {
      target.className = "allocation-list empty";
      target.textContent = "暂无申请。";
      return;
    }
    target.className = "allocation-list";
    target.innerHTML = requests.map((request) => `
      <div class="allocation-item">
        <div><div class="id">${esc(request.display_name || request.username)}</div>
          <div class="muted">${esc(request.request_id)} · ${esc(request.status)}</div>
          <div class="muted">版本 ${esc(requestVersion(request).slice(0, 16))}${requestVersion(request).length > 16 ? "…" : ""}</div>
        </div>
        <div class="muted">${esc(request.affiliation || "")}<br>${esc(request.purpose || "")}</div>
        <div class="muted">${(request.keys || []).map((key) => esc(key.fingerprint)).join("<br>")}</div>
        <div class="row-actions">
          <button class="ghost" data-mdetail="${esc(request.request_id)}">详情</button>
          <button data-maction="approve" data-request="${esc(request.request_id)}">批准</button>
          <button class="ghost" data-maction="needs-info" data-request="${esc(request.request_id)}">需补充</button>
          <button class="danger" data-maction="reject" data-request="${esc(request.request_id)}">拒绝</button>
        </div>
      </div>
    `).join("");
    const byId = new Map(requests.map((request) => [request.request_id, request]));
    target.querySelectorAll("button[data-maction]").forEach((button) => {
      button.addEventListener("click", async () => {
        const request = byId.get(button.dataset.request);
        if (!request) return;
        try {
          await decideRequest(request, button.dataset.maction);
        } catch (error) {
          flash(String(error.message || error), "error");
        }
      });
    });
    target.querySelectorAll("button[data-mdetail]").forEach((button) => {
      button.addEventListener("click", async () => {
        const id = button.dataset.mdetail;
        showDetail("申请详情", '<p class="muted">正在加载…</p>');
        try {
          const payload = await api(`/api/admin/membership/requests/${encodeURIComponent(id)}`);
          if (!payload.ok) throw new Error(`${payload.error?.code}: ${payload.error?.message}`);
          const request = payload.data || {};
          const decision = request.decision || {};
          showDetail(
            `申请 · ${request.display_name || request.username || id}`,
            `<div class="allocation-list">
              <div class="allocation-item"><div><div class="id">${esc(request.username || "")}</div></div>
                <div class="muted">状态 ${esc(request.status || "")} · 版本 ${esc(requestVersion(request))}</div></div>
              <div class="allocation-item"><div class="muted">
                <strong>姓名：</strong>${esc(request.display_name || "")}<br>
                <strong>单位：</strong>${esc(request.affiliation || "")}<br>
                <strong>联系方式：</strong>${esc(request.contact || "")}<br>
                <strong>昵称/公开联系：</strong>${esc(request.nickname || "—")} / ${esc(request.public_contact || "—")}<br>
                <strong>用途：</strong>${esc(request.purpose || "")}<br>
                <strong>创建：</strong>${esc(request.created_at || "")}<br>
                <strong>更新：</strong>${esc(request.updated_at || "")}
              </div></div>
              <div class="allocation-item"><div><div class="id">公钥</div>
                <div class="muted">${(request.keys || []).map((key) => esc(key.fingerprint)).join("<br>") || "无"}</div></div></div>
              <div class="allocation-item"><div><div class="id">管理员决定</div>
                <div class="muted">${decision.status ? `状态 ${esc(decision.status)} · 版本 ${esc(decision.version || "")}<br>${esc(decision.review_note || "")}` : "尚未记录决定"}</div></div></div>
            </div>`,
          );
        } catch (error) {
          showDetail("申请详情", `<p class="error-text">${esc(String(error.message || error))}</p>`);
        }
      });
    });
  }

  async function memberAction(member, action) {
    const reason = window.prompt(
      `${action === "suspend" ? "暂停" : action === "resume" ? "恢复" : "撤销"}原因（必填）：`,
    );
    if (!reason || !reason.trim()) return;
    if (action === "revoke" && !window.confirm("撤销会立即移除登录凭据并关闭连接，workspace 数据保留。确定？")) return;
    const payload = await api(`/api/admin/membership/${action}`, {
      method: "POST",
      body: { member_id: member.member_id, reason: reason.trim() },
    });
    if (!payload.ok) throw new Error(`${payload.error?.code}: ${payload.error?.message}`);
    const result = payload.data || {};
    flash(
      `成员 ${member.member_id}：${result.status || action}${
        result.credential_revocation === "pending_retry"
          ? "（访问已阻断；凭据撤销待重试，请到操作详情重试）"
          : ""
      }`,
      result.credential_revocation === "pending_retry" ? "error" : "ok",
    );
    refreshMembership();
  }

  function renderMembershipMembers(data) {
    const target = $("#membership-members");
    const members = data.members || [];
    if (!members.length) {
      target.className = "allocation-list empty";
      target.textContent = "暂无成员。";
      return;
    }
    target.className = "allocation-list";
    target.innerHTML = members.map((member) => {
      const resumable = member.status === "suspended" || member.status === "expired";
      return `
      <div class="allocation-item">
        <div><div class="id">${esc(member.display_name || member.linux_username || member.member_id)}</div>
          <div class="muted">${esc(member.member_id)}</div></div>
        <div class="muted">${esc(memberStatusLabels[member.status] || member.status)} · uid ${esc(member.linux_uid ?? "—")}<br>
          ${member.valid_until ? `至 ${esc(member.valid_until)}` : "无固定期限"} · 版本 ${esc(member.access_version ?? "—")}</div>
        <div class="row-actions">
          <button class="ghost" data-membershow="${esc(member.member_id)}">详情</button>
          ${resumable ? `<button data-member="${esc(member.member_id)}" data-mact="resume">恢复</button>` : ""}
          ${member.status === "active" ? `<button class="ghost" data-member="${esc(member.member_id)}" data-mact="suspend">暂停</button>` : ""}
          ${member.status !== "revoked" ? `<button class="danger" data-member="${esc(member.member_id)}" data-mact="revoke">撤销</button>` : ""}
        </div>
      </div>`;
    }).join("");
    const byId = new Map(members.map((member) => [member.member_id, member]));
    target.querySelectorAll("button[data-member]").forEach((button) => {
      button.addEventListener("click", async () => {
        const member = byId.get(button.dataset.member);
        if (!member) return;
        try {
          await memberAction(member, button.dataset.mact);
        } catch (error) {
          flash(String(error.message || error), "error");
        }
      });
    });
    target.querySelectorAll("button[data-membershow]").forEach((button) => {
      button.addEventListener("click", async () => {
        const id = button.dataset.membershow;
        showDetail("成员详情", '<p class="muted">正在加载…</p>');
        try {
          const payload = await api(`/api/admin/membership/members/${encodeURIComponent(id)}`);
          if (!payload.ok) throw new Error(`${payload.error?.code}: ${payload.error?.message}`);
          const member = (payload.data || {}).member || {};
          const keys = (payload.data || {}).keys || [];
          const operations = (payload.data || {}).operations || [];
          showDetail(
            `成员 · ${member.display_name || member.linux_username || id}`,
            `<div class="allocation-list">
              <div class="allocation-item"><div class="muted">
                <strong>ID：</strong>${esc(member.member_id || "")}<br>
                <strong>Linux：</strong>${esc(member.linux_username || "—")} (uid ${esc(member.linux_uid ?? "—")})<br>
                <strong>状态：</strong>${esc(memberStatusLabels[member.status] || member.status || "")}<br>
                <strong>有效期：</strong>${esc(member.valid_until || "无固定期限")}<br>
                <strong>访问版本：</strong>${esc(member.access_version ?? "—")}<br>
                <strong>创建/更新：</strong>${esc(member.created_at || "")} / ${esc(member.updated_at || "")}
              </div></div>
              <div class="allocation-item"><div><div class="id">公钥</div>
                <div id="member-key-list" class="allocation-list"></div></div></div>
              <div class="allocation-item"><div><div class="id">添加公钥</div>
                <textarea id="member-new-key" placeholder="ssh-ed25519 AAAA... member@host"></textarea>
                <div class="row-actions"><button id="member-add-key">添加公钥</button></div>
              </div></div>
              <div class="allocation-item"><div><div class="id">操作记录</div>
                <div class="muted">${operations.map((op) => `${esc(op.action)} · ${esc(op.status)} · 尝试 ${esc(op.attempts)}${op.last_error ? `<br>${esc(op.last_error)}` : ""}`).join("<br><br>") || "无"}</div></div></div>
              <div class="row-actions" id="member-op-actions"></div>
            </div>`,
          );
          const actions = $("#member-op-actions");
          const keyList = $("#member-key-list");
          if (!keys.length) {
            keyList.className = "allocation-list empty";
            keyList.textContent = "无";
          } else {
            keyList.className = "allocation-list";
            keys.forEach((key) => {
              const row = document.createElement("div");
              row.className = "allocation-item";
              const label = document.createElement("div");
              label.className = "muted";
              label.textContent = `${key.status} · ${key.fingerprint}`;
              row.appendChild(label);
              if (key.status === "active") {
                const revoke = document.createElement("button");
                revoke.className = "danger";
                revoke.textContent = "撤销";
                revoke.addEventListener("click", async () => {
                  if (!window.confirm("确认撤销该公钥？该成员的连接会被关闭。")) return;
                  try {
                    const result = await api("/api/admin/membership/keys/revoke", {
                      method: "POST",
                      body: { member_id: member.member_id, key_id: key.key_id },
                    });
                    if (!result.ok) throw new Error(`${result.error?.code}: ${result.error?.message}`);
                    flash("公钥已撤销", "ok");
                    closeModal();
                    refreshMembership();
                  } catch (error) {
                    flash(String(error.message || error), "error");
                  }
                });
                row.appendChild(revoke);
              }
              keyList.appendChild(row);
            });
          }
          $("#member-add-key").addEventListener("click", async () => {
            const raw = $("#member-new-key").value.trim();
            if (!raw) {
              flash("请粘贴公钥", "error");
              return;
            }
            try {
              const result = await api("/api/admin/membership/keys/add", {
                method: "POST",
                body: { member_id: member.member_id, public_key: raw },
              });
              if (!result.ok) throw new Error(`${result.error?.code}: ${result.error?.message}`);
              flash(`公钥已添加：${result.data?.fingerprint || ""}`, "ok");
              closeModal();
              refreshMembership();
            } catch (error) {
              flash(String(error.message || error), "error");
            }
          });
          operations.filter((op) => op.status === "failed").forEach((op) => {
            const retry = document.createElement("button");
            retry.textContent = `重试 ${op.action}`;
            retry.addEventListener("click", async () => {
              try {
                const result = await api("/api/admin/membership/retry", {
                  method: "POST",
                  body: { operation_id: op.operation_id },
                });
                if (!result.ok) throw new Error(`${result.error?.code}: ${result.error?.message}`);
                flash(`操作 ${op.operation_id}：${result.data?.status || "已提交重试"}`, "ok");
                closeModal();
                refreshMembership();
              } catch (error) {
                flash(String(error.message || error), "error");
              }
            });
            actions.appendChild(retry);
          });
        } catch (error) {
          showDetail("成员详情", `<p class="error-text">${esc(String(error.message || error))}</p>`);
        }
      });
    });
  }

  async function refreshAdmin() {
    if (!state.adminEnabled) return;
    try {
      const [users, resources, audit] = await Promise.all([
        api("/api/admin/users"),
        api("/api/admin/resources"),
        api("/api/admin/audit?limit=100"),
      ]);
      if (users.ok) renderAdminUsers(users.data.users || []);
      if (resources.ok) renderAdminResources(resources.data);
      if (audit.ok) renderAdminAudit(audit.data.events || []);
      await refreshMembership();
    } catch (error) {
      flash(String(error.message || error), "error");
    }
  }

  function renderAdminUsers(users) {
    const target = $("#admin-users");
    if (!users.length) {
      target.className = "allocation-list empty";
      target.textContent = "No users.";
      return;
    }
    target.className = "allocation-list";
    target.innerHTML = users.map((user) => `
      <div class="allocation-item">
        <div class="id">${esc(user.username)}</div>
        <div class="muted">uid ${esc(user.uid)}</div>
        <div class="muted">${esc(user.role)}</div>
        <div class="muted">${esc(user.created_at)}</div>
      </div>
    `).join("");
  }

  function renderAdminResources(data) {
    const target = $("#admin-resources");
    const gpus = data.gpus || [];
    const allocations = data.allocations || [];
    target.className = "allocation-list";
    const gpuLines = gpus.map((gpu) =>
      `<div class="allocation-item"><div>GPU ${esc(gpu.index)}</div><div class="muted">${esc(gpu.state)}</div><div class="muted">${esc(ownerLabel(gpu) || "—")}</div><div class="muted">${esc(gpu.allocation_id || "")}</div></div>`
    ).join("");
    const allocLines = allocations.map((allocation) =>
      `<div class="allocation-item"><div class="id">${esc(allocation.id)}</div><div class="muted">${esc(allocation.status)}</div><div class="muted">${esc(ownerLabel(allocation))}</div><div class="muted">${esc(allocation.gpus)}</div>${allocation.note ? `<div class="muted">${esc(allocation.note)}</div>` : ""}</div>`
    ).join("");
    target.innerHTML = gpuLines + allocLines || '<div class="allocation-list empty">No data.</div>';
  }

  function renderAdminAudit(events) {
    const target = $("#admin-audit-list");
    if (!events.length) {
      target.className = "allocation-list empty";
      target.textContent = "No events.";
      return;
    }
    target.className = "allocation-list";
    target.innerHTML = events.map((event) => `
      <div class="allocation-item">
        <div class="muted">${esc(event.ts)}</div>
        <div><b>${esc(event.action)}</b></div>
        <div class="muted">${esc(event.actor_uid ?? "—")} · ${esc(event.allocation_id || "")}</div>
        <div class="muted">${esc(JSON.stringify(event.detail || {}))}</div>
      </div>
    `).join("");
  }

  async function changeBrokerMode(mode) {
    const target = $("#mode-error");
    target.textContent = "";
    if (mode === "active" && !window.confirm("Switch broker to active mode? Only do this after approval.")) return;
    try {
      const payload = await api("/api/admin/mode", { method: "POST", body: { mode } });
      if (!payload.ok) throw new Error(`${payload.error?.code}: ${payload.error?.message}`);
      flash(`Mode: ${payload.data.mode}`, "ok");
      await refresh();
    } catch (error) {
      target.textContent = String(error.message || error);
    }
  }

  function tick() {
    $$("[data-deadline]").forEach((element) => {
      const deadline = Number(element.dataset.deadline);
      if (!Number.isFinite(deadline)) return;
      const remaining = Math.max(0, Math.floor((deadline - Date.now()) / 1000));
      element.textContent = formatRemaining(remaining);
      element.classList.toggle("warn", remaining > 0 && remaining < 600);
    });
  }

  function bindForms() {
    $("#apply-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const errorTarget = $("#apply-error");
      errorTarget.textContent = "";
      const data = new FormData(form);
      const gpus = Array.from(form.querySelectorAll("input[name=gpu]:checked"))
        .map((input) => Number(input.value));
      // Keep one logical request key across retries of this form. A double
      // click or a transient network failure must not create a second queue
      // entry or allocation.
      const idempotencyKey = form.dataset.idempotencyKey || crypto.randomUUID();
      form.dataset.idempotencyKey = idempotencyKey;
      const body = {
        gpus,
        cpus: Number(data.get("cpus")),
        memory_gb: Number(data.get("memory_gb")),
        hours: Number(data.get("hours")),
        queue: data.get("queue") === "on",
        idempotency_key: idempotencyKey,
      };
      const image = String(data.get("image") || "").trim();
      const env = String(data.get("env") || "").trim();
      const note = String(data.get("note") || "").trim();
      if (image && env) {
        errorTarget.textContent = "镜像与环境版本互斥：二选一";
        return;
      }
      if (image) body.image = image;
      if (env) body.env = env;
      if (note) body.note = note;
      try {
        const payload = await api("/api/apply", { method: "POST", body });
        if (!payload.ok) throw new Error(`${payload.error?.code}: ${payload.error?.message}`);
        if (payload.data.queue_id) {
          flash(`Queued: ${payload.data.queue_id} (position ${payload.data.position})`, "ok");
        } else {
          flash(`Allocation ready: ${payload.data.id}`, "ok");
        }
        state.selectedGpus.clear();
        delete form.dataset.idempotencyKey;
        form.reset();
        activateTab("allocations");
        await refresh();
      } catch (error) {
        errorTarget.textContent = String(error.message || error);
      }
    });

    $("#job-start-form").addEventListener("submit", startJob);

    $("#project-create-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const target = $("#project-error");
      target.textContent = "";
      const name = String(new FormData(form).get("name") || "").trim();
      if (!name) return;
      try {
        const payload = await api("/api/projects", { method: "POST", body: { name } });
        if (!payload.ok) throw new Error(`${payload.error?.code}: ${payload.error?.message}`);
        flash(`已创建项目 ${payload.data.project?.name || name}`, "ok");
        form.reset();
        state.envProject = payload.data.project?.name || name;
        await refreshEnvironments();
      } catch (error) {
        target.textContent = String(error.message || error);
      }
    });

    $("#env-capture-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const target = $("#env-capture-error");
      target.textContent = "";
      const project = $("#env-project")?.value || "";
      const allocation = String(new FormData(form).get("allocation") || "");
      if (!project || !allocation) {
        target.textContent = "先选择项目和 active allocation";
        return;
      }
      try {
        const payload = await api("/api/environments", {
          method: "POST",
          body: { project, allocation },
        });
        if (!payload.ok) throw new Error(`${payload.error?.code}: ${payload.error?.message}`);
        const version = payload.data.version || {};
        flash(`已保存 ${version.ref} · fidelity ${version.capture_fidelity}`, "ok");
        await refreshVersions();
      } catch (error) {
        target.textContent = String(error.message || error);
      }
    });

    $("#legacy-claim-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const target = $("#legacy-claim-error");
      target.textContent = "";
      const data = new FormData(form);
      const source = String(data.get("source") || "");
      const project = String(data.get("project") || "");
      const reason = String(data.get("reason") || "").trim();
      if (!source || !project) {
        target.textContent = "先选择旧容器和项目";
        return;
      }
      try {
        const payload = await api("/api/legacy/claims", {
          method: "POST",
          body: { source, project, reason },
        });
        if (!payload.ok) throw new Error(`${payload.error?.code}: ${payload.error?.message}`);
        const claim = payload.data.claim || {};
        flash(`已提交 ${claim.claim_id || ""}，等待管理员审核`, "ok");
        form.reset();
        await refreshLegacy();
      } catch (error) {
        target.textContent = String(error.message || error);
      }
    });

    $("#admin-release-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const data = new FormData(form);
      const target = $("#admin-release-error");
      target.textContent = "";
      try {
        const payload = await api("/api/admin/release", {
          method: "POST",
          body: { id: String(data.get("id") || ""), reason: String(data.get("reason") || "") },
        });
        if (!payload.ok) throw new Error(`${payload.error?.code}: ${payload.error?.message}`);
        flash(`Force-released ${payload.data.id}`, "ok");
        form.reset();
        await refresh();
      } catch (error) {
        target.textContent = String(error.message || error);
      }
    });
  }

  function bindControls() {
    $$(".tab").forEach((tab) => tab.addEventListener("click", () => activateTab(tab.dataset.tab)));
    $("#refresh-my").addEventListener("click", refreshMy);
    $("#refresh-jobs").addEventListener("click", refreshJobs);
    $("#env-refresh").addEventListener("click", refreshVersions);
    $("#env-project").addEventListener("change", refreshVersions);
    $("#legacy-refresh").addEventListener("click", refreshLegacy);
    $("#legacy-admin-refresh").addEventListener("click", refreshLegacyAdmin);
    $("#legacy-scan").addEventListener("click", scanLegacySources);
    $("#mode-monitor").addEventListener("click", () => changeBrokerMode("monitor"));
    $("#mode-maintenance").addEventListener("click", () => changeBrokerMode("maintenance"));
    $("#mode-active").addEventListener("click", () => changeBrokerMode("active"));
    $("#admin-audit").addEventListener("click", refreshAdmin);
    $("#membership-refresh")?.addEventListener("click", refreshMembership);
    $("#admin-reconcile").addEventListener("click", async () => {
      try {
        const payload = await api("/api/admin/reconcile", { method: "POST" });
        if (!payload.ok) throw new Error(`${payload.error?.code}: ${payload.error?.message}`);
        flash(`Reconcile: ${JSON.stringify(payload.data)}`, "ok");
        await refresh();
      } catch (error) {
        flash(String(error.message || error), "error");
      }
    });
    $$("[data-close-modal]").forEach((button) => button.addEventListener("click", closeModal));
    $("#modal-backdrop").addEventListener("click", (event) => {
      if (event.target === $("#modal-backdrop")) closeModal();
    });
    window.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeModal();
    });
    window.addEventListener("resize", () => {
      if (state.terminal && !$("#terminal-modal").hidden) {
        state.terminal.fit.fit();
      }
    });
  }

  async function start() {
    bindControls();
    bindForms();
    try {
      await loadConfig();
    } catch (error) {
      setConnection(false);
      flash(String(error.message || error), "error");
    }
    await refresh();
    state.poll = window.setInterval(refresh, 5000);
    state.clock = window.setInterval(tick, 1000);
    tick();
  }

  start();
})();
