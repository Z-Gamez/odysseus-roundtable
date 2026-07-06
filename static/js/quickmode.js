// Quick-panel compact mode (?quick=1). A single Spotlight-style pill: a spark
// icon + one input line ("What can I help you with today?"). When you send,
// the answer appears below and the host window grows to fit (content-fit).
// Esc hides via the pywebview js_api.
(function () {
  var params = new URLSearchParams(location.search);
  if (params.get('quick') !== '1') return;

  var root = document.documentElement;
  root.classList.add('quick-mode');

  var css = document.createElement('style');
  css.textContent = [
    /* Nothing but the pill + answers. Hide nav, window controls, title bar,
       welcome splash, the composer toolbar, model picker and pinned tools. */
    '.quick-mode .sidebar, .quick-mode .icon-rail, .quick-mode .win-controls,',
    '.quick-mode .hamburger-btn, .quick-mode .chat-top-bar, .quick-mode #welcome-screen,',
    '.quick-mode .chat-input-bottom, .quick-mode #pinned-tools-bar { display: none !important; }',
    /* The window IS the pill: no dead area, DWM rounds the corners. */
    '.quick-mode body { padding: 0 !important; }',
    '.quick-mode .chat-container { flex-direction: column-reverse !important;',
    '  border-radius: 0 !important; background: var(--bg) !important; border: none !important; }',
    /* Skinny pill row: spark icon + input + compact model chip. */
    '.quick-mode .chat-input-bar { border: none !important; box-shadow: none !important;',
    '  border-radius: 0 !important; background: transparent !important; margin: 0 !important; padding: 0 !important; }',
    '.quick-mode .chat-input-top { display: flex !important; align-items: center !important;',
    '  gap: 11px !important; padding: 9px 16px !important; margin: 0 !important; }',
    '.quick-mode #quick-spark { flex: 0 0 auto; display: flex; color: var(--red); }',
    '.quick-mode #message { flex: 1 1 auto !important; background: transparent !important;',
    '  border: none !important; box-shadow: none !important; outline: none !important;',
    '  resize: none !important; padding: 0 !important; margin: 0 !important;',
    '  font-size: 15px !important; line-height: 1.35 !important; min-height: 22px !important; max-height: 110px !important; }',
    '.quick-mode #message::placeholder { opacity: 0.5 !important; }',
    /* Compact model chip on the right of the pill. */
    '.quick-mode .model-picker-wrap { flex: 0 0 auto !important; position: static !important; margin: 0 !important; }',
    '.quick-mode .model-picker-btn { font-size: 12px !important; opacity: 0.65 !important; padding: 3px 7px !important;',
    '  background: transparent !important; border: none !important; }',
    '.quick-mode .model-picker-btn:hover { opacity: 1 !important; }',
    /* Kill the composer prompt chevron (the red ">"); the spark replaces it. */
    '.quick-mode .chat-input-bar::before, .quick-mode .chat-input-top::before { content: none !important; display: none !important; }',
    /* Answers area: appears only once there is a reply, with a hairline rule. */
    '.quick-mode .chat-history:empty { display: none !important; }',
    '.quick-mode .chat-history { border-top: 1px solid color-mix(in srgb, var(--fg) 9%, transparent) !important;',
    '  padding: 10px 14px 12px !important; margin: 0 !important; }'
  ].join('\n');
  document.head.appendChild(css);

  var PLACEHOLDER = 'What can I help you with today?';

  // Placeholder + spark icon (Claude/Spotlight vibe). app.js re-sets the
  // placeholder on every window resize (and the panel resizes constantly to
  // content-fit), so this must be re-applied, not set once.
  function dressComposer() {
    var m = document.getElementById('message');
    if (m && m.getAttribute('placeholder') !== PLACEHOLDER) {
      m.setAttribute('placeholder', PLACEHOLDER);
    }
    var top = document.querySelector('.chat-input-top');
    if (top && !document.getElementById('quick-spark')) {
      var spark = document.createElement('span');
      spark.id = 'quick-spark';
      spark.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
        + '<path d="M12 1.5c.4 3.4 1.3 5.6 2.9 7.1 1.5 1.6 3.7 2.5 7.1 2.9-3.4.4-5.6 1.3-7.1 2.9-1.6 1.5-2.5 3.7-2.9 7.1-.4-3.4-1.3-5.6-2.9-7.1-1.5-1.6-3.7-2.5-7.1-2.9 3.4-.4 5.6-1.3 7.1-2.9C10.7 7.1 11.6 4.9 12 1.5z"/>'
        + '</svg>';
      top.insertBefore(spark, top.firstChild);
    }
  }

  function focusComposer() {
    var m = document.getElementById('message');
    if (m) { try { m.focus(); } catch (e) {} }
  }

  // Open on a clean slate (Spotlight starts empty, not on the last chat). Click
  // the new-chat control once, after the app has initialized.
  var startedFresh = false;
  function startFresh() {
    if (startedFresh) return;
    var btn = document.getElementById('sidebar-new-chat-btn') || document.getElementById('rail-new-session');
    var hist = document.getElementById('chat-history');
    if (btn && hist) {
      if (hist.children.length > 0) btn.click();
      startedFresh = true;
    }
  }

  window.addEventListener('DOMContentLoaded', function () { dressComposer(); focusComposer(); });
  setTimeout(function () { dressComposer(); startFresh(); focusComposer(); }, 700);
  setTimeout(function () { startFresh(); focusComposer(); }, 1400);
  window.__quickFocus = focusComposer;

  // Esc hides the panel (host stays warm for instant re-summon).
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      try { window.pywebview.api.hide(); } catch (err) {}
    }
  });

  // Enter-to-send. The app's own handler treats width <= 768 as "mobile" and
  // makes Enter insert a newline instead of sending — and the 640px pill is
  // always "mobile". Handle it ourselves in the CAPTURE phase (runs first) so
  // we submit the form and block the app's newline.
  document.addEventListener('keydown', function (e) {
    if (!e.target || e.target.id !== 'message') return;
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      if (window._ghostAutocomplete && window._ghostAutocomplete.isActive()) return; // let it accept
      var m = document.getElementById('message');
      if (!m || !m.value.trim()) { e.preventDefault(); return; }
      e.preventDefault();
      e.stopImmediatePropagation();
      var f = document.getElementById('chat-form');
      if (f) {
        if (f.requestSubmit) f.requestSubmit();
        else f.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
      }
    }
  }, true);

  // Content-fit: report the natural height so the host resizes the window
  // (grows downward as answers stream / the input wraps).
  var lastH = 0;
  function measure() {
    dressComposer();  // keep placeholder/spark against app.js resets
    var bar = document.querySelector('.chat-input-bar');
    var hist = document.getElementById('chat-history');
    if (!bar) return;
    var h = bar.offsetHeight + 2;
    var hasContent = hist && hist.children.length > 0;
    if (hasContent) h += Math.min(hist.scrollHeight, 440) + 2;
    h = Math.max(58, Math.min(Math.round(h), 680));
    if (Math.abs(h - lastH) > 6) {
      lastH = h;
      try { window.pywebview.api.resize(h); } catch (e) {}
    }
  }
  setInterval(measure, 300);
  setTimeout(measure, 500);
})();
