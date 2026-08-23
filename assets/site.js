/* =========================================================
   SZ Procure — shared front-end logic
   - injects nav + footer (root-relative links)
   - EN / 中文 language switcher (data-zh / data-zh-ph)
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
  var EMAIL = "jay@" + DOMAIN;
  var WHATSAPP = "+86 13530888389";
  var ADDRESS = "110 Fuhua Road, Futian District, Shenzhen, China";

  var LOGO = '<svg class="logo-mark" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
    + '<rect x="3" y="3" width="26" height="26" rx="7" fill="#0b1b33"/>'
    + '<rect x="9" y="9" width="14" height="14" rx="3" fill="#0A84FF"/>'
    + '<path d="M16 3v5M16 24v5M3 16h5M24 16h5" stroke="#0A84FF" stroke-width="2" stroke-linecap="round"/>'
    + '<circle cx="16" cy="16" r="2.4" fill="#fff"/></svg>';

  var LANG_SWITCH = '<div class="lang-switch" role="group" aria-label="Language">'
    + '<button type="button" class="lang-btn" data-lang="en">EN</button>'
    + '<span class="lang-sep">|</span>'
    + '<button type="button" class="lang-btn" data-lang="zh">中文</button>'
    + '</div>';

  var NAV = ''
    + '<header class="site-header"><div class="container nav">'
    + '<a class="brand" href="/" aria-label="' + BRAND + ' home">' + LOGO
    +   '<span>SZ Procure<small data-zh="深圳采购">Shenzhen Sourcing</small></span></a>'
    + '<nav class="nav-links" aria-label="Primary">'
    +   '<a href="/components/" data-zh="元器件">Components</a>'
    +   '<a href="/ai-hardware/" data-zh="AI 硬件">AI Hardware</a>'
    +   '<a href="/sourcing-service/" data-zh="采购服务">Sourcing Service</a>'
    +   '<a href="/about/" data-zh="关于我们">About</a>'
    + '</nav>'
    + '<form class="nav-search" id="globalSearch" role="search" action="/components/">'
    +   '<input type="search" name="q" placeholder="Search part number…" aria-label="Search part number" data-zh-ph="搜索料号 / 型号…" />'
    + '</form>'
    + '<div class="nav-cta">'
    +   LANG_SWITCH
    +   '<a class="btn btn-primary" href="/request-a-quote/" data-zh="获取报价">Request a Quote</a>'
    +   '<button class="nav-toggle" id="navToggle" aria-label="Menu"><span></span><span></span><span></span></button>'
    + '</div></div>'
    + '<div class="mobile-menu" id="mobileMenu">'
    +   LANG_SWITCH
    +   '<form class="nav-search mobile" role="search" action="/components/">'
    +     '<input type="search" name="q" placeholder="Search part number…" aria-label="Search part number" data-zh-ph="搜索料号 / 型号…" />'
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
    +   '<div><a class="brand" href="/">' + LOGO + '<span>SZ Procure<small data-zh="深圳采购">Shenzhen Sourcing</small></span></a>'
    +     '<p style="margin-top:14px;color:#aebfda;max-width:34ch;font-size:.92rem;" data-zh="来自中国的电子元器件与 AI 硬件采购服务。立足深圳，连接供应链。">Electronics &amp; AI Hardware Sourcing from China. Based in Shenzhen, connected to the supply chain.</p></div>'
    +   '<div><h4 data-zh="服务">Services</h4>'
    +     '<a href="/components/" data-zh="元器件">Components</a>'
    +     '<a href="/ai-hardware/" data-zh="AI 硬件">AI Hardware</a>'
    +     '<a href="/sourcing-service/" data-zh="采购服务">Sourcing Service</a></div>'
    +   '<div><h4 data-zh="公司">Company</h4>'
    +     '<a href="/about/" data-zh="关于我们">About</a>'
    +     '<a href="/contact/" data-zh="联系我们">Contact</a>'
    +     '<a href="/request-a-quote/" data-zh="获取报价">Request a Quote</a>'
    +     '<a href="/privacy/" data-zh="隐私政策">Privacy Policy</a>'
    +     '<a href="/terms/" data-zh="服务条款">Terms of Service</a></div>'
    +   '<div class="footer-contact"><h4 data-zh="联系方式">Contact</h4>'
    +     '<p><a href="mailto:' + EMAIL + '">' + EMAIL + '</a></p>'
    +     '<p data-zh="WhatsApp：+86 13530888389">WhatsApp: ' + WHATSAPP + '</p>'
    +     '<p data-zh="中国深圳福田区福华路110号">' + ADDRESS + '</p></div>'
    + '</div>'
    + '<div class="footer-bottom"><span data-zh="© 2026 SZ Procure。保留所有权利。">&copy; 2026 ' + BRAND + '. All rights reserved.</span>'
    +   '<span data-zh="来自中国的电子元器件与 AI 硬件采购服务">Electronics &amp; AI Hardware Sourcing from China</span></div>'
    + '</div></footer>';

  /* ---------- i18n ---------- */
  function getLang() {
    try { return localStorage.getItem("sz_lang") || "en"; }
    catch (e) { return "en"; }
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
    bindMenu();
    bindLang();
    bindSearch();
    bindGlobalSearch();
    bindQuoteForm();
    bindUpload();
    prefillFromQuery();
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

  function prefillFromQuery() {
    var p = new URLSearchParams(window.location.search).get("pn");
    if (p) {
      var el = document.getElementById("part_number");
      if (el) el.value = p;
    }
  }

  function bindQuoteForm() {
    var form = document.getElementById("quote-form");
    if (!form) return;
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var email = form.querySelector("#email");
      var err = form.querySelector("#formError");
      if (email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.value.trim())) {
        if (err) {
          err.textContent = (getLang() === "zh") ? "请输入有效的企业邮箱。" : "Please enter a valid business email.";
          err.style.display = "block";
        }
        email.focus();
        return;
      }
      if (err) err.style.display = "none";
      var success = document.getElementById("formSuccess");
      if (success) success.classList.add("show");
      form.style.display = "none";
      if (success) success.scrollIntoView({ behavior: "smooth", block: "center" });
      /* MVP: no backend. Structure below is API/CRM-ready.
         payload = {
           customer_name, company, email, country, phone,
           request_type, part_number, quantity, target_price,
           delivery_date, destination, requirements, uploaded_files
         }
      */
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
