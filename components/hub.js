/* =========================================================
   SZ Procure — Components Hub V2 (client logic)
   - Search: fuzzy match across PARTS / MANUFACTURERS /
             CATEGORIES / SUBCATEGORIES, with entity-type
             discrimination and correct routing to existing URLs.
   - Browse: 6 fixed categories, collapsible -> subcategories.
   - Pure front-end; consumes window.SZ_COMPONENTS
     (generated read-only by build_components_data.py).
   Frozen modules (gen_parts.py / parts.json / global CSS) are
   NOT touched. This file is additive only.
   ========================================================= */
(function () {
  "use strict";

  var DATA = window.SZ_COMPONENTS || { categories: [], manufacturers: [], parts: [] };

  /* ---------- helpers ---------- */
  function norm(s) { return (s || "").toLowerCase().trim(); }

  function acronyms(name) {
    var words = (name || "").split(/\s+/);
    var wi = words.map(function (w) { return (w.charAt(0) || ""); }).join("").toLowerCase();
    var caps = (name.match(/[A-Z]/g) || []).join("").toLowerCase();
    return [wi, caps];
  }

  // ranking: 1 exact > 2 prefix > 3 acronym > 4 substring > 0 none
  function score(query, label) {
    var q = norm(query), l = norm(label);
    if (!q || !l) return 0;
    if (l === q) return 1;
    if (l.indexOf(q) === 0) return 2;
    var ac = acronyms(label);
    for (var i = 0; i < ac.length; i++) {
      if (ac[i] === q || ac[i].indexOf(q) === 0) return 3;
    }
    if (l.indexOf(q) > -1) return 4;
    return 0;
  }

  // P0-a fix: a PART is reachable by its canonical slug / clean form as well as
  // the raw mpn. Aliases are SEARCH-ONLY — they never create a new product entry.
  // search() keeps exactly ONE entry per SKU and routes every matching form to the
  // same label / url / type, so no duplicate PARTs appear in the dropdown.
  function partAliases(p) {
    var als = [];
    if (p && p.slug) als.push(p.slug);                       // canonical url_slug
    if (p && p.clean_mpn) als.push(p.clean_mpn);             // forward-compat if data exposes it
    if (p && p.url_slug) als.push(p.url_slug);               // forward-compat if data exposes it
    if (p && p.mpn) {
      var clean = String(p.mpn).toLowerCase().replace(/[^a-z0-9]/g, ""); // clean_mpn form
      if (clean) als.push(clean);
    }
    var seen = {}, out = [];
    als.forEach(function (a) {
      var k = String(a).toLowerCase();
      if (!seen[k]) { seen[k] = 1; out.push(a); }
    });
    return out;
  }

  // Score an entry across its label + aliases, keeping the BEST (lowest) rank.
  // Entries without aliases (MANUFACTURER / SUBCATEGORY / CATEGORY) fall back to
  // the label only, so their existing behaviour is unchanged.
  function scoreEntry(query, e) {
    var cands = [e.label];
    if (e.aliases && e.aliases.length) {
      for (var i = 0; i < e.aliases.length; i++) {
        if (e.aliases[i] && cands.indexOf(e.aliases[i]) === -1) cands.push(e.aliases[i]);
      }
    }
    var best = 0;
    for (var j = 0; j < cands.length; j++) {
      var s = score(query, cands[j]);
      if (s > 0 && (best === 0 || s < best)) best = s;
    }
    return best;
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  /* ---------- build flat search index (once) ---------- */
  var IDX = { parts: [], manufacturers: [], categories: [], subcategories: [] };

  (DATA.parts || []).forEach(function (p) {
    IDX.parts.push({
      label: p.mpn,
      mfr: p.mfr,
      subcat: p.subcat,
      url: p.url,
      type: "PART",
      aliases: partAliases(p)
    });
  });
  (DATA.manufacturers || []).forEach(function (m) {
    IDX.manufacturers.push({ label: m.name, count: m.count, url: m.url, type: "MANUFACTURER" });
  });
  (DATA.categories || []).forEach(function (c) {
    IDX.categories.push({ label: c.name, count: c.count, url: c.url, type: "CATEGORY" });
    (c.subcategories || []).forEach(function (s) {
      IDX.subcategories.push({ label: s.name, count: s.count, url: s.url, type: "SUBCATEGORY" });
    });
  });

  var TYPE_RANK = { PART: 0, MANUFACTURER: 1, SUBCATEGORY: 2, CATEGORY: 3 };

  function search(query) {
    var groups = { PART: [], MANUFACTURER: [], SUBCATEGORY: [], CATEGORY: [] };
    Object.keys(IDX).forEach(function (g) {
      IDX[g].forEach(function (e) {
        var sc = scoreEntry(query, e);
        if (sc > 0) groups[e.type].push({ e: e, sc: sc });
      });
    });
    Object.keys(groups).forEach(function (t) {
      groups[t].sort(function (a, b) {
        if (a.sc !== b.sc) return a.sc - b.sc;            // better rank first
        return a.e.label.localeCompare(b.e.label);        // then alphabetical
      });
    });
    return groups;
  }

  /* ---------- render search results ---------- */
  var RESULT_META = {
    PART: { title: "Parts", hint: "Exact match opens the product page" },
    MANUFACTURER: { title: "Manufacturers", hint: "" },
    SUBCATEGORY: { title: "Subcategories", hint: "" },
    CATEGORY: { title: "Categories", hint: "" }
  };

  function renderResults(query, groups, box) {
    var q = norm(query);
    if (!q) { box.innerHTML = ""; box.hidden = true; return; }
    var total = 0;
    var html = "";
    ["PART", "MANUFACTURER", "SUBCATEGORY", "CATEGORY"].forEach(function (t) {
      var list = groups[t].slice(0, 8);
      if (!list.length) return;
      total += list.length;
      html += '<div class="sr-group"><div class="sr-group-head">' +
        esc(RESULT_META[t].title) + " (" + list.length + ")</div>";
      list.forEach(function (item) {
        var e = item.e;
        var sub = "";
        if (e.type === "PART") {
          sub = [e.mfr, e.subcat].filter(Boolean).join(" · ");
        } else if (e.count != null) {
          sub = e.count + " items";
        }
        html += '<a class="sr-item" href="' + esc(e.url) + '">' +
          '<span class="sr-label">' + esc(e.label) + '</span>' +
          (sub ? '<span class="sr-sub">' + esc(sub) + '</span>' : '') +
          '<span class="sr-go" aria-hidden="true">&rarr;</span></a>';
      });
      html += "</div>";
    });
    if (total === 0) {
      var pn = encodeURIComponent(query);
      box.innerHTML = '<div class="sr-empty">No exact match for <strong>' + esc(query) +
        '</strong>. We can still source it — ' +
        '<a href="/request-a-quote/?pn=' + pn + '">request a quote &rarr;</a></div>';
      box.hidden = false;
      return;
    }
    box.innerHTML = html;
    box.hidden = false;
  }

  function bestOverall(groups) {
    var best = null;
    ["PART", "MANUFACTURER", "SUBCATEGORY", "CATEGORY"].forEach(function (t) {
      if (!groups[t].length) return;
      var cand = groups[t][0];
      if (!best || cand.sc < best.sc ||
        (cand.sc === best.sc && TYPE_RANK[t] < TYPE_RANK[best.type])) {
        best = { e: cand.e, type: t, sc: cand.sc };
      }
    });
    return best;
  }

  /* P0-b routing: decide where a SUBMITTED query should navigate.
     - exactly one EXACT PART (sc===1) -> product page
       (covers unique MPN / slug / clean form; also when a slug is an exact
        match for one SKU yet a prefix of another — still goes direct)
     - any other PART match (series / family / partial / single non-exact) -> Results Page
     - no PART but exactly one auxiliary entity -> that entity's page (MFR/SUB/CAT)
     - no PART and 0 / >1 auxiliary -> Search Results Page (also handles no-results)
     Never silently picks one SKU for a series / family / partial query. */
  function decideSubmit(q) {
    var groups = search(q);
    var parts = groups.PART || [];
    var exact = parts.filter(function (x) { return x.sc === 1; });
    if (exact.length === 1) {
      return exact[0].e.url;                                   // Case B
    }
    if (parts.length >= 1) {
      return "/search/?q=" + encodeURIComponent(q); // Case C / D
    }
    var aux = [];
    ["MANUFACTURER", "SUBCATEGORY", "CATEGORY"].forEach(function (t) {
      (groups[t] || []).forEach(function (x) { aux.push(x); });
    });
    if (aux.length === 1) {
      return aux[0].e.url;                                     // Case E (single nav)
    }
    return "/search/?q=" + encodeURIComponent(q);   // Case E (multi) / F (no results)
  }

  /* ---------- browse: progressive enhancement only ----------
     The category/subcategory catalog is server-rendered statically in
     components/index.html — all 26 subcat <a href> already exist in the
     initial DOM, so Google discovers them without executing JS.
     V2.4 layout: a compact sticky left nav (smooth-scrolls to + highlights
     the matching right-hand section, never navigates) plus every category
     section rendered on the right with its subcategories; each section
     header carries a dropdown toggle (chevron) that collapses/expands its
     subcategory grid. No innerHTML — toggles only flip classes/aria.
     The search core below (search/score/partAliases/decideSubmit/
     renderResults/bestOverall/SZSearchCore) is untouched. */
  function renderCatalog(root) {
    if (!root) return;

    // Left nav -> smooth-scroll to the section + mark active (never navigates)
    root.querySelectorAll(".catalog-nav-item").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var cat = btn.getAttribute("data-category");
        var sec = root.querySelector('.catalog-section[data-category="' + cat + '"]');
        root.querySelectorAll(".catalog-nav-item").forEach(function (b) {
          b.classList.toggle("active", b === btn);
        });
        if (sec) {
          sec.scrollIntoView({ behavior: "smooth", block: "start" });
          var subs = sec.querySelector(".catalog-subs");
          var tog = sec.querySelector(".catalog-toggle");
          if (subs && subs.classList.contains("collapsed")) {
            subs.classList.remove("collapsed");
            if (tog) tog.setAttribute("aria-expanded", "true");
          }
        }
      });
    });

    // Section header dropdown toggles -> collapse / expand subcategory grid
    root.querySelectorAll(".catalog-toggle").forEach(function (tog) {
      tog.addEventListener("click", function (e) {
        if (e) e.preventDefault();
        var sec = tog.closest(".catalog-section");
        var subs = sec ? sec.querySelector(".catalog-subs") : null;
        if (!subs) return;
        var collapsed = subs.classList.toggle("collapsed");
        tog.setAttribute("aria-expanded", collapsed ? "false" : "true");
      });
    });
  }

  /* ---------- init ---------- */
  function init() {
    var box = document.getElementById("searchResults");
    var input = document.getElementById("compSearchInput");
    var form = document.getElementById("compSearch");
    var browse = document.getElementById("catBrowse");

    if (browse) renderCatalog(browse);

    if (input && box) {
      var timer = null;
      input.addEventListener("input", function () {
        clearTimeout(timer);
        var v = input.value;
        timer = setTimeout(function () { renderResults(v, search(v), box); }, 120);
      });
      if (form) {
        form.addEventListener("submit", function (e) {
          e.preventDefault();
          var q = input.value.trim();
          if (!q) { input.focus(); return; }                 // Case A: empty -> stay
          var url = decideSubmit(q);
          if (url) window.location.href = url;
        });
      }
    }

    // support ?q= deep link from global nav search -> prefill + run
    try {
      var params = new URLSearchParams(window.location.search);
      var q = params.get("q");
      if (q && input) {
        input.value = q;
        renderResults(q, search(q), box);
        var sec = document.getElementById("search");
        if (sec) sec.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    } catch (e) {}
  }

  /* P0-b: expose the single search core so the Search Results Page reuses it
     instead of re-implementing a second norm/score/search/acronyms pipeline. */
  window.SZSearchCore = {
    search: search,
    score: score,
    scoreEntry: scoreEntry,
    norm: norm,
    acronyms: acronyms,
    partAliases: partAliases,
    bestOverall: bestOverall,
    decideSubmit: decideSubmit,
    IDX: IDX,
    DATA: DATA
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
