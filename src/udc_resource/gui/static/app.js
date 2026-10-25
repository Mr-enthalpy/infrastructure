(() => {
  "use strict";

  const tokenMeta = document.querySelector('meta[name="udc-token"]');
  const token = tokenMeta ? tokenMeta.content : "";
  const state = {
    config: null,
    overview: null,
    my: null,
    queue: [],
    jobs: [],
    jobsRefreshedAt: 0,
    adminEnabled: false,
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
    const init = { ...options, headers: { "X-UDC-Token": token, ...(options.headers || {}) } };
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
    if (name === "admin" && state.adminEnabled) refreshAdmin();
  }

  async function loadConfig() {
    const payload = await api("/api/config");
    if (!payload.ok) throw new Error(payload?.error?.message || "config failed");
    state.config = payload.data;
    $("#server-name").textContent = payload.data.server_name;
    if (payload.data.admin_enabled) {
      try {
        const admin = await api("/api/admin/users");
        if (admin.ok) {
          state.adminEnabled = true;
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
    const idle = gpus.filter((gpu) => gpu.state === "free");
    if (!idle.length) {
      container.innerHTML = '<span class="muted">没有空闲 GPU；留空将申请 CPU-only。</span>';
      return;
    }
    container.innerHTML = idle.map((gpu) => `
      <label class="gpu-choice">
        <input type="checkbox" name="gpu" value="${gpu.index}" ${state.selectedGpus.has(gpu.index) ? "checked" : ""}>
        GPU ${gpu.index}
      </label>
    `).join("");
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
    $("#logs-content").textContent = "";
    $("#history-list").textContent = "";
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
      const body = {
        gpus,
        cpus: Number(data.get("cpus")),
        memory_gb: Number(data.get("memory_gb")),
        hours: Number(data.get("hours")),
        queue: data.get("queue") === "on",
      };
      const image = String(data.get("image") || "").trim();
      const note = String(data.get("note") || "").trim();
      if (image) body.image = image;
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
        form.reset();
        activateTab("allocations");
        await refresh();
      } catch (error) {
        errorTarget.textContent = String(error.message || error);
      }
    });

    $("#job-start-form").addEventListener("submit", startJob);

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
    $("#mode-monitor").addEventListener("click", () => changeBrokerMode("monitor"));
    $("#mode-maintenance").addEventListener("click", () => changeBrokerMode("maintenance"));
    $("#mode-active").addEventListener("click", () => changeBrokerMode("active"));
    $("#admin-audit").addEventListener("click", refreshAdmin);
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
