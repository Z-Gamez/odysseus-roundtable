/* SAW Round Table — self-contained overlay UI.
 *
 * Additive: attaches its own handler to the #rail-roundtable button and renders
 * everything inside one injected overlay, so it doesn't touch Odysseus's view
 * framework. Talks to /api/roundtable/* and parses the orchestrator's tagged SSE
 * stream (run_start / role_start / delta / tool / role_done / gate / run_done).
 */
(function () {
  "use strict";

  var ACCENT = "#7F77DD";           // SAW violet
  var OK = "#1D9E75", BAD = "#D85A30", WARN = "#C9A227";

  var ROLE_LABELS = {
    bsa: "BSA · Analyst", architect: "Architect", developer: "Developer",
    qas: "QAS · Reviewer", security: "Security", tech_writer: "Tech Writer",
    rte: "RTE · Release"
  };
  var DEFAULT_WORKSPACE = "C\\:\\Odysseus\\saw-sandbox".replace(/\\/g, "\\"); // display only

  var els = null;       // overlay element refs once built
  var current = null;   // { runId, blocks: {roleKey_iter: bodyEl}, chips: {role: el} }

  function h(html) { var t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstChild; }
  function esc(s) { return (s == null ? "" : String(s)).replace(/[&<>]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]; }); }

  // ---- styles --------------------------------------------------------------
  function injectStyles() {
    if (document.getElementById("rt-styles")) return;
    var css = `
    #rt-overlay{position:fixed;inset:0;z-index:9000;display:none;background:rgba(0,0,0,.45);
      backdrop-filter:blur(2px);}
    #rt-overlay.rt-open{display:flex;}
    #rt-panel{margin:auto;width:min(1180px,96vw);height:min(880px,94vh);display:flex;flex-direction:column;
      background:var(--bg,#16161a);color:inherit;border:1px solid rgba(127,127,127,.28);border-radius:14px;
      box-shadow:0 24px 80px rgba(0,0,0,.5);overflow:hidden;}
    #rt-head{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid rgba(127,127,127,.2);}
    #rt-head .rt-title{font-weight:700;font-size:15px;letter-spacing:.2px;}
    #rt-head .rt-title b{color:${ACCENT};}
    #rt-head .rt-spacer{flex:1;}
    #rt-x{cursor:pointer;border:none;background:transparent;color:inherit;font-size:20px;opacity:.7;padding:2px 8px;border-radius:8px;}
    #rt-x:hover{opacity:1;background:rgba(127,127,127,.15);}
    #rt-body{flex:1;display:flex;min-height:0;}
    #rt-left{width:340px;min-width:300px;border-right:1px solid rgba(127,127,127,.2);display:flex;flex-direction:column;padding:14px;gap:10px;overflow:auto;}
    #rt-main{flex:1;display:flex;flex-direction:column;min-width:0;}
    .rt-field label{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.6px;opacity:.65;margin:0 0 4px;}
    .rt-field input,.rt-field textarea{width:100%;box-sizing:border-box;background:var(--input-bg,rgba(127,127,127,.08));
      color:inherit;border:1px solid var(--input-border,rgba(127,127,127,.3));border-radius:9px;padding:8px 10px;font:inherit;font-size:13px;}
    .rt-field textarea{resize:vertical;min-height:54px;}
    #rt-run{margin-top:2px;width:100%;cursor:pointer;border:none;border-radius:10px;padding:11px;font:inherit;font-weight:700;
      color:#fff;background:${ACCENT};}
    #rt-run:disabled{opacity:.5;cursor:default;}
    .rt-recent{margin-top:8px;}
    .rt-recent h4{margin:6px 0;font-size:11px;text-transform:uppercase;letter-spacing:.6px;opacity:.6;}
    .rt-recent .rt-run-item{padding:7px 9px;border:1px solid rgba(127,127,127,.18);border-radius:8px;margin-bottom:5px;cursor:pointer;font-size:12px;display:flex;gap:8px;align-items:center;}
    .rt-recent .rt-run-item:hover{border-color:${ACCENT};}
    .rt-dot{width:8px;height:8px;border-radius:50%;flex:0 0 auto;background:rgba(127,127,127,.5);}
    #rt-pipeline{display:flex;align-items:center;gap:6px;flex-wrap:wrap;padding:12px 16px;border-bottom:1px solid rgba(127,127,127,.2);}
    .rt-chip{display:flex;align-items:center;gap:7px;padding:7px 11px;border:1px solid rgba(127,127,127,.3);border-radius:999px;font-size:12px;font-weight:600;opacity:.55;transition:.2s;}
    .rt-chip.run{opacity:1;border-color:${ACCENT};box-shadow:0 0 0 2px rgba(127,119,221,.18);}
    .rt-chip.done{opacity:1;}
    .rt-chip .rt-cmodel{font-weight:400;opacity:.6;font-size:10.5px;}
    .rt-arrow{opacity:.4;}
    .rt-gatebadge{font-size:14px;}
    #rt-status{padding:8px 16px;font-size:12px;border-bottom:1px solid rgba(127,127,127,.2);opacity:.85;}
    #rt-log{flex:1;overflow:auto;padding:14px 16px;}
    .rt-role-block{margin-bottom:14px;border:1px solid rgba(127,127,127,.18);border-radius:10px;overflow:hidden;}
    .rt-role-head{padding:7px 12px;font-weight:700;font-size:12px;background:rgba(127,119,221,.10);border-bottom:1px solid rgba(127,127,127,.15);display:flex;gap:8px;align-items:center;}
    .rt-role-head .rt-iter{font-weight:400;opacity:.6;font-size:11px;}
    .rt-role-body{padding:10px 12px;white-space:pre-wrap;word-break:break-word;font-size:13px;line-height:1.5;}
    .rt-tool{font-size:11px;opacity:.6;font-style:italic;margin:3px 0;}
    .rt-gate{margin:10px 0;padding:9px 12px;border-radius:9px;font-size:12.5px;font-weight:600;border:1px solid;}
    .rt-gate.pass{color:${OK};border-color:${OK};background:rgba(29,158,117,.08);}
    .rt-gate.fail{color:${BAD};border-color:${BAD};background:rgba(216,90,48,.08);}
    .rt-gate.halt{color:${WARN};border-color:${WARN};background:rgba(201,162,39,.10);}
    .rt-final{margin:8px 0 0;padding:11px 13px;border-radius:10px;font-weight:700;text-align:center;}
    #rt-cfg{cursor:pointer;border:1px solid rgba(127,127,127,.3);background:transparent;color:inherit;font:inherit;font-size:12px;padding:4px 10px;border-radius:8px;opacity:.85;}
    #rt-cfg:hover{opacity:1;border-color:${ACCENT};}
    #rt-config{flex:1;min-height:0;overflow:auto;padding:16px 20px;display:none;flex-direction:column;}
    #rt-config.open{display:flex;}
    #rt-config h3{margin:0;font-size:15px;}
    .rt-cfg-sub{opacity:.6;font-size:12px;margin:4px 0 14px;}
    .rt-cfg-row{display:flex;align-items:center;gap:12px;padding:9px 0;border-bottom:1px solid rgba(127,127,127,.15);}
    .rt-cfg-row .rt-cfg-role{flex:0 0 150px;font-weight:600;font-size:13px;}
    .rt-cfg-row select{flex:1;min-width:0;background:var(--input-bg,rgba(127,127,127,.08));color:inherit;border:1px solid var(--input-border,rgba(127,127,127,.3));border-radius:8px;padding:7px 9px;font:inherit;font-size:12.5px;}
    #rt-cfg-done{cursor:pointer;border:none;border-radius:8px;padding:6px 14px;font:inherit;font-weight:600;color:#fff;background:${ACCENT};}
    .rt-pr{margin:12px 0;border:1px solid ${ACCENT};border-radius:10px;overflow:hidden;}
    .rt-pr-head{padding:8px 12px;font-weight:700;font-size:13px;background:rgba(127,119,221,.12);}
    .rt-pr-mode{font-weight:400;font-size:11px;opacity:.6;border:1px solid rgba(127,127,127,.3);border-radius:6px;padding:1px 6px;margin-left:6px;}
    .rt-pr-row{padding:5px 12px;font-size:12px;}
    .rt-pr-row code{background:rgba(127,127,127,.12);padding:1px 5px;border-radius:4px;}
    .rt-pr-title{padding:6px 12px;font-weight:700;font-size:13px;}
    .rt-pr-body{padding:4px 12px 10px;white-space:pre-wrap;font-size:12.5px;line-height:1.5;opacity:.9;}
    .rt-pr-stat{margin:0 12px 12px;padding:8px 10px;background:rgba(127,127,127,.08);border-radius:8px;font-size:11px;overflow:auto;white-space:pre;}
    .rt-pr-row a{color:var(--color-text-info,#6aa9d9);word-break:break-all;}
    .rt-pr-actions{display:flex;align-items:center;gap:8px;padding:8px 12px 12px;flex-wrap:wrap;}
    .rt-pr-approve{cursor:pointer;border:none;border-radius:8px;padding:7px 13px;font:inherit;font-weight:700;color:#fff;background:${OK};}
    .rt-pr-reject{cursor:pointer;border:1px solid rgba(127,127,127,.35);border-radius:8px;padding:7px 13px;font:inherit;background:transparent;color:inherit;}
    .rt-pr-approve:disabled,.rt-pr-reject:disabled{opacity:.5;cursor:default;}
    .rt-pr-result{font-size:12px;}
    `;
    var s = document.createElement("style"); s.id = "rt-styles"; s.textContent = css; document.head.appendChild(s);
  }

  // ---- overlay construction -----------------------------------------------
  function build() {
    injectStyles();
    var ov = h(`<div id="rt-overlay" role="dialog" aria-label="Round Table">
      <div id="rt-panel">
        <div id="rt-head">
          <span class="rt-title">⊹ <b>Round&nbsp;Table</b> — SAFe mission control</span>
          <span class="rt-spacer"></span>
          <button id="rt-cfg" title="Per-role models">⚙ Models</button>
          <button id="rt-x" title="Close" aria-label="Close">×</button>
        </div>
        <div id="rt-body">
          <div id="rt-left">
            <div class="rt-field"><label>Ticket title</label><input id="rt-title" placeholder="e.g. Add a hello() function with a test"></div>
            <div class="rt-field"><label>Description</label><textarea id="rt-desc" placeholder="What needs doing and why"></textarea></div>
            <div class="rt-field"><label>Acceptance criteria (optional — BSA defines if blank)</label><textarea id="rt-ac" placeholder="- [ ] ..."></textarea></div>
            <div class="rt-field"><label>Workspace</label><input id="rt-ws" value="C:\\Odysseus\\saw-sandbox"></div>
            <button id="rt-run">▶ Run pipeline</button>
            <div class="rt-recent"><h4>Recent runs</h4><div id="rt-recent-list"></div></div>
          </div>
          <div id="rt-main">
            <div id="rt-pipeline"></div>
            <div id="rt-status">Idle. Fill in a ticket and hit Run.</div>
            <div id="rt-log"></div>
          </div>
        </div>
        <div id="rt-config">
          <div style="display:flex;align-items:center;gap:10px;">
            <h3>Per-role models</h3><span style="flex:1"></span>
            <button id="rt-cfg-done">Done</button>
          </div>
          <div class="rt-cfg-sub">Route each role to a local or API model. "Default" uses its tier (saw_heavy = Claude / saw_cheap = local).</div>
          <div id="rt-cfg-list"></div>
        </div>
      </div></div>`);
    document.body.appendChild(ov);
    els = {
      overlay: ov,
      title: ov.querySelector("#rt-title"), desc: ov.querySelector("#rt-desc"),
      ac: ov.querySelector("#rt-ac"), ws: ov.querySelector("#rt-ws"),
      run: ov.querySelector("#rt-run"), pipeline: ov.querySelector("#rt-pipeline"),
      status: ov.querySelector("#rt-status"), log: ov.querySelector("#rt-log"),
      recent: ov.querySelector("#rt-recent-list"),
      body: ov.querySelector("#rt-body"), config: ov.querySelector("#rt-config"),
      cfgList: ov.querySelector("#rt-cfg-list"),
    };
    ov.querySelector("#rt-x").addEventListener("click", close);
    ov.addEventListener("click", function (e) { if (e.target === ov) close(); });
    ov.querySelector("#rt-cfg").addEventListener("click", toggleConfig);
    ov.querySelector("#rt-cfg-done").addEventListener("click", closeConfig);
    els.run.addEventListener("click", function () {
      if (els.run.dataset.mode === "stop") stopRun(); else startRun();
    });
    return els;
  }

  function open() {
    if (!els) build();
    els.overlay.classList.add("rt-open");
    loadRecent();
  }
  function close() { if (els) els.overlay.classList.remove("rt-open"); }

  // ---- run lifecycle -------------------------------------------------------
  function setStatus(txt, color) { els.status.textContent = txt; els.status.style.color = color || ""; }

  function renderPipeline(pipeline) {
    els.pipeline.innerHTML = "";
    current.chips = {};
    pipeline.forEach(function (role, i) {
      if (i > 0) els.pipeline.appendChild(h(`<span class="rt-arrow">→</span>`));
      var chip = h(`<span class="rt-chip" data-role="${role}"><span>${esc(ROLE_LABELS[role] || role)}</span><span class="rt-cmodel"></span></span>`);
      els.pipeline.appendChild(chip);
      current.chips[role] = chip;
    });
  }

  function chipState(role, state, model) {
    var c = current.chips[role]; if (!c) return;
    c.classList.remove("run", "done"); if (state) c.classList.add(state);
    if (model) c.querySelector(".rt-cmodel").textContent = model;
  }

  function roleBlock(role, iter, model) {
    var key = role + "_" + iter;
    if (current.blocks[key]) return current.blocks[key];
    var label = (ROLE_LABELS[role] || role);
    var blk = h(`<div class="rt-role-block"><div class="rt-role-head">${esc(label)}<span class="rt-iter">${model ? esc(model) : ""}${iter > 1 ? " · attempt " + iter : ""}</span></div><div class="rt-role-body"></div></div>`);
    els.log.appendChild(blk);
    var body = blk.querySelector(".rt-role-body");
    current.blocks[key] = body;
    els.log.scrollTop = els.log.scrollHeight;
    return body;
  }

  function dispatch(ev) {
    switch (ev.type) {
      case "run_start":
        els.log.innerHTML = ""; current.blocks = {};
        renderPipeline(ev.pipeline || []);
        setStatus("Running — workspace: " + (ev.workspace || ""), ACCENT);
        break;
      case "role_start":
        chipState(ev.role, "run", ev.model);
        current.active = ev.role + "_" + (ev.iteration || 1);
        roleBlock(ev.role, ev.iteration || 1, ev.model);
        setStatus((ROLE_LABELS[ev.role] || ev.role) + " working… (" + (ev.purpose || "") + " → " + (ev.model || "") + ")", ACCENT);
        break;
      case "delta": {
        var body = current.blocks[current.active] || roleBlock(ev.role, 1);
        body.appendChild(document.createTextNode(ev.text || ""));
        els.log.scrollTop = els.log.scrollHeight;
        break;
      }
      case "tool": {
        var b = current.blocks[current.active]; if (!b) break;
        b.appendChild(h(`<div class="rt-tool">⚙ ${esc(ev.tool || "tool")} ${ev.phase === "output" ? "✓" : "…"}</div>`));
        els.log.scrollTop = els.log.scrollHeight;
        break;
      }
      case "role_done":
        chipState(ev.role, "done");
        break;
      case "gate": {
        var cls = ev.status === "pass" ? "pass" : (ev.status === "halt" ? "halt" : "fail");
        var icon = ev.status === "pass" ? "✓" : (ev.status === "halt" ? "✋" : "✗");
        els.log.appendChild(h(`<div class="rt-gate ${cls}">${icon} GATE · ${esc(ev.gate)} — ${esc(ev.detail || "")}</div>`));
        els.log.scrollTop = els.log.scrollHeight;
        break;
      }
      case "pr": {
        var urlRow = ev.url ? ('<div class="rt-pr-row"><b>PR</b> <a href="' + esc(ev.url) + '" target="_blank" rel="noopener">' + esc(ev.url) + '</a></div>') : '';
        var card = h(
          '<div class="rt-pr">' +
            '<div class="rt-pr-head">📦 Pull Request <span class="rt-pr-mode">' + esc(ev.mode === "dry_run" ? "dry-run" : (ev.mode || "")) + '</span></div>' +
            '<div class="rt-pr-row"><b>branch</b> <code>' + esc(ev.branch || "") + '</code>' +
              (ev.committed ? ' <span style="color:' + OK + '">✓ committed</span>' : ' <span style="color:' + BAD + '">not committed</span>') + '</div>' +
            urlRow +
            '<div class="rt-pr-row"><b>commit</b> <code>' + esc(ev.commit || "") + '</code></div>' +
            '<div class="rt-pr-title">' + esc(ev.title || "") + '</div>' +
            '<div class="rt-pr-body">' + esc(ev.body || "") + '</div>' +
            (ev.stat ? '<pre class="rt-pr-stat">' + esc(ev.stat) + '</pre>' : '') +
            '<div class="rt-pr-actions"><button class="rt-pr-approve">✓ Approve &amp; Merge</button><button class="rt-pr-reject">Reject</button><span class="rt-pr-result"></span></div>' +
          '</div>');
        els.log.appendChild(card);
        (function () {
          var runId = ev.run_id || (current && current.runId);
          var approve = card.querySelector(".rt-pr-approve"), reject = card.querySelector(".rt-pr-reject"), result = card.querySelector(".rt-pr-result");
          approve.addEventListener("click", function () { mergeRun(runId, approve, reject, result); });
          reject.addEventListener("click", function () { approve.disabled = true; reject.disabled = true; result.textContent = " ✗ rejected — branch kept, not merged"; result.style.color = BAD; });
        })();
        els.log.scrollTop = els.log.scrollHeight;
        break;
      }
      case "run_done": {
        var color = ev.status === "passed" ? OK : (ev.status === "halted" ? WARN : BAD);
        var word = { passed: "✓ SHIPPED", failed: "✗ FAILED", halted: "✋ HALTED", error: "⚠ ERROR" }[ev.status] || ev.status;
        els.log.appendChild(h(`<div class="rt-final" style="color:#fff;background:${color}">${word}${ev.detail ? " — " + esc(ev.detail) : ""}</div>`));
        setStatus(word + (ev.detail ? " — " + ev.detail : ""), color);
        current.done = true;
        loadRecent();
        break;
      }
    }
  }

  function setRunBtn(mode) {
    if (mode === "stop") {
      els.run.dataset.mode = "stop"; els.run.textContent = "■ Stop";
      els.run.disabled = false; els.run.style.background = BAD;
    } else {
      els.run.dataset.mode = "run"; els.run.textContent = "▶ Run pipeline";
      els.run.disabled = false; els.run.style.background = "";
    }
  }

  async function stopRun() {
    if (!current || !current.runId) return;
    els.run.disabled = true; els.run.textContent = "Stopping…";
    setStatus("Stopping…", WARN);
    try { await fetch("/api/roundtable/" + current.runId + "/stop", { method: "POST" }); }
    catch (e) { /* ignore — the stream will end and finalize the UI */ }
  }

  async function startRun() {
    var title = els.title.value.trim();
    if (!title) { setStatus("A ticket title is required.", BAD); els.title.focus(); return; }
    current = { runId: null, blocks: {}, chips: {}, active: null, done: false };
    els.pipeline.innerHTML = ""; els.log.innerHTML = "";
    setStatus("Starting…", ACCENT);
    setRunBtn("stop");
    try {
      var resp = await fetch("/api/roundtable/start", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: title, description: els.desc.value, acceptance: els.ac.value, workspace: els.ws.value.trim() }),
      });
      var data = await resp.json();
      if (!resp.ok) { setStatus("Error: " + (data.error || resp.status), BAD); return; }
      current.runId = data.run_id;
      await streamRun(data.run_id);
      if (!current.done) setStatus("⏹ Stopped.", WARN);   // stream ended without run_done
    } catch (e) {
      setStatus("Failed: " + e, BAD);
    } finally {
      setRunBtn("run");
    }
  }

  async function streamRun(runId) {
    var resp = await fetch("/api/roundtable/" + runId + "/stream", { headers: { Accept: "text/event-stream" } });
    var reader = resp.body.getReader();
    var dec = new TextDecoder(); var buf = "";
    while (true) {
      var r = await reader.read();
      if (r.done) break;
      buf += dec.decode(r.value, { stream: true });
      var idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        var raw = buf.slice(0, idx); buf = buf.slice(idx + 2);
        raw.split("\n").forEach(function (line) {
          line = line.trim();
          if (line.slice(0, 6) !== "data: ") return;  // skip ':' heartbeats & 'event:' lines
          var payload = line.slice(6).trim();
          if (payload === "[DONE]") return;
          try { dispatch(JSON.parse(payload)); } catch (e) { /* heartbeat/non-json */ }
        });
      }
    }
  }

  async function loadRecent() {
    try {
      var resp = await fetch("/api/roundtable/runs");
      var data = await resp.json();
      els.recent.innerHTML = "";
      (data.runs || []).slice(0, 12).forEach(function (run) {
        var color = run.status === "passed" ? OK : (run.status === "failed" ? BAD : (run.status === "halted" ? WARN : "rgba(127,127,127,.5)"));
        var item = h(`<div class="rt-run-item" title="${esc(run.run_id)}"><span class="rt-dot" style="background:${color}"></span><span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(run.title)}</span><span style="opacity:.5">${esc(run.status)}</span></div>`);
        item.addEventListener("click", function () { loadRun(run.run_id); });
        els.recent.appendChild(item);
      });
    } catch (e) { /* ignore */ }
  }

  async function loadRun(runId) {
    try {
      var resp = await fetch("/api/roundtable/" + runId);
      var run = await resp.json();
      if (run.error) return;
      current = { runId: runId, blocks: {}, chips: {}, active: null };
      renderPipeline(Array.from(new Set((run.steps || []).map(function (s) { return s.role; }))));
      els.log.innerHTML = "";
      setStatus("Loaded run " + runId + " — " + run.status, ACCENT);
      (run.steps || []).forEach(function (s) {
        chipState(s.role, "done", s.model);
        var body = roleBlock(s.role, s.iteration || 1, s.model);
        body.textContent = s.output || "";
        if (s.verdict && s.verdict !== "unknown") {
          var pass = s.verdict === "pass";
          els.log.appendChild(h(`<div class="rt-gate ${pass ? "pass" : "fail"}">${pass ? "✓" : "✗"} GATE · qas — verdict ${esc(s.verdict.toUpperCase())}</div>`));
        }
      });
    } catch (e) { /* ignore */ }
  }

  // ---- per-role model config -----------------------------------------------
  function toggleConfig() { if (els.config.classList.contains("open")) closeConfig(); else openConfig(); }
  function closeConfig() { els.config.classList.remove("open"); if (els.body) els.body.style.display = ""; }
  async function openConfig() {
    els.config.classList.add("open");
    if (els.body) els.body.style.display = "none";
    els.cfgList.innerHTML = '<div style="opacity:.6">Loading…</div>';
    try {
      var resp = await fetch("/api/roundtable/config");
      var data = await resp.json();
      var eps = data.endpoints || [];
      els.cfgList.innerHTML = "";
      if (!eps.length) { els.cfgList.innerHTML = '<div style="opacity:.6">No enabled model endpoints. Add one in Settings → Models.</div>'; return; }
      (data.roles || []).forEach(function (role) {
        var row = h('<div class="rt-cfg-row"><span class="rt-cfg-role">' + esc(ROLE_LABELS[role.key] || role.title) + '</span></div>');
        var sel = document.createElement("select");
        var def = document.createElement("option");
        def.value = ""; def.textContent = "Default (" + role.purpose + ")"; sel.appendChild(def);
        eps.forEach(function (ep) {
          (ep.models || []).forEach(function (m) {
            var o = document.createElement("option");
            o.value = ep.endpoint_id + "|" + m;
            o.textContent = m + "  ·  " + ep.label;
            if (role.endpoint_id === ep.endpoint_id && role.model === m) o.selected = true;
            sel.appendChild(o);
          });
        });
        sel.addEventListener("change", function () { saveRoleModel(role.key, sel.value, sel); });
        row.appendChild(sel);
        els.cfgList.appendChild(row);
      });
      var modeRow = h('<div class="rt-cfg-row" style="border-top:1px solid rgba(127,127,127,.25);margin-top:10px;padding-top:14px;"><span class="rt-cfg-role">Release mode</span></div>');
      var modeSel = document.createElement("select");
      [["dry_run", "Dry-run — local branch + commit"], ["github", "GitHub — push + open a real PR"]].forEach(function (opt) {
        var o = document.createElement("option"); o.value = opt[0]; o.textContent = opt[1];
        if ((data.rte_mode || "dry_run") === opt[0]) o.selected = true;
        modeSel.appendChild(o);
      });
      modeSel.addEventListener("change", function () { saveRteMode(modeSel.value, modeSel); });
      modeRow.appendChild(modeSel); els.cfgList.appendChild(modeRow);
    } catch (e) {
      els.cfgList.innerHTML = '<div style="color:' + BAD + '">Failed to load: ' + esc(String(e)) + '</div>';
    }
  }
  async function saveRoleModel(roleKey, value, sel) {
    var parts = value ? value.split("|") : ["", ""];
    sel.disabled = true;
    try {
      await fetch("/api/roundtable/config", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role: roleKey, endpoint_id: parts[0], model: parts.slice(1).join("|") }),
      });
    } catch (e) { /* ignore */ }
    sel.disabled = false;
  }

  async function mergeRun(runId, approve, reject, result) {
    if (!runId) { result.textContent = " (no run id)"; result.style.color = BAD; return; }
    approve.disabled = true; reject.disabled = true;
    result.textContent = " merging…"; result.style.color = "";
    try {
      var resp = await fetch("/api/roundtable/" + runId + "/merge", { method: "POST" });
      var data = await resp.json();
      if (data.ok) { result.textContent = " ✓ " + (data.detail || "merged"); result.style.color = OK; }
      else { result.textContent = " ✗ " + (data.detail || data.error || "merge failed"); result.style.color = BAD; approve.disabled = false; }
    } catch (e) { result.textContent = " ✗ " + e; result.style.color = BAD; approve.disabled = false; }
  }

  async function saveRteMode(mode, sel) {
    sel.disabled = true;
    try { await fetch("/api/roundtable/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rte_mode: mode }) }); }
    catch (e) { /* ignore */ }
    sel.disabled = false;
  }

  // ---- wire the launch buttons (rail icon + expanded-sidebar item) ----------
  var _rtTries = 0;
  function wire() {
    ["rail-roundtable", "tool-roundtable-btn"].forEach(function (id) {
      var el = document.getElementById(id);
      if (el && !el.dataset.rtWired) { el.dataset.rtWired = "1"; el.addEventListener("click", open); }
    });
    if ((!document.getElementById("rail-roundtable") || !document.getElementById("tool-roundtable-btn")) && _rtTries++ < 20) {
      setTimeout(wire, 300);  // retry until both launchers exist in the DOM
    }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
  else wire();

  window.RoundTable = { open: open, close: close };
})();
