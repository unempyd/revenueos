/* RevenueOS — shared site behaviour. Loaded by every page.
   Scroll-edge chrome, the mobile sheet, section reveals, wayfinding.
   Design lives in design.css; this file only adds state classes.       */
(function () {
  "use strict";
  var reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  var nav = document.querySelector("header.topnav");
  var links = document.querySelector("nav.links");
  var btn = document.querySelector(".navbtn");

  /* the chrome earns its hairline only once content is beneath it */
  if (nav) {
    var edge = function () { nav.classList.toggle("is-scrolled", scrollY > 8); };
    addEventListener("scroll", edge, { passive: true });
    edge();
  }

  /* The sheet is built at BODY level on purpose: header.topnav carries a
     backdrop-filter, which makes it the containing block for any
     position:fixed descendant — a sheet inside it would be trapped in the
     header's own 60px box instead of filling the viewport. */
  var scrim = document.querySelector(".navscrim");
  if (!scrim) {
    scrim = document.createElement("div");
    scrim.className = "navscrim";
    scrim.setAttribute("aria-hidden", "true");
    document.body.appendChild(scrim);
  }
  var sheet = document.querySelector(".navsheet");
  if (!sheet && links) {
    sheet = document.createElement("nav");
    sheet.className = "navsheet";
    sheet.id = "navsheet";
    sheet.setAttribute("aria-label", "Site menu");
    links.querySelectorAll("a").forEach(function (a) { sheet.appendChild(a.cloneNode(true)); });
    var cta = document.querySelector(".nav-cta") || document.querySelector(".hero-ctas .cta-btn");
    if (cta) { var c = cta.cloneNode(true); c.className = "cta-btn"; sheet.appendChild(c); }
    document.body.appendChild(sheet);
    if (btn) btn.setAttribute("aria-controls", "navsheet");
  }

  function setOpen(open) {
    if (!btn) return;
    document.body.classList.toggle("nav-open", open);
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    btn.setAttribute("aria-label", open ? "Close menu" : "Open menu");
    var focus = open && sheet ? sheet.querySelector("a") : btn;
    if (focus) focus.focus({ preventScroll: true });
  }
  if (btn) btn.addEventListener("click", function () {
    setOpen(!document.body.classList.contains("nav-open"));
  });
  scrim.addEventListener("click", function () { setOpen(false); });
  if (sheet) sheet.addEventListener("click", function (e) { if (e.target.closest("a")) setOpen(false); });
  addEventListener("keydown", function (e) {
    if (e.key === "Escape" && document.body.classList.contains("nav-open")) setOpen(false);
  });
  /* returning to desktop must never leave the body scroll-locked */
  matchMedia("(min-width: 901px)").addEventListener("change", function (e) { if (e.matches) setOpen(false); });

  /* reveals — observe once, then stop paying for them */
  /* Anything already marked .rv in the markup must be observed too, or it
     keeps opacity:0 forever — a reveal class with no observer is an
     invisible element, which is worse than no animation at all. */
  var targets = document.querySelectorAll("section, .loop, .price-grid, .evidence-shots, .brief-box, .rv");
  targets.forEach(function (el) { el.classList.add("rv"); });
  if (reduce || !("IntersectionObserver" in window)) {
    targets.forEach(function (el) { el.classList.add("in"); });
  } else {
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (e) { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.05 });
    targets.forEach(function (el) { io.observe(el); });
    /* nothing stays invisible: anything on screen after 2s is revealed */
    setTimeout(function () {
      document.querySelectorAll(".rv:not(.in)").forEach(function (el) {
        if (el.getBoundingClientRect().top < innerHeight) el.classList.add("in");
      });
    }, 2000);
  }

  /* wayfinding — the nav reflects the section you are actually in */
  if (links) {
    var as = [].slice.call(links.querySelectorAll('a[href^="#"]'));
    var secs = as.map(function (a) { return document.querySelector(a.getAttribute("href")); }).filter(Boolean);
    if (secs.length) {
      var spy = new IntersectionObserver(function (es) {
        es.forEach(function (e) {
          if (!e.isIntersecting) return;
          as.forEach(function (a) {
            a.setAttribute("aria-current", a.getAttribute("href") === "#" + e.target.id ? "true" : "false");
          });
        });
      }, { rootMargin: "-45% 0px -50% 0px", threshold: 0 });
      secs.forEach(function (s) { spy.observe(s); });
    }
  }
})();

/* ══ The loop, demonstrated ════════════════════════════════════════════════
   Every line below is the actual stdout of the 2026-09-13 run against
   plausible.io — the same run the evidence screenshots come from. Nothing
   here is written for effect; if the product's output changes, this is wrong
   and should be re-captured, not edited.                                   */
(function () {
  "use strict";
  var root = document.querySelector(".loopdemo");
  if (!root) return;
  var body = root.querySelector(".term-body");
  var chips = [].slice.call(root.querySelectorAll(".stage-chip"));
  var card = root.querySelector(".demo-card");
  var btnApprove = root.querySelector('[data-act="approve"]');
  var btnExecute = root.querySelector('[data-act="execute"]');
  var measured = root.querySelector(".measured");
  var replay = root.querySelector(".replay");
  var reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;

  var SCRIPT = [
    { stage: 0, type: "revenueos init --from plausible.io" },
    { out: 'Onboarded <span class="ok">Plausible Analytics</span> — read from the public site.', pause: 260 },
    { out: "" },
    { stage: 1, type: "revenueos run all" },
    { out: '<span class="ok">ok</span>   discover   0 lead(s) read, 0 contactable, 0 qualified' },
    { out: '<span class="ok">ok</span>   seo        2 new site issue(s) from 12 crawled pages' },
    { out: '<span class="dim">              authority check unavailable (HTTP 403) — reported, not guessed</span>' },
    { out: '<span class="ok">ok</span>   content    3 new content idea(s) for website, seo, blog, twitter' },
    { out: '<span class="ok">ok</span>   monitor    0 conversation(s) worth joining — nothing surfaced to look busy' },
    { out: "" },
    { stage: 2, type: "revenueos today" },
    { out: "TODAY — Plausible Analytics" },
    { out: '  <span class="ok">2</span> SEO opportunities found' },
    { out: '  <span class="ok">3</span> content opportunities' },
    { out: ' <span class="dim">$0 pipeline generated — no lead was contacted on this run</span>' },
    { card: true, pause: 500 },
    { stage: 3, approve: true, pause: 700 },
    { type: "revenueos execute 4" },
    { out: 'executed [4] SEO: An SEO content plan: the searches to target' },
    { out: '  deliverable written to <span class="ok">data/outputs/20260913-seo-content-strategy-4.md</span>' },
    { out: "" },
    { stage: 4, type: "revenueos run measure" },
    { out: '<span class="ok">ok</span>   measure    1 result(s) measured' },
    { measured: true }
  ];

  var timers = [], running = false;
  function wait(ms) { return new Promise(function (r) { timers.push(setTimeout(r, ms)); }); }
  function clear() { timers.forEach(clearTimeout); timers = []; }

  function line(html, open) {
    var el = document.createElement("span");
    el.className = "term-line";
    /* `open` leaves the line without its newline so the caret can sit on it
       while the command is typed; the newline is added when typing ends. */
    el.innerHTML = open ? html : html + "\n";
    body.appendChild(el);
    body.scrollTop = body.scrollHeight;
    return el;
  }
  function setStage(i) {
    chips.forEach(function (c, n) {
      c.setAttribute("data-state", n < i ? "done" : n === i ? "active" : "");
    });
  }

  async function typeLine(cmd) {
    var el = line('<span class="p">$</span> ', true);
    var caret = document.createElement("span");
    caret.className = "caret";
    el.appendChild(caret);
    for (var i = 0; i < cmd.length; i++) {
      caret.insertAdjacentText("beforebegin", cmd[i]);
      /* a human types unevenly; a fixed interval reads as a machine */
      await wait(cmd[i] === " " ? 46 : 16 + Math.random() * 34);
    }
    await wait(230);
    caret.remove();
    el.appendChild(document.createTextNode("\n"));
  }

  function finalState() {
    body.innerHTML = "";
    SCRIPT.forEach(function (s) {
      if (s.type) line('<span class="p">$</span> ' + s.type);
      else if (typeof s.out === "string") line(s.out);
    });
    setStage(4);
    chips.forEach(function (c) { c.setAttribute("data-state", "done"); });
    card.classList.add("show", "done");
    btnApprove.classList.add("gone");
    btnExecute.classList.add("gone");
    measured.classList.add("show");
  }

  async function play() {
    if (running) return;
    running = true;
    clear();
    body.innerHTML = "";
    card.classList.remove("show", "done");
    btnApprove.classList.remove("gone", "pressed");
    btnExecute.classList.remove("gone", "pressed");
    measured.classList.remove("show");
    replay.classList.remove("show");
    setStage(0);

    for (var i = 0; i < SCRIPT.length; i++) {
      var s = SCRIPT[i];
      if (s.stage !== undefined) setStage(s.stage);
      if (s.card) { card.classList.add("show"); }
      if (s.approve) {
        /* feedback on press, then the button retires — the state it created
           is now shown by the card itself, so the control stops competing */
        btnApprove.classList.add("pressed"); await wait(140);
        btnApprove.classList.remove("pressed"); btnApprove.classList.add("gone");
        await wait(260);
        btnExecute.classList.add("pressed"); await wait(140);
        btnExecute.classList.remove("pressed");
      }
      if (s.measured) {
        card.classList.add("done");
        btnExecute.classList.add("gone");
        measured.classList.add("show");
      }
      if (s.type) await typeLine(s.type);
      else if (typeof s.out === "string") { line(s.out); await wait(s.out ? 120 : 60); }
      if (s.pause) await wait(s.pause);
    }
    chips.forEach(function (c) { c.setAttribute("data-state", "done"); });
    replay.classList.add("show");
    running = false;
  }

  if (reduce) { finalState(); return; }
  replay.addEventListener("click", play);
  /* start only when it is actually on screen, and only once */
  var io = new IntersectionObserver(function (es) {
    es.forEach(function (e) { if (e.isIntersecting) { io.disconnect(); play(); } });
  }, { threshold: 0.35 });
  io.observe(root);
})();

/* ══ Audit command builder ════════════════════════════════════════════════
   Their lead magnet asks for an email and promises a report later. Ours
   hands you the command immediately, because the audit genuinely needs no
   account — so there is nothing to gate.                                  */
(function () {
  "use strict";
  var input = document.getElementById("auditurl");
  var outEl = document.getElementById("auditcmd");
  if (!input || !outEl) return;

  function clean(v) {
    return (v || "").trim()
      .replace(/^https?:\/\//i, "")
      .replace(/^www\./i, "")
      .replace(/\/+$/, "")
      .split(/[\s/?#]/)[0];
  }
  function render() {
    var d = clean(input.value) || "yourcompany.com";
    outEl.textContent = "revenueos demo " + d;
  }
  input.addEventListener("input", render);
  render();

  document.querySelectorAll(".copy").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var t = document.querySelector(btn.getAttribute("data-copy"));
      if (!t) return;
      var text = "revenueos demo " + clean(input.value || "yourcompany.com");
      var done = function () {
        btn.textContent = "Copied"; btn.setAttribute("data-done", "1");
        setTimeout(function () { btn.textContent = "Copy"; btn.removeAttribute("data-done"); }, 1600);
      };
      if (navigator.clipboard && isSecureContext) {
        navigator.clipboard.writeText(text).then(done, done);
      } else {
        /* file:// and plain http have no async clipboard — fall back rather
           than leaving the button inert, which reads as broken */
        var ta = document.createElement("textarea");
        ta.value = text; ta.style.cssText = "position:fixed;opacity:0";
        document.body.appendChild(ta); ta.select();
        try { document.execCommand("copy"); } catch (e) {}
        ta.remove(); done();
      }
    });
  });
})();


/* ══ Walkthrough: six real screenshots, auto-advancing; click a step to hold it ═ */
(function () {
  "use strict";
  var root = document.getElementById("walkthrough");
  if (!root) return;
  var steps = [].slice.call(root.querySelectorAll(".stp"));
  var img = root.querySelector(".stage-shot img");
  var cap = root.querySelector(".stage-shot figcaption");
  var reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  var i = 0, timer = null, held = false;
  function show(n) {
    i = n % steps.length;
    steps.forEach(function (b, k) { b.classList.toggle("is-on", k === i); b.setAttribute("aria-selected", k === i ? "true" : "false"); });
    var b = steps[i];
    img.classList.add("fade");
    setTimeout(function () { img.src = b.getAttribute("data-shot"); img.alt = b.getAttribute("data-cap"); cap.textContent = b.getAttribute("data-cap"); img.classList.remove("fade"); }, 180);
  }
  function tick() { if (!held) show(i + 1); }
  steps.forEach(function (b, k) { b.addEventListener("click", function () { held = true; show(k); }); });
  root.addEventListener("mouseleave", function () { held = false; });
  if (!reduce) timer = setInterval(tick, 4200);
})();
