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
  var targets = document.querySelectorAll("section, .loop, .price-grid, .evidence-shots, .brief-box");
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
