/* SAW Round Table — self-contained edge-to-edge view.
 *
 * Additive: attaches its own handler to the #rail-roundtable button and renders
 * everything inside one injected panel that fills the chat column (beside the
 * rail), so it doesn't touch Odysseus's view framework. Talks to
 * /api/roundtable/* and parses the orchestrator's tagged SSE stream
 * (run_start / role_start / delta / tool / role_done / gate / pr / run_done).
 */
(function () {
  "use strict";

  var ACCENT = "#7F77DD";           // SAW violet
  var ACCENT_SOFT = "rgba(127,119,221,.14)";
  var OK = "#1D9E75", BAD = "#D85A30", WARN = "#C9A227";

  var ROLE_LABELS = {
    bsa: "BSA · Analyst", architect: "Architect", developer: "Developer",
    qas: "QAS · Reviewer", security: "Security", tech_writer: "Tech Writer",
    rte: "RTE · Release"
  };

  var els = null;       // element refs once built
  var current = null;   // { runId, blocks: {roleKey_iter: state}, chips: {role: el} }
  var chipTimer = null; // elapsed-time interval while a run is live

  function h(html) { var t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstChild; }
  function esc(s) { return (s == null ? "" : String(s)).replace(/[&<>]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]; }); }
  function fmtDur(ms) {
    var s = Math.max(0, Math.round(ms / 1000));
    return s < 60 ? s + "s" : Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  }
  // The backend still keys runs by a short title; derive one from the project
  // description so the user doesn't have to fill a separate field.
  function deriveTitle(desc) {
    var t = (desc || "").trim().split("\n")[0].trim();
    if (t.length > 80) t = t.slice(0, 77) + "...";
    return t || "Round Table project";
  }
  function relTime(ts) {
    if (!ts) return "";
    var s = Math.max(0, Date.now() / 1000 - Number(ts));
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    return Math.floor(s / 86400) + "d ago";
  }

  // ---- inline SVG icons (no emoji) ------------------------------------------
  var ICONS = {
    table:   '<circle cx="12" cy="12" r="5"/><circle cx="12" cy="3.6" r="1.5"/><circle cx="12" cy="20.4" r="1.5"/><circle cx="3.6" cy="12" r="1.5"/><circle cx="20.4" cy="12" r="1.5"/>',
    play:    '<path d="M7 4.5v15l12-7.5z"/>',
    stop:    '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    close:   '<path d="M6 6l12 12M18 6L6 18"/>',
    check:   '<path d="M4.5 12.5l5 5 10-11"/>',
    cross:   '<path d="M6 6l12 12M18 6L6 18"/>',
    halt:    '<path d="M12 5v9M12 17.5v.5"/>',
    chev:    '<path d="M9 6l6 6-6 6"/>',
    tool:    '<path d="M14.7 6.3a4 4 0 0 0-5.3 5.2L4 17v3h3l5.4-5.4a4 4 0 0 0 5.2-5.3l-2.8 2.8-2.1-.7-.7-2.1z"/>',
    branch:  '<circle cx="6" cy="5" r="2.2"/><circle cx="6" cy="19" r="2.2"/><circle cx="18" cy="9" r="2.2"/><path d="M6 7.2v9.6M18 11.2c0 3-3 4-6 4"/>',
    refresh: '<path d="M20 11a8 8 0 1 0-2.3 6.3M20 5v6h-6"/>',
    folder:  '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    chevd:   '<path d="M6 9l6 6 6-6"/>',
    up:      '<path d="M12 19V5M5 12l7-7 7 7"/>',
    discuss: '<path d="M21 11a7 7 0 0 1-7 7H7l-4 3V11a7 7 0 0 1 7-7h4a7 7 0 0 1 7 7z"/><path d="M8.5 10.5h7M8.5 13.5h4"/>',
    bsa:       '<rect x="6" y="4" width="12" height="17" rx="2"/><path d="M9 4.5V3h6v1.5M9 9.5h6M9 13h6M9 16.5h4"/>',
    architect: '<path d="M4 20L20 4M4 20h5M4 20v-5M20 4h-5M20 4v5"/>',
    developer: '<path d="M8 7l-5 5 5 5M16 7l5 5-5 5M13 4l-2 16"/>',
    qas:       '<path d="M12 3l7 3v6c0 4-3 6.5-7 8.5C8 18.5 5 16 5 12V6z"/><path d="M9 12l2 2 4-4.5"/>',
    security:  '<rect x="5" y="10" width="14" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
    tech_writer: '<path d="M5 4h9l5 5v11H5z"/><path d="M14 4v5h5M8 13h8M8 16.5h6"/>',
    rte:       '<circle cx="6" cy="5" r="2.2"/><circle cx="6" cy="19" r="2.2"/><circle cx="18" cy="9" r="2.2"/><path d="M6 7.2v9.6M18 11.2c0 3-3 4-6 4"/>'
  };
  function icon(name, size) {
    return '<svg class="rt-i" width="' + (size || 14) + '" height="' + (size || 14) + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + (ICONS[name] || ICONS.tool) + '</svg>';
  }

  // ---- styles ----------------------------------------------------------------
  function injectStyles() {
    if (document.getElementById("rt-styles")) return;
    var css = `
    /* Frost the ENTIRE window — including behind/around the rail — so the blur
       has no visible boundary. The rail (and its hamburger) are lifted ABOVE
       the glass while the Round Table is open, so they stay crisp; the
       win-controls (z 2000) stay BELOW it, so they frost too. */
    #rt-backdrop{position:fixed;inset:0;z-index:2100;display:none;
      background:rgba(0,0,0,.35);backdrop-filter:blur(7px);-webkit-backdrop-filter:blur(7px);}
    #rt-backdrop.rt-open{display:block;}
    body.rt-open .sidebar,body.rt-open .icon-rail{z-index:2120;}
    body.rt-open .hamburger-btn{z-index:2130;}
    /* Top edge aligned with the rail card (10px), matching margins all around. */
    #rt-overlay{position:absolute;inset:10px 8px 10px 2px;z-index:2150;display:none;flex-direction:column;
      background:var(--sidebar-bg,var(--panel,#14161b));color:inherit;
      border:1px solid color-mix(in srgb, var(--fg,#9cdef2) 11%, transparent);
      border-radius:16px;overflow:hidden;}
    /* No shadow while docked — it paints a dark halo over the flat frost that
       reads as a blurred ghost of the panel. Windowed mode (over live UI)
       keeps a real drop shadow. */
    #rt-overlay.rt-windowed{box-shadow:0 16px 44px rgba(0,0,0,.5);}
    #rt-overlay.rt-fixed{position:fixed;inset:40px 12px 12px 12px;z-index:2150;}
    #rt-overlay.rt-open{display:flex;}
    /* Floating thin scrollbars everywhere inside the panel (standard props win
       over the app's chunky webkit styling on modern Chromium). */
    #rt-overlay, #rt-overlay *{scrollbar-width:thin;
      scrollbar-color:color-mix(in srgb, var(--fg,#9cdef2) 22%, transparent) transparent;}
    #rt-panel{flex:1;min-width:0;min-height:0;display:flex;flex-direction:column;}
    #rt-overlay .rt-i{flex:0 0 auto;}
    @keyframes rt-pulse{0%,100%{opacity:1}50%{opacity:.35}}
    @keyframes rt-spin{to{transform:rotate(360deg)}}

    #rt-head{display:flex;align-items:center;gap:12px;padding:12px 16px 8px;flex:0 0 auto;cursor:grab;user-select:none;}
    #rt-head:active{cursor:grabbing;}
    #rt-overlay.rt-floating{inset:auto;}
    /* Windowed (dragged-out) mode: a compact monitor — slim stepper + log,
       form hidden, no frost behind. Double-click the header to re-dock.
       Sits BELOW the window controls so min/max/close stay usable. */
    #rt-overlay.rt-windowed{z-index:600;}
    #rt-overlay.rt-windowed .rt-field,
    #rt-overlay.rt-windowed #rt-run,
    #rt-overlay.rt-windowed #rt-status{display:none;}
    #rt-overlay.rt-windowed #rt-left{width:172px;min-width:150px;padding:10px;}
    #rt-overlay.rt-windowed #rt-head{padding:9px 12px 6px;}
    #rt-overlay.rt-windowed .rt-tabs{display:none;}
    #rt-overlay.rt-windowed #rt-chip{max-width:55%;}
    #rt-min{cursor:pointer;border:none;background:transparent;color:inherit;opacity:.6;padding:6px;border-radius:8px;
      display:inline-flex;align-items:center;}
    #rt-min:hover{opacity:1;background:color-mix(in srgb, var(--fg,#9cdef2) 10%, transparent);}
    #rt-mini{position:fixed;right:18px;bottom:18px;z-index:2160;display:none;align-items:center;gap:9px;
      background:var(--sidebar-bg,var(--panel,#14161b));border-radius:999px;padding:10px 16px;cursor:pointer;
      box-shadow:0 10px 30px rgba(0,0,0,.5);font-size:12px;font-weight:600;color:inherit;}
    #rt-mini.on{display:inline-flex;}
    #rt-mini .rt-mini-ico{color:${ACCENT};display:inline-flex;}
    #rt-mini .rt-chip-dot{width:8px;height:8px;border-radius:50%;background:${ACCENT};flex:0 0 auto;}
    #rt-mini.run .rt-chip-dot{animation:rt-pulse 1.6s infinite;}
    #rt-mini.ok .rt-chip-dot{background:${OK};animation:none;}
    #rt-mini.bad .rt-chip-dot{background:${BAD};animation:none;}
    #rt-mini.warn .rt-chip-dot{background:${WARN};animation:none;}
    #rt-head .rt-title{display:flex;align-items:center;gap:8px;font-weight:700;font-size:13.5px;letter-spacing:.2px;color:${ACCENT};}
    #rt-chip{display:none;align-items:center;gap:7px;font-size:11.5px;border-radius:999px;padding:4px 12px;
      background:${ACCENT_SOFT};color:inherit;white-space:nowrap;max-width:40%;overflow:hidden;text-overflow:ellipsis;}
    #rt-chip.on{display:inline-flex;}
    #rt-chip .rt-chip-dot{width:7px;height:7px;border-radius:50%;background:${ACCENT};flex:0 0 auto;}
    #rt-chip.run .rt-chip-dot{animation:rt-pulse 1.6s infinite;}
    #rt-chip.ok{background:rgba(29,158,117,.14);} #rt-chip.ok .rt-chip-dot{background:${OK};}
    #rt-chip.bad{background:rgba(216,90,48,.14);} #rt-chip.bad .rt-chip-dot{background:${BAD};}
    #rt-chip.warn{background:rgba(201,162,39,.14);} #rt-chip.warn .rt-chip-dot{background:${WARN};}
    #rt-head .rt-spacer{flex:1;}
    .rt-tabs{display:inline-flex;background:color-mix(in srgb, var(--fg,#9cdef2) 6%, transparent);
      border-radius:11px;padding:3px;gap:2px;flex:0 0 auto;}
    .rt-tab{cursor:pointer;border:none;background:transparent;color:inherit;font:inherit;font-size:12px;
      padding:4px 13px;border-radius:8px;opacity:.6;transition:.15s;}
    .rt-tab:hover{opacity:1;}
    .rt-tab.active{opacity:1;background:${ACCENT_SOFT};color:${ACCENT};font-weight:700;}
    #rt-x{cursor:pointer;border:none;background:transparent;color:inherit;opacity:.6;padding:6px;border-radius:8px;
      display:inline-flex;align-items:center;}
    #rt-x:hover{opacity:1;background:color-mix(in srgb, var(--fg,#9cdef2) 10%, transparent);}

    #rt-body{flex:1;display:flex;min-height:0;gap:4px;padding:0 8px 8px;}
    #rt-left{width:270px;min-width:240px;display:flex;flex-direction:column;gap:10px;overflow:auto;padding:14px;
      background:rgba(0,0,0,.14);border-radius:12px;}
    #rt-main{flex:1;display:flex;flex-direction:column;min-width:0;}

    .rt-field label{display:block;font-size:10.5px;text-transform:uppercase;letter-spacing:.7px;opacity:.55;margin:0 0 4px;}
    .rt-field input,.rt-field textarea{width:100%;box-sizing:border-box;background:color-mix(in srgb, var(--fg,#9cdef2) 6%, transparent);
      color:inherit;border:1px solid transparent;border-radius:10px;
      padding:8px 10px;font:inherit;font-size:12.5px;transition:border-color .15s;}
    .rt-field input:focus,.rt-field textarea:focus{outline:none;border-color:${ACCENT};}
    .rt-field textarea{resize:vertical;min-height:52px;}
    #rt-desc{min-height:100px;}
    #rt-ws-field{position:relative;}
    #rt-ws-btn{width:100%;display:flex;align-items:center;gap:8px;box-sizing:border-box;cursor:pointer;text-align:left;
      background:color-mix(in srgb, var(--fg,#9cdef2) 6%, transparent);color:inherit;
      border:1px solid transparent;border-radius:10px;
      padding:8px 10px;font:inherit;font-size:12.5px;transition:border-color .15s;}
    #rt-ws-btn:hover{border-color:${ACCENT};}
    #rt-ws-btn > .rt-i:first-child{color:${ACCENT};}
    #rt-ws-name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
    #rt-ws-btn > .rt-i:last-child{opacity:.5;}
    #rt-ws-menu{display:none;position:absolute;left:0;right:0;top:100%;margin-top:6px;z-index:20;
      background:var(--sidebar-bg,var(--panel,#14161b));
      border-radius:12px;box-shadow:0 10px 26px rgba(0,0,0,.55);padding:8px;}
    #rt-ws-menu.open{display:block;}
    #rt-ws-path{width:100%;box-sizing:border-box;background:color-mix(in srgb, var(--fg,#9cdef2) 6%, transparent);color:inherit;
      border:1px solid transparent;border-radius:8px;
      padding:6px 8px;font:inherit;font-size:11.5px;margin-bottom:6px;}
    #rt-ws-path:focus{outline:none;border-color:${ACCENT};}
    #rt-ws-list{max-height:210px;overflow:auto;display:flex;flex-direction:column;gap:2px;}
    .rt-ws-row{display:flex;align-items:center;gap:7px;padding:6px 8px;border-radius:8px;font-size:12px;cursor:pointer;opacity:.85;}
    .rt-ws-row:hover{background:color-mix(in srgb, ${ACCENT} 12%, transparent);opacity:1;}
    .rt-ws-row .rt-i{color:${ACCENT};opacity:.8;}
    .rt-ws-row span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
    .rt-ws-empty{padding:8px;font-size:11.5px;opacity:.5;}
    #rt-ws-foot{display:flex;justify-content:flex-end;margin-top:6px;}
    #rt-ws-use{cursor:pointer;border:none;border-radius:8px;padding:6px 12px;font:inherit;font-weight:700;font-size:12px;
      color:#fff;background:${ACCENT};}
    #rt-ws-use:disabled{opacity:.5;cursor:default;}
    #rt-run{width:100%;cursor:pointer;border:none;border-radius:10px;padding:10px;font:inherit;font-weight:700;font-size:13px;
      color:#fff;background:${ACCENT};display:flex;align-items:center;justify-content:center;gap:8px;transition:filter .15s;}
    #rt-run:hover{filter:brightness(1.1);}
    #rt-run:disabled{opacity:.5;cursor:default;}

    #rt-pipeline{display:flex;flex-direction:column;margin-top:6px;}
    #rt-pipeline:empty{display:none;}
    #rt-pipeline::before{content:"Team";font-size:10.5px;text-transform:uppercase;letter-spacing:.7px;opacity:.55;margin-bottom:10px;}
    .rt-step{display:flex;gap:10px;align-items:flex-start;position:relative;padding-bottom:16px;}
    .rt-step::before{content:"";position:absolute;left:9px;top:22px;bottom:2px;width:2px;border-radius:1px;
      background:color-mix(in srgb, var(--fg,#9cdef2) 14%, transparent);}
    .rt-step:last-child{padding-bottom:2px;}
    .rt-step:last-child::before{display:none;}
    .rt-step.done::before{background:${ACCENT};opacity:.55;}
    .rt-step .rt-step-ico{width:20px;height:20px;border-radius:50%;flex:0 0 auto;display:inline-flex;align-items:center;justify-content:center;
      border:1.5px solid color-mix(in srgb, var(--fg,#9cdef2) 22%, transparent);color:transparent;transition:.2s;}
    .rt-step.run .rt-step-ico{border-color:${ACCENT};background:${ACCENT};color:#fff;animation:rt-pulse 1.6s infinite;}
    .rt-step.done .rt-step-ico{border-color:transparent;background:${ACCENT_SOFT};color:${ACCENT};}
    .rt-step.fail .rt-step-ico{border-color:transparent;background:rgba(216,90,48,.18);color:${BAD};animation:none;}
    .rt-step .rt-step-label{font-size:12px;font-weight:600;display:block;opacity:.55;}
    .rt-step.run .rt-step-label,.rt-step.done .rt-step-label,.rt-step.fail .rt-step-label{opacity:1;}
    .rt-step .rt-cmodel{display:block;font-size:10.5px;opacity:.5;font-weight:400;max-width:200px;
      overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}

    #rt-status{padding:8px 16px 4px;font-size:11.5px;opacity:.6;flex:0 0 auto;}
    #rt-log{flex:1;overflow:auto;padding:10px 12px 14px;display:flex;flex-direction:column;gap:10px;}

    /* Role cards as terminal windows: near-black surface, titlebar with
       traffic-light dots, prompt-prefixed command lines, blinking cursor. */
    @keyframes rt-blink{0%,49%{opacity:1}50%,100%{opacity:0}}
    .rt-role-block{background:#0b0e14;border-radius:12px;overflow:hidden;flex:0 0 auto;}
    .rt-role-head{padding:7px 12px;font-weight:700;font-size:11.5px;display:flex;gap:8px;align-items:center;cursor:pointer;
      background:#151a23;user-select:none;}
    .rt-role-block.live .rt-role-head{background:#1a1830;}
    .rt-role-head .rt-role-ico{color:${ACCENT};display:inline-flex;}
    .rt-role-head .rt-iter{font-weight:400;opacity:.5;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
    .rt-role-head .rt-head-spacer{flex:1;}
    .rt-role-head .rt-live-dot{width:7px;height:7px;border-radius:50%;background:${ACCENT};animation:rt-pulse 1.6s infinite;display:none;}
    .rt-role-block.live .rt-live-dot{display:inline-block;}
    .rt-role-head .rt-chev{opacity:.45;transition:transform .18s;transform:rotate(90deg);display:inline-flex;}
    .rt-role-block.collapsed .rt-chev{transform:rotate(0deg);}
    .rt-role-body{padding:11px 14px;white-space:pre-wrap;word-break:break-word;font-size:12.5px;line-height:1.6;
      color:color-mix(in srgb, var(--fg,#9cdef2) 88%, #ffffff 0%);}
    .rt-role-block.collapsed .rt-role-body{display:none;}
    .rt-role-block.live .rt-role-body::after{content:"▋";color:${ACCENT};margin-left:2px;
      animation:rt-blink 1.1s steps(1) infinite;}

    /* Tool calls as terminal command lines: ❯ prompt, name, status glyph. */
    .rt-pill{display:flex;align-items:center;gap:7px;font-size:11.5px;margin:4px 0;
      background:rgba(255,255,255,.03);border-radius:6px;padding:3px 8px;
      white-space:nowrap;max-width:100%;overflow:hidden;text-overflow:ellipsis;opacity:.9;}
    .rt-pill::before{content:"\\276F";color:${ACCENT};font-weight:700;flex:0 0 auto;}
    .rt-pill .rt-i{opacity:.7;flex:0 0 auto;}
    .rt-pill.pending .rt-i{animation:rt-spin 1.2s linear infinite;}
    .rt-pill.more{opacity:.5;}
    .rt-pill.more::before{content:"\\2026";}

    .rt-gate{display:flex;align-items:center;gap:9px;padding:8px 12px;border-radius:12px;font-size:12px;flex:0 0 auto;}
    .rt-gate b{font-weight:700;flex:0 0 auto;}
    .rt-gate span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;opacity:.85;}
    .rt-gate:hover span{white-space:normal;}
    .rt-gate.pass{color:${OK};background:rgba(29,158,117,.12);}
    .rt-gate.fail{color:${BAD};background:rgba(216,90,48,.12);}
    .rt-gate.halt{color:${WARN};background:rgba(201,162,39,.13);}

    .rt-final{margin:2px 0;padding:11px 13px;border-radius:12px;font-weight:700;text-align:center;color:#fff;flex:0 0 auto;}
    .rt-continue{border-radius:12px;padding:12px;
      background:color-mix(in srgb, ${ACCENT} 8%, transparent);flex:0 0 auto;}
    .rt-continue .rt-continue-h{font-size:12.5px;font-weight:600;margin-bottom:8px;opacity:.9;display:flex;align-items:center;gap:7px;}
    .rt-continue textarea{width:100%;box-sizing:border-box;background:rgba(0,0,0,.22);
      color:inherit;border:1px solid transparent;border-radius:10px;
      padding:8px 10px;font:inherit;font-size:12.5px;min-height:52px;resize:vertical;}
    .rt-continue textarea:focus{outline:none;border-color:${ACCENT};}
    .rt-continue button{margin-top:8px;cursor:pointer;border:none;border-radius:9px;padding:8px 14px;font:inherit;font-weight:700;color:#fff;background:${ACCENT};}

    #rt-config,#rt-history{flex:1;min-height:0;overflow:auto;padding:20px 24px;display:none;flex-direction:column;}
    #rt-config.open,#rt-history.open{display:flex;}
    #rt-config h3,#rt-history h3{margin:0;font-size:15px;display:flex;align-items:center;gap:9px;}
    .rt-count{font-size:10.5px;font-weight:700;background:${ACCENT_SOFT};color:${ACCENT};border-radius:999px;padding:2px 9px;}
    .rt-cfg-sub{opacity:.5;font-size:12px;margin:4px 0 16px;}
    .rt-sec{font-size:10.5px;text-transform:uppercase;letter-spacing:.8px;opacity:.5;margin:18px 0 8px;}
    .rt-card-row{display:flex;align-items:center;gap:12px;padding:10px 12px;margin-bottom:8px;
      border-radius:12px;background:color-mix(in srgb, var(--fg,#9cdef2) 3.5%, transparent);transition:background .15s;}
    .rt-card-row:hover{background:color-mix(in srgb, ${ACCENT} 9%, transparent);}
    .rt-role-badge{width:30px;height:30px;border-radius:9px;flex:0 0 auto;display:inline-flex;align-items:center;justify-content:center;
      background:${ACCENT_SOFT};color:${ACCENT};}
    .rt-card-main{flex:1;min-width:0;}
    .rt-card-title{font-size:12.5px;font-weight:700;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
    .rt-card-sub{font-size:11px;opacity:.5;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
    .rt-card-row select{flex:0 0 44%;min-width:0;background:color-mix(in srgb, var(--fg,#9cdef2) 7%, transparent);color:inherit;
      border:1px solid transparent;border-radius:9px;padding:7px 9px;font:inherit;font-size:12px;}
    .rt-card-row select:focus{outline:none;border-color:${ACCENT};}
    .rt-status-pill{flex:0 0 auto;font-size:10px;font-weight:700;border-radius:999px;padding:3px 10px;
      text-transform:uppercase;letter-spacing:.5px;}
    .rt-time{flex:0 0 auto;font-size:11px;opacity:.45;}
    #rt-hist-list .rt-card-row{cursor:pointer;}
    #rt-hist-list .rt-reuse{flex:0 0 auto;cursor:pointer;border:none;
      background:color-mix(in srgb, var(--fg,#9cdef2) 8%, transparent);color:inherit;font:inherit;font-size:11px;
      padding:5px 12px;border-radius:999px;opacity:.85;
      display:inline-flex;align-items:center;gap:5px;transition:.15s;}
    #rt-hist-list .rt-reuse:hover{opacity:1;background:${ACCENT_SOFT};color:${ACCENT};}
    .rt-slider{-webkit-appearance:none;appearance:none;flex:1;min-width:80px;height:6px;border-radius:3px;cursor:pointer;
      background:color-mix(in srgb, var(--fg,#9cdef2) 12%, transparent);outline:none;}
    .rt-slider::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;width:17px;height:17px;border-radius:50%;
      background:${ACCENT};border:2.5px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.4);cursor:grab;}
    .rt-slider::-webkit-slider-thumb:active{cursor:grabbing;}
    .rt-val{flex:0 0 auto;min-width:34px;text-align:center;font-weight:700;font-size:12.5px;color:${ACCENT};
      background:${ACCENT_SOFT};border-radius:8px;padding:4px 8px;}

    .rt-pr{background:color-mix(in srgb, var(--fg,#9cdef2) 3.5%, transparent);border-radius:12px;overflow:hidden;flex:0 0 auto;}
    .rt-pr-head{padding:9px 12px;font-weight:700;font-size:12.5px;background:color-mix(in srgb, ${ACCENT} 10%, transparent);
      display:flex;align-items:center;gap:8px;}
    .rt-pr-mode{font-weight:400;font-size:10.5px;opacity:.75;background:color-mix(in srgb, var(--fg,#9cdef2) 9%, transparent);
      border-radius:999px;padding:2px 9px;}
    .rt-pr-row{padding:5px 12px;font-size:12px;}
    .rt-pr-row code{background:color-mix(in srgb, var(--fg,#9cdef2) 8%, transparent);padding:1px 6px;border-radius:5px;}
    .rt-pr-title{padding:6px 12px;font-weight:700;font-size:13px;}
    .rt-pr-body{padding:4px 12px 10px;white-space:pre-wrap;font-size:12px;line-height:1.5;opacity:.85;}
    .rt-pr-stat{margin:0 12px 12px;padding:8px 10px;background:color-mix(in srgb, var(--fg,#9cdef2) 5%, transparent);
      border-radius:9px;font-size:11px;overflow:auto;white-space:pre;}
    .rt-pr-row a{color:${ACCENT};word-break:break-all;}
    .rt-pr-actions{display:flex;align-items:center;gap:8px;padding:8px 12px 12px;flex-wrap:wrap;}
    .rt-pr-approve{cursor:pointer;border:none;border-radius:9px;padding:7px 14px;font:inherit;font-weight:700;color:#fff;background:${OK};
      display:inline-flex;align-items:center;gap:6px;}
    .rt-pr-reject{cursor:pointer;border:none;border-radius:9px;
      padding:7px 14px;font:inherit;background:color-mix(in srgb, var(--fg,#9cdef2) 8%, transparent);color:inherit;}
    .rt-pr-approve:disabled,.rt-pr-reject:disabled{opacity:.5;cursor:default;}
    .rt-pr-result{font-size:12px;}
    `;
    var s = document.createElement("style"); s.id = "rt-styles"; s.textContent = css; document.head.appendChild(s);
  }

  // ---- panel construction -----------------------------------------------------
  function build() {
    injectStyles();
    var ov = h(`<div id="rt-overlay" role="dialog" aria-label="Round Table">
      <div id="rt-panel">
        <div id="rt-head">
          <span class="rt-title">${icon("table", 16)} Round Table</span>
          <span id="rt-chip"><span class="rt-chip-dot"></span><span id="rt-chip-text"></span></span>
          <span class="rt-spacer"></span>
          <span class="rt-tabs">
            <button class="rt-tab active" id="rt-tab-run">Run</button>
            <button class="rt-tab" id="rt-hist" title="Run history">History</button>
            <button class="rt-tab" id="rt-cfg" title="Per-role models">Models</button>
          </span>
          <button id="rt-min" title="Minimize — the run keeps going" aria-label="Minimize">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" aria-hidden="true"><path d="M5 12h14"/></svg>
          </button>
          <button id="rt-x" title="Close" aria-label="Close">${icon("close", 15)}</button>
        </div>
        <div id="rt-body">
          <div id="rt-left">
            <div class="rt-field"><label>Project</label><textarea id="rt-desc" placeholder="Describe what the team should build or change — the BSA turns this into a spec"></textarea></div>
            <div class="rt-field"><label>Acceptance criteria (optional)</label><textarea id="rt-ac" placeholder="- [ ] ... (BSA defines these if blank)"></textarea></div>
            <div class="rt-field" id="rt-ws-field"><label>Workspace</label>
              <input type="hidden" id="rt-ws">
              <button type="button" id="rt-ws-btn">${icon("folder", 14)}<span id="rt-ws-name">Choose a folder…</span>${icon("chevd", 12)}</button>
              <div id="rt-ws-menu">
                <input type="text" id="rt-ws-path" placeholder="Type a path and press Enter" spellcheck="false" autocomplete="off">
                <div id="rt-ws-list"></div>
                <div id="rt-ws-foot"><button type="button" id="rt-ws-use">Use this folder</button></div>
              </div>
            </div>
            <button id="rt-run">${icon("discuss", 14)} Discuss</button>
            <div id="rt-pipeline"></div>
          </div>
          <div id="rt-main">
            <div id="rt-status">Idle. Describe the project and hit Discuss.</div>
            <div id="rt-log"></div>
          </div>
        </div>
        <div id="rt-config">
          <h3>Models</h3>
          <div class="rt-cfg-sub">Route each role to a local or API model, and tune how the pipeline releases and retries.</div>
          <div id="rt-cfg-list"></div>
        </div>
        <div id="rt-history">
          <h3>Run history <span class="rt-count" id="rt-hist-count"></span></h3>
          <div class="rt-cfg-sub">Past pipeline runs — click one to view its transcript, or reuse its ticket.</div>
          <div id="rt-hist-list"></div>
        </div>
      </div>
      </div>`);

    // Window-wide frosted-glass layer; the rail floats above it (see CSS).
    var bd = h('<div id="rt-backdrop"></div>');
    document.body.appendChild(bd);
    bd.addEventListener("click", close);

    // Minimized pill: the run keeps streaming while collapsed; this shows the
    // live status (active role, attempt, elapsed) and restores on click.
    var mini = h('<div id="rt-mini" title="Round Table is running — click to reopen" role="button">' +
      '<span class="rt-mini-ico">' + icon("table", 14) + '</span>' +
      '<span class="rt-chip-dot"></span><span id="rt-mini-text">Round Table</span></div>');
    document.body.appendChild(mini);
    mini.addEventListener("click", restore);

    // Mount the panel inside the chat column so it sits beside the rail and
    // follows sidebar resize/collapse. Falls back to a fixed overlay in odd embeds.
    var host = document.querySelector(".chat-container");
    if (host) {
      if (getComputedStyle(host).position === "static") host.style.position = "relative";
      host.appendChild(ov);
    } else {
      ov.classList.add("rt-fixed");
      document.body.appendChild(ov);
    }

    els = {
      overlay: ov, backdrop: bd, mini: mini, miniText: mini.querySelector("#rt-mini-text"),
      desc: ov.querySelector("#rt-desc"),
      ac: ov.querySelector("#rt-ac"), ws: ov.querySelector("#rt-ws"),
      wsBtn: ov.querySelector("#rt-ws-btn"), wsName: ov.querySelector("#rt-ws-name"),
      wsMenu: ov.querySelector("#rt-ws-menu"), wsPath: ov.querySelector("#rt-ws-path"),
      wsList: ov.querySelector("#rt-ws-list"), wsUse: ov.querySelector("#rt-ws-use"),
      run: ov.querySelector("#rt-run"), pipeline: ov.querySelector("#rt-pipeline"),
      status: ov.querySelector("#rt-status"), log: ov.querySelector("#rt-log"),
      recent: ov.querySelector("#rt-hist-list"),
      body: ov.querySelector("#rt-body"), config: ov.querySelector("#rt-config"),
      cfgList: ov.querySelector("#rt-cfg-list"), history: ov.querySelector("#rt-history"),
      chip: ov.querySelector("#rt-chip"), chipText: ov.querySelector("#rt-chip-text"),
      tabs: { run: ov.querySelector("#rt-tab-run"), history: ov.querySelector("#rt-hist"), models: ov.querySelector("#rt-cfg") },
    };
    ov.querySelector("#rt-x").addEventListener("click", close);
    ov.querySelector("#rt-min").addEventListener("click", minimize);

    // Drag the panel around by its header, like the Notes mini-window. First
    // drag freezes the current size and switches to fixed positioning;
    // double-click the header to snap back to the docked column position.
    (function wireDrag() {
      var head = ov.querySelector("#rt-head");
      head.addEventListener("mousedown", function (e) {
        if (e.button !== 0 || e.target.closest("button")) return;
        var windowed = ov.classList.contains("rt-windowed");
        var W, H, oxx, oyy;
        if (!windowed) {
          // First drag DETACHES: shrink to a compact monitor window near the
          // cursor and drop the frost so the app behind is fully usable.
          W = Math.min(620, Math.round(window.innerWidth * 0.55));
          H = Math.min(560, Math.round(window.innerHeight * 0.68));
          oxx = Math.max(0, Math.min(window.innerWidth - W, e.clientX - 120));
          oyy = Math.max(0, Math.min(window.innerHeight - H, e.clientY - 18));
          ov.classList.add("rt-floating", "rt-windowed");
          els.backdrop.classList.remove("rt-open");
          document.body.classList.remove("rt-open");
        } else {
          var r = ov.getBoundingClientRect();
          W = r.width; H = r.height; oxx = r.left; oyy = r.top;
        }
        ov.style.position = "fixed";
        ov.style.width = W + "px";
        ov.style.height = H + "px";
        ov.style.left = oxx + "px";
        ov.style.top = oyy + "px";
        ov.style.right = "auto";
        ov.style.bottom = "auto";
        var sx = e.clientX, sy = e.clientY;
        e.preventDefault();
        function mv(ev) {
          var nx = Math.max(0, Math.min(window.innerWidth - 140, oxx + ev.clientX - sx));
          var ny = Math.max(0, Math.min(window.innerHeight - 60, oyy + ev.clientY - sy));
          ov.style.left = nx + "px";
          ov.style.top = ny + "px";
        }
        function up() {
          document.removeEventListener("mousemove", mv);
          document.removeEventListener("mouseup", up);
        }
        document.addEventListener("mousemove", mv);
        document.addEventListener("mouseup", up);
      });
      head.addEventListener("dblclick", function (e) {
        if (e.target.closest("button")) return;
        // Re-dock: full edge-to-edge panel with the frost back, re-aligned
        // to the rail's top edge.
        ov.classList.remove("rt-floating", "rt-windowed");
        ["position", "left", "top", "right", "bottom", "width", "height"].forEach(function (p) {
          ov.style.removeProperty(p);
        });
        alignTop();
        if (ov.classList.contains("rt-open")) {
          els.backdrop.classList.add("rt-open");
          document.body.classList.add("rt-open");
        }
      });
    })();

    els.tabs.run.addEventListener("click", function () { setTab("run"); });
    els.tabs.history.addEventListener("click", function () { setTab("history"); loadRecent(); });
    els.tabs.models.addEventListener("click", function () { setTab("models"); openConfig(); });
    els.run.addEventListener("click", function () {
      if (els.run.dataset.mode === "stop") stopRun(); else startRun();
    });

    // ---- workspace navigation menu (same /api/workspace/* API as the chat picker,
    // but scoped to the Round Table's own workspace, not the chat one) ----------
    var wsCur = "";
    function wsBase(p) {
      if (!p) return "";
      var parts = String(p).replace(/[\\\/]+$/, "").split(/[\\\/]/);
      return parts[parts.length - 1] || p;
    }
    els.setWs = function (p) {
      els.ws.value = p || "";
      els.wsName.textContent = p ? wsBase(p) : "Choose a folder…";
      els.wsBtn.title = p || "";
      try { if (p) localStorage.setItem("saw_ws", p); } catch (e) { /* ignore */ }
    };
    function wsRender(d) {
      wsCur = d.path || "";
      els.wsPath.value = wsCur;
      els.wsList.innerHTML = "";
      if (d.parent) {
        var upRow = h('<div class="rt-ws-row">' + icon("up", 12) + '<span>..</span></div>');
        upRow.addEventListener("click", function () { wsNav(d.parent); });
        els.wsList.appendChild(upRow);
      }
      (d.dirs || []).forEach(function (dir) {
        var row = h('<div class="rt-ws-row">' + icon("folder", 13) + '<span>' + esc(dir.name) + '</span></div>');
        row.addEventListener("click", function () { wsNav(dir.path); });
        els.wsList.appendChild(row);
      });
      if (!(d.dirs || []).length && !d.parent) {
        els.wsList.appendChild(h('<div class="rt-ws-empty">No subfolders</div>'));
      }
      els.wsUse.disabled = d.selectable === false;
      els.wsUse.title = d.selectable === false ? "This folder cannot be used as a workspace" : "";
    }
    function wsNav(p) {
      fetch("/api/workspace/browse" + (p ? ("?path=" + encodeURIComponent(p)) : ""), { credentials: "same-origin" })
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(wsRender)
        .catch(function () { /* ignore — keep the current listing */ });
    }
    els.wsBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      if (els.wsMenu.classList.toggle("open")) wsNav(els.ws.value || "");
    });
    els.wsPath.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); var v = els.wsPath.value.trim(); if (v) wsNav(v); }
    });
    els.wsUse.addEventListener("click", function () {
      if (wsCur) { els.setWs(wsCur); els.wsMenu.classList.remove("open"); }
    });
    document.addEventListener("click", function (e) {
      if (els && els.wsMenu.classList.contains("open") &&
          !els.wsMenu.contains(e.target) && !els.wsBtn.contains(e.target)) {
        els.wsMenu.classList.remove("open");
      }
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && els && els.overlay.classList.contains("rt-open")) close();
    });
    return els;
  }

  function setTab(name) {
    ["run", "history", "models"].forEach(function (t) {
      els.tabs[t].classList.toggle("active", t === name);
    });
    els.body.style.display = name === "run" ? "" : "none";
    els.config.classList.toggle("open", name === "models");
    els.history.classList.toggle("open", name === "history");
  }

  function open() {
    if (!els) build();
    els.mini.classList.remove("on");
    var windowed = els.overlay.classList.contains("rt-windowed");
    // Frost + rail-lift only in docked (modal) mode — the dragged-out compact
    // window leaves the app usable behind it.
    if (!windowed) {
      document.body.classList.add("rt-open");
      els.backdrop.classList.add("rt-open");
    }
    alignTop();
    els.overlay.classList.add("rt-open");
    setTab("run");
    try { var w = localStorage.getItem("saw_ws"); if (w) els.setWs(w); } catch (e) { /* ignore */ }
    loadRecent();
  }
  function close() {
    if (!els) return;
    els.overlay.classList.remove("rt-open");
    els.backdrop.classList.remove("rt-open");
    els.mini.classList.remove("on");
    document.body.classList.remove("rt-open");
  }

  // Align the panel's top edge EXACTLY with the rail card's top edge —
  // measured, not assumed, so container padding/borders can't skew it.
  // Skipped while the panel is floating/windowed.
  function alignTop() {
    if (!els) return;
    try {
      var sb = document.getElementById("sidebar");
      var hostEl = els.overlay.parentElement;
      if (sb && hostEl && sb.offsetParent !== null && !els.overlay.classList.contains("rt-floating")) {
        var t = sb.getBoundingClientRect().top - hostEl.getBoundingClientRect().top;
        if (t >= 0 && t < 80) els.overlay.style.top = Math.round(t) + "px";
      }
    } catch (e) { /* keep the CSS default */ }
  }

  // Minimize: hide the panel + frost but keep everything alive — the SSE
  // stream keeps dispatching into the (hidden) log and the mini pill mirrors
  // the live status chip (active role, attempt, elapsed).
  function minimize() {
    if (!els) return;
    els.overlay.classList.remove("rt-open");
    els.backdrop.classList.remove("rt-open");
    document.body.classList.remove("rt-open");
    els.mini.classList.add("on");
  }
  function restore() {
    if (!els) return;
    els.mini.classList.remove("on");
    // Windowed mode restores as the frost-free compact window it was.
    if (!els.overlay.classList.contains("rt-windowed")) {
      document.body.classList.add("rt-open");
      els.backdrop.classList.add("rt-open");
    }
    els.overlay.classList.add("rt-open");
  }

  // ---- live status chip --------------------------------------------------------
  function setChip(state, text) {
    if (!els) return;
    if (!state) { els.chip.classList.remove("on", "run", "ok", "bad", "warn"); return; }
    els.chip.classList.add("on");
    ["run", "ok", "bad", "warn"].forEach(function (c) { els.chip.classList.remove(c); });
    els.chip.classList.add(state);
    els.chipText.textContent = text || "";
    // Mirror into the minimized pill so it shows live progress while collapsed.
    if (els.mini) {
      ["run", "ok", "bad", "warn"].forEach(function (c) { els.mini.classList.remove(c); });
      els.mini.classList.add(state);
      els.miniText.textContent = text || "Round Table";
    }
  }
  function chipTick() {
    if (!current || !current.startTs || current.done) return;
    var base = current.chipBase || "running";
    setChip("run", base + " · " + fmtDur(Date.now() - current.startTs));
  }
  function startChipTimer() { stopChipTimer(); chipTimer = setInterval(chipTick, 1000); }
  function stopChipTimer() { if (chipTimer) { clearInterval(chipTimer); chipTimer = null; } }

  // ---- run lifecycle -------------------------------------------------------
  function setStatus(txt, color) { els.status.textContent = txt; els.status.style.color = color || ""; }

  function renderPipeline(pipeline) {
    els.pipeline.innerHTML = "";
    current.chips = {};
    pipeline.forEach(function (role) {
      var step = h(`<div class="rt-step" data-role="${role}">
          <span class="rt-step-ico">${icon("check", 11)}</span>
          <span style="min-width:0"><span class="rt-step-label">${esc(ROLE_LABELS[role] || role)}</span><span class="rt-cmodel"></span></span>
        </div>`);
      els.pipeline.appendChild(step);
      current.chips[role] = step;
    });
  }

  function chipState(role, state, model) {
    var c = current.chips[role]; if (!c) return;
    c.classList.remove("run", "done", "fail");
    if (state) c.classList.add(state);
    var meta = c.querySelector(".rt-cmodel");
    if (state === "run") {
      c.dataset.t0 = String(Date.now());
      if (model) c.dataset.model = model;
      meta.textContent = model || c.dataset.model || "";
    } else if (state === "done" || state === "fail") {
      var dur = c.dataset.t0 ? fmtDur(Date.now() - Number(c.dataset.t0)) : "";
      meta.textContent = [(model || c.dataset.model || ""), dur].filter(Boolean).join(" · ");
    } else if (model) {
      c.dataset.model = model;
      meta.textContent = model;
    }
  }

  // ---- role cards -----------------------------------------------------------
  function collapseOthers(exceptKey) {
    Object.keys(current.blocks).forEach(function (k) {
      var st = current.blocks[k];
      if (k !== exceptKey && st && st.blk) {
        st.blk.classList.remove("live");
        st.blk.classList.add("collapsed");
      }
    });
  }

  function roleBlock(role, iter, model) {
    var key = role + "_" + iter;
    if (current.blocks[key]) return current.blocks[key];
    var label = (ROLE_LABELS[role] || role);
    var blk = h(`<div class="rt-role-block live">
        <div class="rt-role-head">
          <span class="rt-role-ico">${icon(ICONS[role] ? role : "tool", 14)}</span>
          <span>${esc(label)}</span>
          <span class="rt-iter">${model ? esc(model) : ""}${iter > 1 ? " · attempt " + iter : ""}</span>
          <span class="rt-head-spacer"></span>
          <span class="rt-live-dot"></span>
          <span class="rt-chev">${icon("chev", 12)}</span>
        </div>
        <div class="rt-role-body"></div>
      </div>`);
    els.log.appendChild(blk);
    var state = {
      blk: blk,
      body: blk.querySelector(".rt-role-body"),
      pills: 0,            // visible tool pills in this block
      morePill: null,      // the "+N earlier" pill once we start folding
      folded: 0,           // how many pills have been folded away
      pending: [],         // pills awaiting their "output" phase
    };
    blk.querySelector(".rt-role-head").addEventListener("click", function () {
      blk.classList.toggle("collapsed");
    });
    current.blocks[key] = state;
    collapseOthers(key);
    els.log.scrollTop = els.log.scrollHeight;
    return state;
  }

  var MAX_PILLS = 10;
  function addToolPill(state, name) {
    // Fold the oldest visible pill into a "+N earlier" counter once we exceed the cap.
    if (state.pills >= MAX_PILLS) {
      var oldest = state.body.querySelector(".rt-pill:not(.more)");
      if (oldest) {
        oldest.remove();
        state.pills--;
        state.folded++;
        if (!state.morePill) {
          state.morePill = h('<span class="rt-pill more"></span>');
          state.body.insertBefore(state.morePill, state.body.firstChild);
        }
        state.morePill.textContent = "+" + state.folded + " earlier";
      }
    }
    var pill = h('<span class="rt-pill pending">' + icon("tool", 11) + '<span>' + esc(name || "tool") + '</span></span>');
    state.body.appendChild(pill);
    state.pills++;
    state.pending.push(pill);
    return pill;
  }
  function completeToolPill(state) {
    var pill = state.pending.shift();
    if (!pill) return;
    pill.classList.remove("pending");
    var ico = pill.querySelector(".rt-i");
    if (ico) ico.outerHTML = icon("check", 11);
  }

  function dispatch(ev) {
    switch (ev.type) {
      case "run_start":
        els.log.innerHTML = ""; current.blocks = {};
        renderPipeline(ev.pipeline || []);
        setStatus("Running — workspace: " + (ev.workspace || ""), ACCENT);
        current.startTs = Date.now();
        current.chipBase = "starting";
        setChip("run", "starting");
        startChipTimer();
        break;
      case "role_start": {
        chipState(ev.role, "run", ev.model);
        current.active = ev.role + "_" + (ev.iteration || 1);
        roleBlock(ev.role, ev.iteration || 1, ev.model);
        setStatus((ROLE_LABELS[ev.role] || ev.role) + " working… (" + (ev.purpose || "") + " → " + (ev.model || "") + ")", ACCENT);
        var iterTxt = (ev.iteration && ev.iteration > 1) ? " · attempt " + ev.iteration : "";
        current.chipBase = (ROLE_LABELS[ev.role] || ev.role) + iterTxt;
        chipTick();
        break;
      }
      case "delta": {
        var st = current.blocks[current.active] || roleBlock(ev.role, 1);
        st.body.appendChild(document.createTextNode(ev.text || ""));
        els.log.scrollTop = els.log.scrollHeight;
        break;
      }
      case "tool": {
        var stt = current.blocks[current.active]; if (!stt) break;
        if (ev.phase === "output") completeToolPill(stt);
        else addToolPill(stt, ev.tool);
        els.log.scrollTop = els.log.scrollHeight;
        break;
      }
      case "role_done": {
        chipState(ev.role, "done");
        // Collapse every finished card for this role; the next role_start expands its own.
        Object.keys(current.blocks).forEach(function (k) {
          if (k.indexOf(ev.role + "_") === 0) {
            var s = current.blocks[k];
            s.blk.classList.remove("live");
            s.blk.classList.add("collapsed");
          }
        });
        break;
      }
      case "gate": {
        var cls = ev.status === "pass" ? "pass" : (ev.status === "halt" ? "halt" : "fail");
        var ico = ev.status === "pass" ? "check" : (ev.status === "halt" ? "halt" : "cross");
        els.log.appendChild(h('<div class="rt-gate ' + cls + '" title="' + esc(ev.detail || "") + '">' +
          icon(ico, 14) + '<b>' + esc(ev.gate || "gate") + '</b><span>' + esc(ev.detail || "") + '</span></div>'));
        els.log.scrollTop = els.log.scrollHeight;
        break;
      }
      case "pr": {
        var urlRow = ev.url ? ('<div class="rt-pr-row"><b>PR</b> <a href="' + esc(ev.url) + '" target="_blank" rel="noopener">' + esc(ev.url) + '</a></div>') : '';
        var isDry = ev.mode === "dry_run";
        // Local mode commits straight to your branch — there's nothing to "approve & merge".
        var actions = isDry
          ? '<div class="rt-pr-row" style="opacity:.75">Committed to <code>' + esc(ev.branch || "") + '</code> in your workspace — the files are right there. Switch RTE to <b>GitHub</b> mode (Models tab) to open a real PR instead.</div>'
          : '<div class="rt-pr-actions"><button class="rt-pr-approve">' + icon("check", 12) + ' Approve &amp; Merge</button><button class="rt-pr-reject">Reject</button><span class="rt-pr-result"></span></div>';
        var card = h(
          '<div class="rt-pr">' +
            '<div class="rt-pr-head">' + icon("branch", 14) + (isDry ? 'Committed to your branch' : 'Pull Request') +
              ' <span class="rt-pr-mode">' + esc(isDry ? "local" : (ev.mode || "")) + '</span></div>' +
            '<div class="rt-pr-row"><b>branch</b> <code>' + esc(ev.branch || "") + '</code>' +
              (ev.committed ? ' <span style="color:' + OK + '">✓ committed</span>' : ' <span style="color:' + BAD + '">not committed</span>') + '</div>' +
            urlRow +
            '<div class="rt-pr-row"><b>commit</b> <code>' + esc(ev.commit || "") + '</code></div>' +
            '<div class="rt-pr-title">' + esc(ev.title || "") + '</div>' +
            '<div class="rt-pr-body">' + esc(ev.body || "") + '</div>' +
            (ev.stat ? '<pre class="rt-pr-stat">' + esc(ev.stat) + '</pre>' : '') +
            actions +
          '</div>');
        els.log.appendChild(card);
        (function () {
          var runId = ev.run_id || (current && current.runId);
          var approve = card.querySelector(".rt-pr-approve"), reject = card.querySelector(".rt-pr-reject"), result = card.querySelector(".rt-pr-result");
          if (approve) approve.addEventListener("click", function () { mergeRun(runId, approve, reject, result); });
          if (reject) reject.addEventListener("click", function () { approve.disabled = true; reject.disabled = true; result.textContent = " ✗ rejected — branch kept, not merged"; result.style.color = BAD; });
        })();
        els.log.scrollTop = els.log.scrollHeight;
        break;
      }
      case "run_done": {
        var color = ev.status === "passed" ? OK : (ev.status === "halted" ? WARN : BAD);
        var word = { passed: "SHIPPED", failed: "FAILED", halted: "HALTED", error: "ERROR" }[ev.status] || ev.status;
        els.log.appendChild(h('<div class="rt-final" style="background:' + color + '">' + word + (ev.detail ? " — " + esc(ev.detail) : "") + '</div>'));
        setStatus(word + (ev.detail ? " — " + ev.detail : ""), color);
        current.done = true;
        stopChipTimer();
        var chipCls = ev.status === "passed" ? "ok" : (ev.status === "halted" ? "warn" : "bad");
        setChip(chipCls, word.toLowerCase() + " · " + fmtDur(Date.now() - (current.startTs || Date.now())));
        if (ev.status !== "passed" && current.active) {
          var failedRole = current.active.split("_")[0];
          chipState(failedRole, "fail");
        }
        loadRecent();
        if (ev.status === "passed") showContinue(current.runId);
        break;
      }
    }
  }

  function setRunBtn(mode) {
    if (mode === "stop") {
      els.run.dataset.mode = "stop"; els.run.innerHTML = icon("stop", 13) + " Stop";
      els.run.disabled = false; els.run.style.background = BAD;
    } else {
      els.run.dataset.mode = "run"; els.run.innerHTML = icon("discuss", 14) + " Discuss";
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

  // Shared launch path for both a fresh run and a follow-up ("continue the discussion").
  async function launchRun(payload) {
    current = { runId: null, blocks: {}, chips: {}, active: null, done: false, startTs: null, chipBase: "" };
    els.pipeline.innerHTML = ""; els.log.innerHTML = "";
    setStatus(payload.parent_run_id ? "Starting follow-up…" : "Starting…", ACCENT);
    setRunBtn("stop");
    try { if (payload.workspace) localStorage.setItem("saw_ws", payload.workspace); } catch (e) { /* ignore */ }
    try {
      var resp = await fetch("/api/roundtable/start", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      var data = await resp.json();
      if (!resp.ok) { setStatus("Error: " + (data.error || resp.status), BAD); return; }
      current.runId = data.run_id;
      await streamRun(data.run_id);
      if (!current.done) {
        setStatus("Stopped.", WARN);   // stream ended without run_done
        setChip("warn", "stopped");
      }
    } catch (e) {
      setStatus("Failed: " + e, BAD);
      setChip("bad", "failed to start");
    } finally {
      setRunBtn("run");
      stopChipTimer();
    }
  }

  async function startRun() {
    var desc = els.desc.value.trim();
    if (!desc) { setStatus("Describe the project first.", BAD); els.desc.focus(); return; }
    await launchRun({ title: deriveTitle(desc), description: desc,
                      acceptance: els.ac.value, workspace: els.ws.value.trim() });
  }

  // Follow-up: keep the workspace + project, send the user's requested change, and link the
  // parent run so the team builds on what already exists (reads prior SPEC.md + files).
  async function continueRun(parentId, changes) {
    await launchRun({ title: deriveTitle(els.desc.value) , description: changes,
                      acceptance: "", workspace: els.ws.value.trim(), parent_run_id: parentId });
  }

  // After a successful run, offer a box to request changes and re-run on top of the result.
  function showContinue(parentId) {
    var card = h('<div class="rt-continue"><div class="rt-continue-h">' + icon("refresh", 13) +
      ' Continue the discussion — the team keeps everything it just built and applies your changes</div>' +
      '<textarea class="rt-change" placeholder="Describe the changes you want, e.g. \'make the header blue and add a Friends counter\'"></textarea>' +
      '<button class="rt-change-go">Send changes to the team</button></div>');
    els.log.appendChild(card);
    var box = card.querySelector(".rt-change");
    card.querySelector(".rt-change-go").addEventListener("click", function () {
      var changes = box.value.trim();
      if (!changes) { box.focus(); return; }
      continueRun(parentId, changes);
    });
    box.focus();
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

  var STATUS_PILL = {
    passed:  ["rgba(29,158,117,.16)", OK],
    failed:  ["rgba(216,90,48,.16)", BAD],
    halted:  ["rgba(201,162,39,.16)", WARN],
    stopped: ["rgba(140,146,160,.14)", "#8a93a3"],
    running: [ACCENT_SOFT, ACCENT],
  };

  async function loadRecent() {
    try {
      var resp = await fetch("/api/roundtable/runs");
      var data = await resp.json();
      els.recent.innerHTML = "";
      var runs = (data.runs || []).slice(0, 20);
      var cnt = document.getElementById("rt-hist-count");
      if (cnt) cnt.textContent = runs.length ? String(runs.length) : "";
      runs.forEach(function (run) {
        var p = STATUS_PILL[run.status] || STATUS_PILL.stopped;
        var item = h('<div class="rt-card-row" title="View transcript — ' + esc(run.run_id) + '">' +
          '<span class="rt-status-pill" style="background:' + p[0] + ';color:' + p[1] + '">' + esc(run.status || "?") + '</span>' +
          '<span class="rt-card-main"><span class="rt-card-title">' + esc(run.title) + '</span>' +
          '<span class="rt-card-sub">' + esc(run.run_id) + '</span></span>' +
          '<span class="rt-time">' + relTime(run.created_at) + '</span>' +
          '<button class="rt-reuse" title="Load this ticket into the form to run again">' + icon("refresh", 11) + ' Reuse</button></div>');
        item.addEventListener("click", function () { setTab("run"); loadRun(run.run_id); });
        item.querySelector(".rt-reuse").addEventListener("click", function (e) { e.stopPropagation(); reuseRun(run.run_id); });
        els.recent.appendChild(item);
      });
    } catch (e) { /* ignore */ }
  }

  // Pull a past run's ticket back into the form so it can be run again (tweaked
  // or as-is). Does NOT auto-start — the user reviews then hits Run.
  async function reuseRun(runId) {
    try {
      var resp = await fetch("/api/roundtable/" + runId);
      var run = await resp.json();
      if (run.error) return;
      els.desc.value = run.description || run.title || "";
      els.ac.value = run.acceptance || "";
      if (run.workspace) els.setWs(run.workspace);
      setTab("run");
      setStatus("Loaded project from " + runId + " — review and hit Discuss.", ACCENT);
      els.desc.focus();
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
      setChip(run.status === "passed" ? "ok" : (run.status === "failed" ? "bad" : "warn"), esc(run.status));
      (run.steps || []).forEach(function (s) {
        chipState(s.role, "done", s.model);
        var st = roleBlock(s.role, s.iteration || 1, s.model);
        st.body.textContent = s.output || "";
        st.blk.classList.remove("live");
        st.blk.classList.add("collapsed");
        if (s.verdict && s.verdict !== "unknown") {
          var pass = s.verdict === "pass";
          els.log.appendChild(h('<div class="rt-gate ' + (pass ? "pass" : "fail") + '">' +
            icon(pass ? "check" : "cross", 14) + '<b>qas</b><span>verdict ' + esc(s.verdict.toUpperCase()) + '</span></div>'));
        }
      });
    } catch (e) { /* ignore */ }
  }

  // ---- per-role model config -----------------------------------------------
  var TIER_LABELS = {
    saw_heavy: "heavy tier — code, review & judgment",
    saw_cheap: "light tier — docs & analysis",
  };

  async function openConfig() {
    els.cfgList.innerHTML = '<div style="opacity:.6">Loading…</div>';
    try {
      var resp = await fetch("/api/roundtable/config");
      var data = await resp.json();
      var eps = data.endpoints || [];
      els.cfgList.innerHTML = "";
      if (!eps.length) { els.cfgList.innerHTML = '<div style="opacity:.6">No enabled model endpoints. Add one in Settings → Models.</div>'; return; }
      els.cfgList.appendChild(h('<div class="rt-sec">Roles</div>'));
      (data.roles || []).forEach(function (role) {
        var row = h('<div class="rt-card-row">' +
          '<span class="rt-role-badge">' + icon(ICONS[role.key] ? role.key : "tool", 15) + '</span>' +
          '<span class="rt-card-main"><span class="rt-card-title">' + esc(ROLE_LABELS[role.key] || role.title) + '</span>' +
          '<span class="rt-card-sub">' + esc(TIER_LABELS[role.purpose] || role.purpose || "") + '</span></span></div>');
        var sel = document.createElement("select");
        if (!role.endpoint_id || !role.model) {
          var ph = document.createElement("option");
          ph.value = ""; ph.textContent = "Choose a model…";
          ph.disabled = true; ph.selected = true;
          sel.appendChild(ph);
        }
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

      els.cfgList.appendChild(h('<div class="rt-sec">Pipeline settings</div>'));
      var modeRow = h('<div class="rt-card-row">' +
        '<span class="rt-role-badge">' + icon("branch", 15) + '</span>' +
        '<span class="rt-card-main"><span class="rt-card-title">Release mode</span>' +
        '<span class="rt-card-sub">what the RTE does with a shipped change</span></span></div>');
      var modeSel = document.createElement("select");
      [["dry_run", "Dry-run — local branch + commit"], ["github", "GitHub — push + open a real PR"]].forEach(function (opt) {
        var o = document.createElement("option"); o.value = opt[0]; o.textContent = opt[1];
        if ((data.rte_mode || "dry_run") === opt[0]) o.selected = true;
        modeSel.appendChild(o);
      });
      modeSel.addEventListener("change", function () { saveRteMode(modeSel.value, modeSel); });
      modeRow.appendChild(modeSel); els.cfgList.appendChild(modeRow);

      var iterRow = h('<div class="rt-card-row">' +
        '<span class="rt-role-badge">' + icon("refresh", 15) + '</span>' +
        '<span class="rt-card-main" style="flex:0 1 220px"><span class="rt-card-title">Max attempts</span>' +
        '<span class="rt-card-sub">Dev↔QA retries — ∞ keeps going until it passes</span></span></div>');
      // Server stores 0 = infinite; the slider represents ∞ as its top position (11).
      var rawIter = parseInt(data.max_iterations, 10);
      var iterVal = (rawIter === 0) ? 11 : Math.max(1, Math.min(rawIter || 3, 10));
      var fmtIter = function (v) { return parseInt(v, 10) >= 11 ? "∞" : String(v); };
      var iterSlider = document.createElement("input");
      iterSlider.type = "range"; iterSlider.min = "1"; iterSlider.max = "11"; iterSlider.step = "1";
      iterSlider.className = "rt-slider";
      iterSlider.value = String(iterVal);
      var track = "color-mix(in srgb, var(--fg,#9cdef2) 12%, transparent)";
      var paint = function () {
        var p = (parseInt(iterSlider.value, 10) - 1) / 10 * 100;
        iterSlider.style.background = "linear-gradient(90deg, " + ACCENT + " " + p + "%, " + track + " " + p + "%)";
      };
      var iterNum = h('<span class="rt-val">' + fmtIter(iterVal) + '</span>');
      iterSlider.addEventListener("input", function () { iterNum.textContent = fmtIter(iterSlider.value); paint(); });
      iterSlider.addEventListener("change", function () { saveMaxIterations(iterSlider.value, iterSlider); iterNum.textContent = fmtIter(iterSlider.value); paint(); });
      paint();
      iterRow.appendChild(iterSlider);
      iterRow.appendChild(iterNum);
      els.cfgList.appendChild(iterRow);
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

  async function saveMaxIterations(n, el) {
    var raw = parseInt(n, 10) || 3;
    // Slider's top position (11) means ∞, stored server-side as 0.
    var send = raw >= 11 ? 0 : Math.max(1, Math.min(raw, 10));
    el.disabled = true;
    try { await fetch("/api/roundtable/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ max_iterations: send }) }); }
    catch (e) { /* ignore */ }
    el.disabled = false;
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

  // `_dispatch` / `_demoReset` are internal hooks for automated UI tests and
  // demo capture: _demoReset seeds the run state launchRun would normally
  // create, then _dispatch feeds a scripted event through the exact same
  // render path the live SSE stream uses, so a replay renders identically to
  // a real run. Not part of the public API.
  window.RoundTable = {
    open: open, close: close, minimize: minimize, restore: restore,
    _dispatch: dispatch,
    _demoReset: function () {
      current = { runId: "rt_demo", blocks: {}, chips: {}, active: null,
                  done: false, startTs: null, chipBase: "" };
    },
  };
})();
