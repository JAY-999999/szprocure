/* =========================================================
   SZ Procure — shared front-end logic
   - injects nav + footer (root-relative links)
   - EN / 中文 language switcher (data-zh / data-zh-ph) — DISABLED in production (English-only)
   - mobile menu toggle
   - part-number search -> request-a-quote prefill
   - quote form: client validation + success state
   - BOM file upload UI
   Paths are root-relative ("/...") so the site works under a
   static server and on any host (e.g. /components, /ai-hardware).
   ========================================================= */
(function () {
  "use strict";

  var BRAND = "SZ Procure";
  var DOMAIN = "szprocure.com";
  var EMAIL = "sales@" + DOMAIN;
  var WHATSAPP = "+86 13530888389";
  var ADDRESS = "14th Floor, Guangye Building, 110 Fuhua Road, Futian District, Shenzhen, Guangdong, China";

  var LOGO = '<svg class="logo-mark" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
    + '<rect x="3" y="3" width="26" height="26" rx="7" fill="#0b1b33"/>'
    + '<rect x="9" y="9" width="14" height="14" rx="3" fill="#0A84FF"/>'
    + '<path d="M16 3v5M16 24v5M3 16h5M24 16h5" stroke="#0A84FF" stroke-width="2" stroke-linecap="round"/>'
    + '<circle cx="16" cy="16" r="2.4" fill="#fff"/></svg>';

  function isLocalHost() {
    var h = location.hostname;
    return h === '127.0.0.1' || h === 'localhost';
  }
  var LANG_SWITCH = (isLocalHost() ? (
    '<div class="lang-switch" role="group" aria-label="Language">'
    + '<button type="button" class="lang-btn" data-lang="en">EN</button>'
    + '</div>'
  ) : ''); // English-only on production host; underlying data-zh multilingual capability retained (masked in EN view)

  var NAV = ''
    + '<header class="site-header"><div class="container nav">'
    + '<a class="brand" href="/" aria-label="' + BRAND + ' home">' + LOGO
    +   '<span>SZ Procure</span></a>'
    + '<nav class="nav-links" aria-label="Primary">'
    +   '<a href="/components/" data-zh="元器件">Components</a>'
    +   '<a href="/ai-hardware/" data-zh="AI 硬件">AI Hardware</a>'
    +   '<a href="/sourcing-service/" data-zh="采购服务">Sourcing Service</a>'
    +   '<a href="/about/" data-zh="关于我们">About</a>'
    + '</nav>'
    + '<form class="nav-search" id="globalSearch" role="search" action="/components/">'
    +   '<input type="search" name="q" placeholder="Search Part Number, MPN or Keyword" aria-label="Search Part Number, MPN or Keyword" data-zh-ph="搜索料号、型号或关键词" />'
    + '</form>'
    + '<div class="nav-cta">'
    +   LANG_SWITCH
    +   '<a class="btn btn-primary" href="/request-a-quote/" data-zh="获取报价">Request a Quote</a>'
    +   '<button class="nav-toggle" id="navToggle" aria-label="Menu"><span></span><span></span><span></span></button>'
    + '</div></div>'
    + '<div class="mobile-menu" id="mobileMenu">'
    +   LANG_SWITCH
    +   '<form class="nav-search mobile" role="search" action="/components/">'
    +     '<input type="search" name="q" placeholder="Search Part Number, MPN or Keyword" aria-label="Search Part Number, MPN or Keyword" data-zh-ph="搜索料号、型号或关键词" />'
    +   '</form>'
    +   '<a href="/components/" data-zh="元器件">Components</a>'
    +   '<a href="/ai-hardware/" data-zh="AI 硬件">AI Hardware</a>'
    +   '<a href="/sourcing-service/" data-zh="采购服务">Sourcing Service</a>'
    +   '<a href="/about/" data-zh="关于我们">About</a>'
    +   '<a class="btn btn-primary btn-block" href="/request-a-quote/" data-zh="获取报价">Request a Quote</a>'
    + '</div></header>';

  var FOOTER = ''
    + '<footer class="site-footer"><div class="container">'
    + '<div class="footer-grid">'
    +   '<div><a class="brand" href="/">' + LOGO + '<span>SZ Procure</span></a>'
    +     '<p style="margin-top:14px;color:#aebfda;max-width:34ch;font-size:.92rem;" data-zh="为全球客户提供电子元器件与 AI 硬件采购解决方案，连接可靠供应资源。">Providing global buyers with reliable electronic components and AI hardware sourcing solutions.</p></div>'
    +   '<div><h4 data-zh="服务">Services</h4>'
    +     '<a href="/components/" data-zh="元器件">Components</a>'
    +     '<a href="/ai-hardware/" data-zh="AI 硬件">AI Hardware</a>'
    +     '<a href="/sourcing-service/" data-zh="采购服务">Sourcing Service</a></div>'
+   '<div><h4 data-zh="公司">Company</h4>'
+     '<a href="/about/" data-zh="关于我们">About</a>'
+     '<a href="/contact/" data-zh="联系我们">Contact</a>'
+     '<a href="/privacy/" data-zh="隐私政策">Privacy Policy</a>'
+     '<a href="/terms/" data-zh="服务条款">Terms of Service</a></div>'
    +   '<div class="footer-contact"><h4 data-zh="联系方式">Contact</h4>'
    +     '<p data-zh="邮箱：' + EMAIL + '">Email: ' + EMAIL + '</p>'
    +     '<p data-zh="WhatsApp：' + WHATSAPP + '">WhatsApp: ' + WHATSAPP + '</p>'
    +     '<p data-zh="地址：中国广东省深圳市福田区福华路110号广业大厦14楼">Address: ' + ADDRESS + '</p></div>'
    + '</div>'
    + '<div class="footer-bottom"><span data-zh="© 2026 SZ Procure。保留所有权利。">&copy; 2026 ' + BRAND + '. All rights reserved.</span>'
    +   '<span data-zh="全球电子元器件与 AI 硬件采购合作伙伴">Global Electronic Components Sourcing Partner</span></div>'
    + '</div></footer>';

  /* Mobile fixed bottom CTA bar (Request Quote + WhatsApp) — injected on all pages */
  var MOBILE_CTA = ''
    + '<div class="mobile-cta-bar" aria-label="Quick contact">'
    +   '<a class="btn btn-primary" href="/request-a-quote/"><span data-zh="获取报价">Request a Quote</span></a>'
    +   '<a class="btn btn-ghost" href="https://wa.me/8613530888389" target="_blank" rel="noopener"><span data-zh="WhatsApp">WhatsApp</span></a>'
    + '</div>';

  /* Desktop floating Quick RFQ button — hidden on mobile via CSS */
  var FLOAT_CTA = ''
    + '<a class="float-quote-btn" href="/request-a-quote/" aria-label="Quick RFQ">'
    +   '<span data-zh="快速询价">Quick RFQ</span>'
    + '</a>';

  /* ---------- i18n ---------- */
  function getLang() {
    if (isLocalHost()) { return localStorage.getItem("sz_lang") || "en"; }
    return "en"; // production: force English; i18n switch disabled
  }
  function applyLang(lang) {
    document.documentElement.lang = (lang === "zh") ? "zh-CN" : "en";
    document.querySelectorAll("[data-zh]").forEach(function (el) {
      if (el.__orig === undefined) el.__orig = el.textContent;
      var zh = el.getAttribute("data-zh");
      el.textContent = (lang === "zh") ? (zh || el.__orig) : el.__orig;
    });
    document.querySelectorAll("[data-zh-ph]").forEach(function (el) {
      if (el.__ph === undefined) el.__ph = el.getAttribute("placeholder") || "";
      var zh = el.getAttribute("data-zh-ph");
      el.setAttribute("placeholder", (lang === "zh") ? (zh || el.__ph) : el.__ph);
    });
    try { localStorage.setItem("sz_lang", lang); } catch (e) {}
    updateToggle(lang);
  }
  function updateToggle(lang) {
    document.querySelectorAll(".lang-btn").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-lang") === lang);
    });
  }
  function bindLang() {
    document.querySelectorAll(".lang-btn").forEach(function (b) {
      b.addEventListener("click", function () { applyLang(b.getAttribute("data-lang")); });
    });
  }

  function inject() {
    var h = document.getElementById("site-header");
    var f = document.getElementById("site-footer");
    if (h) h.outerHTML = NAV;
    if (f) f.outerHTML = FOOTER;
    document.body.insertAdjacentHTML("beforeend", MOBILE_CTA + FLOAT_CTA);
    bindMenu();
    bindLang();
    bindSearch();
    bindGlobalSearch();
    bindQuoteForm();
    bindUpload();
    prefillFromQuery();
    bindChangePart();
    bindQuotePrefill();
    detectCountry();
    applyLang(getLang());
  }

  function bindMenu() {
    var t = document.getElementById("navToggle");
    var m = document.getElementById("mobileMenu");
    if (t && m) t.addEventListener("click", function () { m.classList.toggle("open"); });
  }

  function bindSearch() {
    document.querySelectorAll("[data-part-search]").forEach(function (form) {
      form.addEventListener("submit", function (e) {
        e.preventDefault();
        var input = form.querySelector("input");
        var v = input && input.value.trim();
        if (v) {
          window.location.href = "/request-a-quote/?pn=" + encodeURIComponent(v);
        } else if (input) {
          input.focus();
        }
      });
    });
  }

  /* Global nav search -> /search/?q= (form action handles submit; guard empty) */
  function bindGlobalSearch() {
    document.querySelectorAll("#globalSearch").forEach(function (form) {
      form.addEventListener("submit", function (e) {
        var input = form.querySelector("input");
        if (!input || !input.value.trim()) {
          e.preventDefault();
          input && input.focus();
        }
      });
    });
  }

  /* Auto-fill RFQ fields from product-page deep links (?pn=&mfr=&cat=&source=).
     Phase 1: pre-fill product identity fields + show context banner + hint.
     Fields stay EDITABLE so buyers can request alternatives or adjust the part.
     Phase 2 will later collapse the form into a 2-field quick mode. */
  /* Auto-detect visitor country/region from browser timezone + locale (static-site safe, no IP lookup).
     Tags country_source=auto; a user override flips it to manual in the country input listener below. */
  function detectCountry() {
    var el = document.getElementById("country");
    if (!el || el.value.trim()) return;
    var country = "", src = "auto";
    try {
      var tz = (Intl.DateTimeFormat().resolvedOptions().timeZone) || "";
      var TZ = {
        "Asia/Shanghai": "China", "Asia/Hong_Kong": "Hong Kong, China", "Asia/Taipei": "Taiwan, China",
        "Asia/Macau": "Macao, China", "Asia/Tokyo": "Japan", "Asia/Seoul": "South Korea", "Asia/Singapore": "Singapore",
        "Asia/Kolkata": "India", "Asia/Kuala_Lumpur": "Malaysia", "Asia/Bangkok": "Thailand", "Asia/Ho_Chi_Minh": "Vietnam",
        "Europe/Berlin": "Germany", "Europe/London": "United Kingdom", "Europe/Paris": "France", "Europe/Amsterdam": "Netherlands",
        "Europe/Madrid": "Spain", "Europe/Rome": "Italy", "Europe/Stockholm": "Sweden", "Europe/Zurich": "Switzerland",
        "America/New_York": "United States", "America/Chicago": "United States", "America/Denver": "United States", "America/Los_Angeles": "United States",
        "America/Toronto": "Canada", "America/Mexico_City": "Mexico", "America/Sao_Paulo": "Brazil", "Australia/Sydney": "Australia"
      };
      if (TZ[tz]) {
        country = TZ[tz];
      } else {
        var loc = (navigator.language || "").split("-")[1] || (navigator.language || "").split("_")[1] || "";
        var REGION = {
          "US": "United States", "CN": "China", "HK": "Hong Kong, China", "TW": "Taiwan, China", "JP": "Japan",
          "KR": "South Korea", "SG": "Singapore", "IN": "India", "MY": "Malaysia", "TH": "Thailand", "VN": "Vietnam",
          "DE": "Germany", "GB": "United Kingdom", "FR": "France", "NL": "Netherlands", "ES": "Spain", "IT": "Italy",
          "SE": "Sweden", "CH": "Switzerland", "CA": "Canada", "MX": "Mexico", "BR": "Brazil", "AU": "Australia", "RU": "Russia"
        };
        if (REGION[loc.toUpperCase()]) country = REGION[loc.toUpperCase()];
      }
    } catch (e) {}
    if (country) {
      el.value = country;
      var csEl = document.getElementById("country_source");
      if (csEl) csEl.value = src;
      var hint = document.getElementById("country-auto-hint");
      if (hint) hint.hidden = false;
    }
  }

  function prefillFromQuery() {
    var params = new URLSearchParams(window.location.search);
    var p = params.get("pn");
    var m = params.get("mfr");
    var c = params.get("cat");
    var src = params.get("source");

    var pnEl = document.getElementById("part_number");
    if (pnEl && p) pnEl.value = p;

    var mfrEl = document.getElementById("manufacturer");
    if (mfrEl && m) mfrEl.value = m;

    var catEl = document.getElementById("rfq_category");
    if (catEl && c) catEl.value = c;

    // SKU-origin RFQ: tag business type so future CRM/analytics know the source
    var typeEl = document.getElementById("rfq_type");
    if (typeEl) typeEl.value = (src === "product") ? "sku_quote" : typeEl.value;

    if (p || m) {
      var details = document.getElementById("rfq-sku-details");
      if (details) {
        var html = "";
        if (p) html += '<p class="ctx-part"><strong>' + escapeHtml(p) + "</strong></p>";
        if (m) html += '<p class="ctx-mfr">' + escapeHtml(m) + "</p>";
        details.innerHTML = html;
      }
      var box = document.getElementById("rfq-sku-context");
      if (box) box.hidden = false;
      var hint = document.getElementById("rfq-autofill-hint");
      if (hint) hint.hidden = false;
      // SKU mode: show read-only context, hide the editable part fields until "Change Part"
      var edit = document.getElementById("rfq-part-edit");
      if (edit) edit.hidden = true;
      var pnReq = document.getElementById("pn-req");
      if (pnReq) pnReq.hidden = true;
    }
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  /* SKU mode: "Change Part" reveals the editable part/manufacturer fields + marks required */
  function bindChangePart() {
    var btn = document.getElementById("rfqChangePart");
    if (!btn) return;
    btn.addEventListener("click", function () {
      var banner = document.getElementById("rfq-sku-context");
      var edit = document.getElementById("rfq-part-edit");
      var pnReq = document.getElementById("pn-req");
      if (banner) banner.hidden = true;
      if (edit) edit.hidden = false;
      if (pnReq) pnReq.hidden = false;
      var pn = document.getElementById("part_number");
      if (pn) pn.focus();
    });
  }

  /* Remember contact fields locally so repeat RFQ visitors don't re-type */
  function bindQuotePrefill() {
    var form = document.getElementById("quote-form");
    if (!form) return;
    var fields = {
      contact_name: "sz_rfq_contact",
      company: "sz_rfq_company",
      email: "sz_rfq_email",
      country: "sz_rfq_country"
    };
    Object.keys(fields).forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) return;
      try {
        var saved = localStorage.getItem(fields[id]);
        if (saved && !el.value.trim()) el.value = saved;
      } catch (e) {}
      el.addEventListener("input", function () {
        try { localStorage.setItem(fields[id], el.value); } catch (e) {}
        if (id === "country") {
          var s = document.getElementById("country_source");
          if (s) s.value = el.value.trim() ? "manual" : "auto";
        }
      });
    });
  }

  function bindQuoteForm() {
    var form = document.getElementById("quote-form");
    if (!form) return;
    var success = document.getElementById("formSuccess");
    var emailErr = form.querySelector("#formError");
    var submitErr = form.querySelector("#formSubmitError");
    var btn = form.querySelector('button[type="submit"]');

    function clearErrors() {
      if (emailErr) { emailErr.textContent = ""; emailErr.style.display = "none"; }
      if (submitErr) { submitErr.textContent = ""; submitErr.style.display = "none"; }
    }

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      clearErrors();

      // 0) Trim all text inputs / textareas / selects, then fill tracking fields
      form.querySelectorAll("input:not([type=file]), textarea, select").forEach(function (el) {
        if (el.value) el.value = el.value.trim();
      });
      var srcEl = document.getElementById("source_url");
      if (srcEl) srcEl.value = window.location.href;
      var typeEl = document.getElementById("rfq_type");
      if (typeEl) {
        var fEl = form.querySelector('input[type=file]');
        var hasBom = false;
        if (fEl && fEl.files) {
          for (var i = 0; i < fEl.files.length; i++) {
            if (/\.(xls|xlsx|csv)$/i.test(fEl.files[i].name)) { hasBom = true; break; }
          }
        }
        if (hasBom) typeEl.value = "bom_quote";
      }

      // 0a) System-collected tracking fields (not customer-entered)
      var referrerEl = document.getElementById("referrer");
      if (referrerEl) referrerEl.value = document.referrer || "";
      var stEl = document.getElementById("submitted_at");
      if (stEl) stEl.value = new Date().toISOString();
      var csEl = document.getElementById("country_source");
      if (csEl) {
        var cEl = document.getElementById("country");
        if (cEl && cEl.value.trim() && !csEl.value) csEl.value = "manual";
      }
      var rtEl = document.getElementById("requirement_type");
      if (rtEl && typeEl) {
        rtEl.value = (typeEl.value === "bom_quote") ? "bom"
          : (typeEl.value === "sku_quote") ? "sku" : "general";
      }

      // 0b) Quantity must be a whole number >= 1 (only when provided — non-blocking)
      var qtyEl = form.querySelector("#quantity");
      if (qtyEl && qtyEl.value) {
        var qn = Number(qtyEl.value);
        if (!Number.isInteger(qn) || qn < 1) {
          if (submitErr) {
            submitErr.textContent = (getLang() === "zh")
              ? "请输入有效的数量（正整数，不低于 1）。"
              : "Please enter a valid quantity (whole number, at least 1).";
            submitErr.style.display = "block";
          }
          if (qtyEl) qtyEl.classList.add("invalid");
          return;
        }
      }

      // 1) Email format check
      var email = form.querySelector("#email");
      if (email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.value.trim())) {
        if (emailErr) {
          emailErr.textContent = (getLang() === "zh") ? "请输入有效的企业邮箱。" : "Please enter a valid business email.";
          emailErr.style.display = "block";
        }
        email.focus();
        return;
      }

      // 1c) Part number required only for general (non-SKU) requests
      if (typeEl && typeEl.value === "general_quote") {
        var pnChk = document.getElementById("part_number");
        if (pnChk && !pnChk.value.trim()) {
          if (submitErr) {
            submitErr.textContent = (getLang() === "zh")
              ? "请填写料号或产品名称。"
              : "Please enter a part number or product name.";
            submitErr.style.display = "block";
          }
          if (pnChk) pnChk.classList.add("invalid");
          if (pnChk) pnChk.focus();
          return;
        }
      }

      // 2) Required fields check
      var ok = true;
      form.querySelectorAll("[required]").forEach(function (el) {
        if (!el.value.trim()) { ok = false; el.classList.add("invalid"); }
        else { el.classList.remove("invalid"); }
      });
      if (!ok) {
        if (submitErr) {
          submitErr.textContent = (getLang() === "zh") ? "请填写带 * 的必填项。" : "Please complete the required fields.";
          submitErr.style.display = "block";
        }
        return;
      }

      // 3) Submit to third-party endpoint (Formsubmit.co → sales@szprocure.com)
      if (btn) { btn.disabled = true; btn.dataset.label = btn.textContent; btn.textContent = (getLang() === "zh") ? "提交中…" : "Submitting…"; }
      var data = new FormData(form);
      fetch(form.getAttribute("action"), {
        method: "POST",
        body: data,
        headers: { "Accept": "application/json" }
      }).then(function (res) {
        if (res.ok) {
          if (success) { success.classList.add("show"); form.style.display = "none"; success.scrollIntoView({ behavior: "smooth", block: "center" }); }
        } else {
          throw new Error("bad-status");
        }
      }).catch(function () {
        if (btn) { btn.disabled = false; btn.textContent = btn.dataset.label || "Submit RFQ"; }
        if (submitErr) {
          submitErr.textContent = (getLang() === "zh")
            ? "提交失败，请稍后重试，或直接将需求发邮件至 sales@szprocure.com。"
            : "Submission failed. Please try again, or email sales@szprocure.com directly.";
          submitErr.style.display = "block";
        }
      });
    });
  }

  function bindUpload() {
    document.querySelectorAll("[data-upload]").forEach(function (box) {
      var input = box.querySelector("input[type=file]");
      var list = box.parentElement.querySelector("[data-file-list]");
      var trigger = box.querySelector("[data-upload-trigger]");
      if (trigger && input) trigger.addEventListener("click", function (e) { e.preventDefault(); input.click(); });
      ["dragover", "dragenter"].forEach(function (ev) {
        box.addEventListener(ev, function (e) { e.preventDefault(); box.classList.add("drag"); });
      });
      ["dragleave", "drop"].forEach(function (ev) {
        box.addEventListener(ev, function (e) { e.preventDefault(); box.classList.remove("drag"); });
      });
      box.addEventListener("drop", function (e) {
        if (e.dataTransfer && input) input.files = e.dataTransfer.files;
        render();
      });
      if (input) input.addEventListener("change", render);
      function render() {
        if (!list || !input) return;
        list.innerHTML = "";
        Array.prototype.forEach.call(input.files, function (f) {
          var d = document.createElement("div");
          d.textContent = "– " + f.name + " (" + (f.size / 1024).toFixed(0) + " KB)";
          list.appendChild(d);
        });
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", inject);
  } else {
    inject();
  }
})();
