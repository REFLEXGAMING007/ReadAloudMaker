/* ReadAloudMaker UI */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const RING = 2 * Math.PI * 52;

  const state = {
    env: null,
    engines: [],
    engine: "qwen",
    jobId: null,
    job: null,
    seq: 0,
    pollTimer: null,
    pollFailures: 0,
    running: false,
    scan: null,
    epub: "",
    startedAt: 0,
    duration: 0,
    logText: [],
    toastTimer: null,
    uploadedAnything: false,
  };

  const AUDIO_EXT = [".mp3", ".m4a", ".m4b", ".wav", ".ogg", ".flac", ".aac", ".opus"];

  // ---------------------------------------------------------------- helpers
  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[ch]);
  }

  function fmtSize(bytes) {
    if (!bytes && bytes !== 0) return "";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + " KB";
    if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + " MB";
    return (bytes / (1024 * 1024 * 1024)).toFixed(2) + " GB";
  }

  function fmtDuration(seconds) {
    seconds = Math.max(0, Math.round(seconds || 0));
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    if (h) return h + "h " + m + "m";
    if (m) return m + "m " + s + "s";
    return s + "s";
  }

  function fmtElapsed(seconds) {
    seconds = Math.max(0, Math.round(seconds || 0));
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return m + ":" + String(s).padStart(2, "0");
  }

  function baseName(path) {
    if (!path) return "";
    return String(path).split(/[\\/]/).pop();
  }

  function dirName(path) {
    if (!path) return "";
    const text = String(path).replace(/[\\/]+$/, "");
    const cut = Math.max(text.lastIndexOf("\\"), text.lastIndexOf("/"));
    return cut > 0 ? text.slice(0, cut) : text;
  }

  function joinOutput(folder) {
    if (!folder) return "output/";
    return folder.replace(/[\\/]+$/, "") + (folder.includes("\\") ? "\\output" : "/output");
  }

  function toast(message, kind) {
    const node = $("toast");
    node.textContent = message;
    node.className = "toast show" + (kind ? " " + kind : "");
    if (state.toastTimer) clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => { node.className = "toast"; }, 4600);
  }

  async function api(path, options) {
    const request = Object.assign({}, options || {});
    if (request.body && typeof request.body !== "string") {
      request.body = JSON.stringify(request.body);
    }
    if (!request.headers) request.headers = {};
    if (request.body && !request.headers["Content-Type"]) {
      request.headers["Content-Type"] = "application/json";
    }
    let response;
    try {
      response = await fetch(path, request);
    } catch (error) {
      throw new Error("Cannot reach the local server. Is it still running?");
    }
    let data = null;
    try { data = await response.json(); } catch (error) { data = null; }
    if (!response.ok) {
      const failure = new Error((data && data.error) || response.statusText || "Request failed");
      failure.hint = (data && data.hint) || "";
      throw failure;
    }
    return data;
  }

  function showError(error) {
    toast(error && error.hint ? error.message + " — " + error.hint : (error && error.message) || "Something went wrong.", "error");
  }

  // ------------------------------------------------------------- engine cards
  const ENGINE_ICONS = {
    qwen: '<svg viewBox="0 0 20 20" fill="none"><path d="M3 10v2.4M6 7.4v7.2M9 4.8v10.4M12 8.2v5.6M15 5.8v8.4M18 9v4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/><circle cx="12" cy="4.2" r="1.2" fill="currentColor"/></svg>',
    classic: '<svg viewBox="0 0 20 20" fill="none"><path d="M10 2.8 17 6.4l-7 3.6-7-3.6 7-3.6Z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><path d="m3 10 7 3.6 7-3.6M3 13.6l7 3.6 7-3.6" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  };

  function getEngine(key) {
    return state.engines.find((engine) => engine.key === key) || null;
  }

  function gpuList() {
    return (state.env && state.env.gpus) || [];
  }

  function shortGpuName(name) {
    return String(name || "GPU").replace(/^NVIDIA\s+(GeForce\s+)?/i, "").trim();
  }

  function recommendedGpu() {
    if (!state.env || state.env.recommendedGpu == null) return null;
    return gpuList().find((gpu) => gpu.index === state.env.recommendedGpu) || null;
  }

  function gpuSummary() {
    const gpus = gpuList();
    if (!gpus.length) return state.env && state.env.gpu ? state.env.gpu : "";
    const best = recommendedGpu() || gpus[0];
    const extra = gpus.length > 1 ? " (best of " + gpus.length + ")" : "";
    return "auto · " + shortGpuName(best.name) + extra;
  }

  function renderDevices() {
    const select = $("device");
    if (!select) return;
    const gpus = gpuList();
    const best = recommendedGpu();
    const current = select.value || "auto";
    const options = ['<option value="auto">auto · best GPU</option>'];
    gpus.forEach((gpu) => {
      // Tag the whole card class: `auto` may pick either of two identical cards.
      const tag = best && gpu.computeCap === best.computeCap &&
        gpu.totalMemory === best.totalMemory ? " · recommended" : "";
      const label = "cuda:" + gpu.index + " · " + shortGpuName(gpu.name) + " · " +
        fmtSize(gpu.totalMemory) + tag;
      options.push('<option value="cuda:' + gpu.index + '">' + esc(label) + "</option>");
    });
    options.push('<option value="cpu">cpu (slow)</option>');
    select.innerHTML = options.join("");
    const values = Array.from(select.options).map((option) => option.value);
    select.value = values.indexOf(current) === -1 ? "auto" : current;
    select.title = state.env && state.env.cudaVisible
      ? "CUDA_VISIBLE_DEVICES=" + state.env.cudaVisible
      : "";
  }

  function renderEngines() {
    const container = $("engines");
    if (!state.engines.length) {
      container.innerHTML = '<div class="engine-skeleton"></div><div class="engine-skeleton"></div>';
      return;
    }
    container.innerHTML = state.engines.map((engine) => {
      const available = !!engine.available;
      const badge = available
        ? '<span class="badge ok">ready</span>'
        : '<span class="badge missing">setup</span>';
      const note = (engine.notes || []).filter(Boolean).slice(0, 2).join(" · ");
      return '' +
        '<button type="button" class="engine-card' + (available ? "" : " unavailable") + '"' +
        ' data-key="' + esc(engine.key) + '" role="radio" aria-checked="false">' +
          '<span class="engine-icon">' + (ENGINE_ICONS[engine.key] || ENGINE_ICONS.classic) + "</span>" +
          '<span class="engine-copy">' +
            '<span class="engine-title">' + esc(engine.name) + badge + "</span>" +
            '<span class="engine-sub">' + esc(engine.subtitle || engine.description || "") + "</span>" +
            '<span class="engine-note">' + esc(note) + "</span>" +
          "</span>" +
          '<span class="engine-check" aria-hidden="true">' +
            '<svg viewBox="0 0 12 12" fill="none"><path d="m2 6.2 2.6 2.6L10 3.4" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>' +
          "</span>" +
        "</button>";
    }).join("");

    container.querySelectorAll(".engine-card").forEach((card) => {
      card.addEventListener("click", () => {
        const engine = getEngine(card.dataset.key);
        if (engine && !engine.available) {
          const missing = (engine.missing || []).join(", ") || "dependencies";
          toast(engine.name + " needs: " + missing, "error");
          $("helpDialog").showModal();
          return;
        }
        selectEngine(card.dataset.key);
      });
    });
  }

  function selectEngine(key) {
    const engine = getEngine(key);
    if (!engine) return;
    state.engine = key;
    document.querySelectorAll(".engine-card").forEach((card) => {
      const selected = card.dataset.key === key;
      card.classList.toggle("selected", selected);
      card.setAttribute("aria-checked", selected ? "true" : "false");
    });
    $("enginePill").textContent = engine.key === "qwen" ? "Qwen3" : "Classic";
    $("runEngine").textContent = engine.key === "qwen"
      ? "Qwen3 · word-level"
      : "aeneas + syncabook";
    $("advancedTag").textContent = engine.key === "qwen" ? "word level accuracy" : "classic";
    $("qwenOptions").hidden = engine.key !== "qwen";
    $("classicOptions").hidden = engine.key !== "classic";

    if (engine.key === "qwen") {
      const defaults = engine.defaults || {};
      if (!$("maxChunk").dataset.touched && defaults.max_chunk_seconds) {
        $("maxChunk").value = defaults.max_chunk_seconds;
      }
      if (!$("minCoverage").dataset.touched && defaults.min_coverage) {
        $("minCoverage").value = Math.round(defaults.min_coverage * 100);
      }
    }
    updateRangeLabels();

    if (engine.available) {
      const gpu = engine.key === "qwen" ? gpuSummary() : "";
      $("engineHint").textContent = engine.pythonLabel + (gpu ? " · " + gpu : "");
    } else {
      $("engineHint").textContent = "Missing: " + ((engine.missing || []).join(", ") || "dependencies") +
        " — open “How it works” for setup steps.";
    }

    $("engineHint").classList.toggle("warn-text", !engine.available);
    $("runBtn").disabled = state.running || !engine.available;
    $("runBtn").querySelector(".btn-label").textContent = engine.available ? "Build EPUB" : "Engine unavailable";

    localStorage.setItem("readaloud.engine", key);
    if (state.scan && $("folder").value.trim()) scan(false);
  }

  // -------------------------------------------------------------------- boot
  async function loadEnvironment() {
    try {
      state.env = await api("/api/env");
      state.engines = state.env.engines || [];
      $("hostPill").innerHTML = '<i></i> local · ' + (state.env.port || 9459);
      renderDevices();
      renderEngines();

      const preferred = localStorage.getItem("readaloud.engine") || "qwen";
      let chosen = getEngine(preferred);
      if (!chosen || !chosen.available) {
        chosen = state.engines.find((engine) => engine.available) || chosen || state.engines[0];
      }
      if (chosen) selectEngine(chosen.key);

      if (state.env.gpus && state.env.gpus.length) {
        $("footEngine").textContent = gpuSummary() + " · classic available for compatibility";
      } else if (state.env.gpu) {
        $("footEngine").textContent = state.env.gpu + " · classic available for compatibility";
      } else if (state.engines.some((engine) => engine.key === "qwen" && engine.available)) {
        $("footEngine").textContent = "Qwen3 ready · classic available for compatibility";
      } else {
        $("footEngine").textContent = "Install the Qwen3 environment for best accuracy";
      }
    } catch (error) {
      showError(error);
      $("engineHint").textContent = "Could not detect the build engines.";
      $("runBtn").disabled = true;
    }
  }

  function restorePrefs() {
    const folder = localStorage.getItem("readaloud.folder");
    if (folder) $("folder").value = folder;
    const out = localStorage.getItem("readaloud.out");
    if (out) $("out").value = out;
    const device = localStorage.getItem("readaloud.device");
    if (device && Array.from($("device").options).some((option) => option.value === device)) {
      $("device").value = device;
    }
    const noise = localStorage.getItem("readaloud.noise");
    if (noise === "1") {
      $("showNoise").checked = true;
      $("log").classList.remove("hide-noise");
    }
    updateOutputPreview();
    if (folder) scan(false);
  }

  function savePrefs() {
    localStorage.setItem("readaloud.folder", $("folder").value.trim());
    localStorage.setItem("readaloud.out", $("out").value.trim());
    localStorage.setItem("readaloud.engine", state.engine);
    localStorage.setItem("readaloud.device", $("device").value);
    localStorage.setItem("readaloud.noise", $("showNoise").checked ? "1" : "0");
  }

  // -------------------------------------------------------------------- scan
  async function scan(verbose) {
    const folder = $("folder").value.trim();
    if (!folder) {
      if (verbose) toast("Choose the folder that holds the EPUB and audio.", "error");
      return;
    }
    $("scanBtn").disabled = true;
    $("scanBtn").textContent = "Scanning…";
    try {
      const data = await api("/api/scan", {
        method: "POST",
        body: {
          folder: folder,
          engine: state.engine,
          exclude: $("exclude").value.trim(),
        },
      });
      state.scan = data;
      state.epub = data.epub || "";
      renderScan(data);
      if (verbose && data.ready) toast("Looks good — " + data.chapters + " chapters matched.", "success");
    } catch (error) {
      state.scan = null;
      $("scanCard").hidden = true;
      if (verbose) showError(error);
    } finally {
      $("scanBtn").disabled = false;
      $("scanBtn").textContent = "Scan";
    }
  }

  function renderScan(data) {
    const card = $("scanCard");
    card.hidden = false;
    const warnings = data.warnings || [];
    card.classList.toggle("warn", warnings.length > 0 || !data.ready);
    $("scanBadge").textContent = data.ready ? "Ready" : "Check files";
    $("scanTitle").textContent = data.title || data.epub || baseName(data.folder) || "Book";
    $("scanSub").textContent = data.language ? "· " + data.language : "";

    const chips = [];
    if (data.chapters != null) chips.push(chip(data.chapters, "chapters"));
    if (data.fragments != null) chips.push(chip(data.fragments, "sentences"));
    chips.push(chip((data.audio || []).length, "audio files"));
    const stats = $("scanStats");
    stats.innerHTML = chips.join("");

    const warningBox = $("scanWarnings");
    if (warnings.length) {
      warningBox.hidden = false;
      warningBox.innerHTML = warnings.map((text) => '' +
        '<div class="scan-warning">' +
          '<svg viewBox="0 0 16 16" fill="none"><path d="M8 2.5 14.2 13H1.8L8 2.5Z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/><path d="M8 6v3.2" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/><circle cx="8" cy="11.2" r=".8" fill="currentColor"/></svg>' +
          "<span>" + esc(text) + "</span>" +
        "</div>").join("");
    } else {
      warningBox.hidden = true;
      warningBox.innerHTML = "";
    }

    const epubs = data.epubs || [];
    const row = $("epubRow");
    const select = $("epubSelect");
    if (epubs.length > 1) {
      row.hidden = false;
      select.innerHTML = epubs.map((name) =>
        '<option value="' + esc(name) + '"' + (name === data.epub ? " selected" : "") + ">" +
        esc(name) + "</option>").join("");
      state.epub = select.value;
    } else {
      row.hidden = true;
      state.epub = data.epub || "";
    }

    if (data.output) {
      $("runOut").textContent = data.output;
      if (!$("out").value.trim()) $("out").placeholder = data.output;
    }
    updateOutputPreview();
  }

  function chip(value, label) {
    return '<span class="stat-chip"><b>' + esc(value) + "</b>" + esc(label) + "</span>";
  }

  // ------------------------------------------------------------------- build
  function buildPayload() {
    return {
      engine: state.engine,
      folder: $("folder").value.trim(),
      out: $("out").value.trim(),
      epub: state.epub || "",
      language: $("language").value.trim(),
      exclude: $("exclude").value.trim(),
      keep_work: $("keepWork").checked,
      title: $("title").value.trim(),
      author: $("author").value.trim(),
      prepare_only: $("prepareOnly").checked,
      max_chunk_seconds: Number($("maxChunk").value),
      min_coverage: Number($("minCoverage").value) / 100,
      device: $("device").value,
      dtype: $("dtype").value,
      model: $("model").value.trim(),
      dry_run: $("dryRun").checked,
      fake_aligner: $("fakeAligner").checked,
      compile_model: $("compileModel").checked,
      epubcheck: $("epubcheck").checked,
    };
  }

  async function startBuild(event) {
    event.preventDefault();
    if (state.running) return;
    const engine = getEngine(state.engine);
    if (!engine || !engine.available) {
      toast("That engine is not installed yet. Open “How it works” for setup.", "error");
      return;
    }
    const folder = $("folder").value.trim();
    if (!folder) {
      toast("Choose the source folder first.", "error");
      $("folder").focus();
      return;
    }

    savePrefs();
    resetProgress();
    state.running = true;
    state.jobId = null;
    state.job = null;
    state.seq = 0;
    state.logText = [];
    state.startedAt = Date.now();
    state.duration = 0;
    setStatus("running");
    $("cancelBtn").hidden = false;
    $("cancelBtn").disabled = false;
    $("runBtn").disabled = true;
    $("runBtn").querySelector(".btn-label").textContent = "Starting…";
    $("stageLabel").textContent = "Starting the build";
    $("stageDetail").textContent = "Loading " + engine.name + "…";

    try {
      const summary = await api("/api/jobs", { method: "POST", body: buildPayload() });
      state.jobId = summary.id;
      $("runBtn").querySelector(".btn-label").textContent = "Building…";
      poll();
    } catch (error) {
      state.running = false;
      $("cancelBtn").hidden = true;
      $("runBtn").querySelector(".btn-label").textContent = "Build EPUB";
      setStatus("idle");
      showError(error);
      appendLogLines([{ time: 0, level: "error", text: error.message }]);
    }
  }

  function resetProgress() {
    state.logText = [];
    $("log").innerHTML = '<div class="log-empty" id="logEmpty"><span class="pulse-dot"></span>The build log appears here.</div>';
    $("result").hidden = true;
    $("result").className = "result";
    $("ring").className = "ring";
    $("ringFill").style.stroke = "";
    $("ringFill").style.strokeDashoffset = String(RING);
    $("percent").textContent = "0%";
    $("barFill").style.width = "0%";
    $("bar").setAttribute("aria-valuenow", "0");
    $("steps").querySelectorAll("li").forEach((item) => {
      item.classList.remove("active", "done", "failed");
    });
    $("stageLabel").textContent = "Preparing";
    $("stageDetail").textContent = "Starting…";
    $("elapsed").textContent = "0:00";
  }

  async function poll() {
    if (!state.jobId) return;
    try {
      const data = await api("/api/jobs/" + state.jobId + "?since=" + state.seq);
      state.job = data;
      state.pollFailures = 0;
      applyJob(data);
      if (["succeeded", "failed", "cancelled"].indexOf(data.status) !== -1) {
        finish(data);
        return;
      }
      state.pollTimer = setTimeout(poll, 420);
    } catch (error) {
      state.pollFailures += 1;
      if (state.pollFailures > 10) {
        state.running = false;
        $("cancelBtn").hidden = true;
        setStatus("failed");
        showError(error);
        return;
      }
      state.pollTimer = setTimeout(poll, 900);
    }
  }

  function applyJob(job) {
    state.duration = job.duration || (Date.now() - state.startedAt) / 1000;
    setStatus(job.status);
    appendLogLines(job.lines || []);
    state.seq = job.next || state.seq;

    const progress = Math.max(0, Math.min(1, job.progress || 0));
    $("percent").textContent = Math.round(progress * 100) + "%";
    $("ringFill").style.strokeDashoffset = String(RING * (1 - progress));
    $("barFill").style.width = (progress * 100).toFixed(1) + "%";
    $("bar").setAttribute("aria-valuenow", String(Math.round(progress * 100)));
    $("stageLabel").textContent = job.stageLabel || "Working";
    $("stageDetail").textContent = job.stageDetail || job.stageLabel || "";
    $("elapsed").textContent = fmtElapsed(state.duration);
    updateSteps(job.stage, job.status);
  }

  function setStatus(status) {
    const pill = $("statusPill");
    const labels = {
      idle: "Idle", queued: "Queued", running: "Running", cancelling: "Stopping",
      succeeded: "Complete", failed: "Failed", cancelled: "Cancelled",
    };
    pill.className = "status-pill " + status;
    pill.textContent = labels[status] || status;
    $("ring").classList.toggle("running", status === "running" || status === "cancelling");
    $("ring").classList.toggle("failed", status === "failed");
    $("ring").classList.toggle("succeeded", status === "succeeded");
    if (status === "failed") $("ringFill").style.stroke = "var(--red)";
    else if (status === "succeeded") $("ringFill").style.stroke = "var(--green)";
    else $("ringFill").style.stroke = "";
  }

  function updateSteps(stage, status) {
    const order = ["prepare", "align", "package", "check"];
    let index = order.indexOf(stage);
    if (stage === "done") index = order.length;
    if (index < 0) index = 0;
    const items = Array.from($("steps").querySelectorAll("li"));
    items.forEach((item, position) => {
      item.classList.remove("active", "done", "failed");
      if (status === "succeeded") {
        item.classList.add("done");
      } else if (status === "failed" || status === "cancelled") {
        if (position < index) item.classList.add("done");
        else if (position === index) item.classList.add("failed");
      } else if (position < index) {
        item.classList.add("done");
      } else if (position === index) {
        item.classList.add("active");
      }
    });
  }

  // --------------------------------------------------------------------- log
  function appendLogLines(lines) {
    if (!lines.length) return;
    const box = $("log");
    const empty = $("logEmpty");
    if (empty) empty.remove();

    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 90;
    const fragment = document.createDocumentFragment();
    lines.forEach((line) => {
      state.logText.push(line.text || "");
      const row = document.createElement("div");
      row.className = "line level-" + (line.level || "info");
      const time = document.createElement("span");
      time.className = "t";
      time.textContent = fmtElapsed(line.time || 0);
      const message = document.createElement("span");
      message.className = "msg";
      message.textContent = line.text == null ? "" : line.text;
      row.appendChild(time);
      row.appendChild(message);
      fragment.appendChild(row);
    });
    box.appendChild(fragment);
    if (state.logText.length > 5000) {
      state.logText = state.logText.slice(-5000);
    }

    while (box.childElementCount > 2200) {
      box.removeChild(box.firstElementChild);
    }
    if (nearBottom) box.scrollTop = box.scrollHeight;
  }

  function copyLog() {
    const text = state.logText.join("\n");
    if (!text) return toast("The log is empty.", "error");
    const done = () => toast("Log copied to the clipboard.", "success");
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
    } else {
      fallbackCopy(text, done);
    }
  }

  function fallbackCopy(text, done) {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    try { document.execCommand("copy"); done(); }
    catch (error) { toast("Could not copy the log.", "error"); }
    area.remove();
  }

  // ------------------------------------------------------------------ finish
  function finish(job) {
    state.running = false;
    state.duration = job.duration || (Date.now() - state.startedAt) / 1000;
    $("cancelBtn").hidden = true;
    $("runBtn").disabled = !(getEngine(state.engine) || {}).available;
    $("runBtn").querySelector(".btn-label").textContent = "Build EPUB";
    $("elapsed").textContent = fmtElapsed(state.duration);
    setStatus(job.status);
    updateSteps(job.stage, job.status);

    const result = $("result");
    result.hidden = false;
    result.className = "result" + (job.status === "failed" ? " failed" :
      job.status === "cancelled" ? " cancelled" : "");

    const icon = $("resultIcon");
    const title = $("resultTitle");
    const sub = $("resultSub");
    const stats = $("resultStats");
    const download = $("downloadBtn");
    const reveal = $("revealBtn");
    stats.innerHTML = "";
    download.hidden = true;
    reveal.hidden = false;
    download.removeAttribute("href");

    if (job.status === "succeeded") {
      const output = (job.outputs || [])[0];
      icon.innerHTML = '<svg viewBox="0 0 20 20" fill="none"><path d="m4 10.4 3.8 3.8L16 5.4" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>';
      if (output) {
        title.textContent = "EPUB ready";
        sub.textContent = output.name + " · " + fmtSize(output.size);
        download.hidden = false;
        download.href = "/api/jobs/" + job.id + "/download/0";
        state.revealPath = dirName(output.path);
        download.setAttribute("download", output.name);
      } else {
        title.textContent = "Plan ready";
        sub.textContent = "The dry run finished. No EPUB was written.";
        state.revealPath = job.folder;
      }
      if (job.report) {
        const report = job.report;
        const chapters = (report.chapters || []).length;
        const pars = report.validation && report.validation.pars;
        const duration = report.total_duration;
        if (chapters) stats.appendChild(statNode(chapters, "chapters"));
        if (pars) stats.appendChild(statNode(pars, "sentences"));
        if (duration) stats.appendChild(statNode(fmtDuration(duration), "audio"));
        const epubcheck = report.epubcheck;
        if (epubcheck && epubcheck !== "not run") stats.appendChild(statNode(epubcheck, "EPUBCheck"));
      }
      toast("Build complete — the EPUB is ready.", "success");
    } else if (job.status === "cancelled") {
      icon.innerHTML = '<svg viewBox="0 0 20 20" fill="none"><rect x="5" y="5" width="10" height="10" rx="2" stroke="currentColor" stroke-width="1.6"/></svg>';
      title.textContent = "Build cancelled";
      sub.textContent = "The partial work folder was kept for inspection.";
      state.revealPath = job.folder;
    } else {
      icon.innerHTML = '<svg viewBox="0 0 20 20" fill="none"><path d="M10 3.2 17.6 16H2.4L10 3.2Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M10 8v3.4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><circle cx="10" cy="13.6" r=".9" fill="currentColor"/></svg>';
      title.textContent = "Build failed";
      sub.textContent = job.error || "Read the log above for details.";
      state.revealPath = job.folder;
      toast("The build failed. Check the live log.", "error");
    }
  }

  function statNode(value, label) {
    const node = document.createElement("span");
    node.className = "stat-chip";
    const strong = document.createElement("b");
    strong.textContent = value;
    node.appendChild(strong);
    node.appendChild(document.createTextNode(label));
    return node;
  }

  async function cancelBuild() {
    if (!state.jobId || !state.running) return;
    $("cancelBtn").disabled = true;
    try {
      await api("/api/jobs/" + state.jobId + "/cancel", { method: "POST", body: {} });
      toast("Stopping the build…");
    } catch (error) {
      showError(error);
      $("cancelBtn").disabled = false;
    }
  }

  async function reveal(path) {
    if (!path) return;
    try {
      await api("/api/reveal", { method: "POST", body: { path: path } });
    } catch (error) {
      showError(error);
    }
  }

  // ----------------------------------------------------------------- uploads
  function handleFiles(fileList) {
    const files = Array.from(fileList || []).filter((file) => {
      const name = file.name.toLowerCase();
      return name.endsWith(".epub") || AUDIO_EXT.some((extension) => name.endsWith(extension));
    });
    if (!files.length) {
      toast("Drop an EPUB together with the chapter audio files.", "error");
      return;
    }
    uploadFiles(files);
  }

  async function uploadFiles(files) {
    const status = $("uploadStatus");
    const label = $("uploadText");
    status.hidden = false;
    $("browseBtn").disabled = true;
    try {
      await api("/api/upload/reset", { method: "POST", body: {} });
      let folder = "";
      for (let index = 0; index < files.length; index += 1) {
        label.textContent = "Uploading " + (index + 1) + " of " + files.length + " · " + files[index].name;
        let response;
        try {
          response = await fetch("/api/upload?name=" + encodeURIComponent(files[index].name), {
            method: "POST",
            body: files[index],
          });
        } catch (error) {
          throw new Error("The upload was interrupted.");
        }
        let data = null;
        try { data = await response.json(); } catch (error) { data = null; }
        if (!response.ok) throw new Error((data && data.error) || "Upload failed");
        folder = data.folder;
      }
      $("folder").value = folder;
      $("out").value = "";
      savePrefs();
      updateOutputPreview();
      label.textContent = "Uploaded " + files.length + " file(s). Scanning…";
      await scan(true);
      toast("Files uploaded to a temporary folder.", "success");
    } catch (error) {
      showError(error);
    } finally {
      status.hidden = true;
      $("browseBtn").disabled = false;
    }
  }

  // ------------------------------------------------------------------- misc
  function updateOutputPreview() {
    const out = $("out").value.trim();
    $("runOut").textContent = out || joinOutput($("folder").value.trim());
  }

  function updateRangeLabels() {
    $("maxChunkValue").textContent = $("maxChunk").value + " s";
    $("minCoverageValue").textContent = $("minCoverage").value + "%";
  }

  function updateElapsed() {
    if (!state.running) return;
    $("elapsed").textContent = fmtElapsed((Date.now() - state.startedAt) / 1000);
  }

  // ------------------------------------------------------------------- bind
  function bind() {
    $("buildForm").addEventListener("submit", startBuild);
    $("scanBtn").addEventListener("click", () => scan(true));
    $("rescanBtn").addEventListener("click", () => scan(true));
    $("cancelBtn").addEventListener("click", cancelBuild);
    $("copyLog").addEventListener("click", copyLog);

    $("folder").addEventListener("input", () => {
      state.scan = null;
      $("scanCard").hidden = true;
      updateOutputPreview();
      savePrefs();
    });
    $("folder").addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); scan(true); }
    });
    $("out").addEventListener("input", () => { updateOutputPreview(); savePrefs(); });
    $("device").addEventListener("change", () => savePrefs());
    $("exclude").addEventListener("change", () => scan(false));
    $("epubSelect").addEventListener("change", (event) => { state.epub = event.target.value; });

    $("maxChunk").addEventListener("input", () => { $("maxChunk").dataset.touched = "1"; updateRangeLabels(); });
    $("minCoverage").addEventListener("input", () => { $("minCoverage").dataset.touched = "1"; updateRangeLabels(); });

    $("showNoise").addEventListener("change", (event) => {
      $("log").classList.toggle("hide-noise", !event.target.checked);
      localStorage.setItem("readaloud.noise", event.target.checked ? "1" : "0");
    });

    $("helpBtn").addEventListener("click", () => $("helpDialog").showModal());
    $("helpDialog").addEventListener("click", (event) => {
      const rect = $("helpDialog").getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right ||
          event.clientY < rect.top || event.clientY > rect.bottom) {
        $("helpDialog").close();
      }
    });

    // drag and drop
    const zone = $("dropzone");
    ["dragenter", "dragover"].forEach((name) => {
      zone.addEventListener(name, (event) => {
        event.preventDefault();
        zone.classList.add("dragging");
      });
    });
    ["dragleave", "dragend"].forEach((name) => {
      zone.addEventListener(name, () => zone.classList.remove("dragging"));
    });
    zone.addEventListener("drop", (event) => {
      event.preventDefault();
      zone.classList.remove("dragging");
      handleFiles(event.dataTransfer.files);
    });
    zone.addEventListener("click", (event) => {
      if (event.target.closest("button")) return;
      $("fileInput").click();
    });
    zone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        $("fileInput").click();
      }
    });
    $("browseBtn").addEventListener("click", (event) => {
      event.stopPropagation();
      $("fileInput").click();
    });
    $("fileInput").addEventListener("change", (event) => {
      handleFiles(event.target.files);
      event.target.value = "";
    });
    window.addEventListener("dragover", (event) => event.preventDefault());
    window.addEventListener("drop", (event) => {
      if (!$("dropzone").contains(event.target)) event.preventDefault();
    });

    $("revealBtn").addEventListener("click", () => reveal(state.revealPath));

    document.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        $("buildForm").requestSubmit();
      }
    });

    setInterval(updateElapsed, 500);
  }

  document.addEventListener("DOMContentLoaded", function () {
    bind();
    updateRangeLabels();
    loadEnvironment().then(restorePrefs);
  });
})();
