/* =========================================================
   SZ Procure — Search Results Page logic (P0-b)
   Reuses the SINGLE search core exposed by hub.js
   (window.SZSearchCore.search / score / partAliases / DATA).
   No second norm/score/acronyms pipeline is implemented here.
   - Renders PART results as the absolute main list.
   - MFR / CATEGORY / SUBCATEGORY are only "Related matches".
   - Pagination via ?page= (URL-driven, survives refresh, works pre-JS).
   - 0 results -> "No matching parts found" + Request a Quote (?pn=).
   - All user input is HTML-escaped (XSS-safe).
   - No price / stock / buy-now / cart anywhere.
   ========================================================= */
(function () {
  "use strict";

  var PAGE_SIZE = 20; // first version: 20 per page; URL-driven for 5k/10k/100k+ scale

  var core = window.SZSearchCore;

  // slug/url -> part lookup for best-effort Category display
  var partByUrl = {};
  var subcatToCat = {};
  if (core && core.DATA) {
    (core.DATA.parts || []).forEach(function (p) { if (p.url) partByUrl[p.url] = p; });
    (core.DATA.categories || []).forEach(function (c) {
      (c.subcategories || []).forEach(function (s) { subcatToCat[s.name] = c.name; });
    });
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function catOf(p) {
    if (p && p.cat && String(p.cat).trim()) return String(p.cat).trim();
    if (p && p.subcat && subcatToCat[p.subcat]) return subcatToCat[p.subcat];
    return "";
  }

  function render() {
    var headEl = document.getElementById("resultHead");
    var partsEl = document.getElementById("partResults");
    var pageEl = document.getElementById("pagination");
    var relEl = document.getElementById("relatedMatches");
    var emptyEl = document.getElementById("emptyState");
    if (!headEl || !partsEl) return; // safety

    if (!core) {
      partsEl.innerHTML = '<p class="search-prompt">Search is temporarily unavailable.</p>';
      return;
    }

    var params = new URLSearchParams(window.location.search);
    var q = (params.get("q") || "").trim();
    var page = parseInt(params.get("page") || "1", 10);
    if (!page || page < 1) page = 1;

    // prefill the search box from the URL (only if empty — user may have typed)
    var input = document.getElementById("searchInput");
    if (input && !input.value) input.value = q;

    // No query at all -> gentle prompt, nothing else.
    if (!q) {
      headEl.innerHTML = "";
      partsEl.innerHTML = '<p class="search-prompt">Enter a part number (MPN), manufacturer or category to search our 552 sourced components.</p>';
      pageEl.innerHTML = "";
      relEl.innerHTML = "";
      emptyEl.innerHTML = "";
      return;
    }

    var groups = core.search(q);
    var parts = groups.PART || [];
    var n = parts.length;

    // Head: "Search results for "X"" + accurate count
    var countText = n === 0 ? "No matching parts" : (n === 1 ? "1 matching part" : (n + " matching parts"));
    headEl.innerHTML =
      '<h2 class="results-title">Search results for &ldquo;' + esc(q) + '&rdquo;</h2>' +
      '<p class="results-count">' + esc(countText) + '</p>';

    // No PART matches -> empty state (NOT a silent RFQ redirect)
    if (n === 0) {
      partsEl.innerHTML = "";
      pageEl.innerHTML = "";
      relEl.innerHTML = renderRelated(groups);
      var pn = encodeURIComponent(q);
      emptyEl.innerHTML =
        '<div class="sr-empty-page">' +
          '<h3>No matching parts found.</h3>' +
          '<p>Can&rsquo;t find the part you&rsquo;re looking for?</p>' +
          '<p>Send us the part number, manufacturer and quantity &mdash; we&rsquo;ll check sourcing.</p>' +
          '<a class="btn btn-primary" href="/request-a-quote/?pn=' + pn + '">Request a Quote</a>' +
        '</div>';
      return;
    }
    emptyEl.innerHTML = "";

    // Pagination math
    var totalPages = Math.max(1, Math.ceil(n / PAGE_SIZE));
    if (page > totalPages) page = totalPages;
    var start = (page - 1) * PAGE_SIZE;
    var pageItems = parts.slice(start, start + PAGE_SIZE);

    // PART main list
    var rows = "";
    pageItems.forEach(function (item) {
      var e = item.e;
      var p = partByUrl[e.url] || null;
      var cat = catOf(p);
      var subline = [e.mfr, (cat ? (cat + " · " + e.subcat) : e.subcat)]
        .filter(Boolean).join("  ·  ");
      rows +=
        '<a class="part-row" href="' + esc(e.url) + '">' +
          '<span class="part-mpn">' + esc(e.label) + '</span>' +
          '<span class="part-meta">' + esc(subline) + '</span>' +
          '<span class="part-go" aria-hidden="true">&rarr;</span>' +
        '</a>';
    });
    partsEl.innerHTML = '<div class="part-list">' + rows + '</div>';

    pageEl.innerHTML = renderPagination(q, page, totalPages);
    relEl.innerHTML = renderRelated(groups);
  }

  function renderPagination(q, page, totalPages) {
    if (totalPages <= 1) return "";
    var enc = encodeURIComponent(q);
    var html = '<nav class="pager" aria-label="Pagination">';
    if (page > 1) {
      html += '<a class="pager-prev" href="/components/search/?q=' + enc + '&page=' + (page - 1) + '">&lsaquo; Prev</a>';
    }
    // windowed page numbers: first, last, current +-2
    var win = [];
    for (var i = 1; i <= totalPages; i++) {
      if (i === 1 || i === totalPages || Math.abs(i - page) <= 2) {
        if (win.length && i - win[win.length - 1] > 1) win.push("…");
        win.push(i);
      }
    }
    win.forEach(function (i) {
      if (i === "…") {
        html += '<span class="pager-gap">&hellip;</span>';
      } else if (i === page) {
        html += '<span class="pager-cur" aria-current="page">' + i + '</span>';
      } else {
        html += '<a class="pager-num" href="/components/search/?q=' + enc + '&page=' + i + '">' + i + '</a>';
      }
    });
    if (page < totalPages) {
      html += '<a class="pager-next" href="/components/search/?q=' + enc + '&page=' + (page + 1) + '">Next &rsaquo;</a>';
    }
    html += '</nav>';
    return html;
  }

  function renderRelated(groups) {
    var defs = [
      { t: "MANUFACTURER", title: "Manufacturers" },
      { t: "CATEGORY", title: "Categories" },
      { t: "SUBCATEGORY", title: "Subcategories" }
    ];
    var blocks = [];
    defs.forEach(function (d) {
      var list = (groups[d.t] || []).slice(0, 8);
      if (!list.length) return;
      var items = list.map(function (item) {
        return '<a class="rel-item" href="' + esc(item.e.url) + '">' + esc(item.e.label) + '</a>';
      }).join("");
      blocks.push(
        '<div class="rel-block"><div class="rel-head">' + d.title + '</div>' +
        '<div class="rel-items">' + items + '</div></div>'
      );
    });
    if (!blocks.length) return "";
    return '<div class="related"><div class="rel-title">Related matches</div>' + blocks.join("") + '</div>';
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", render);
  } else {
    render();
  }
})();
