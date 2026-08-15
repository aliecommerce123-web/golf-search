/* Belek Golf Collection - flow control, no animations. */
(() => {
  "use strict";

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

  const HOTELS = window.__HOTELS__ || [];
  const COURSES = window.__COURSES__ || [];
  const BY_OID = {};
  HOTELS.forEach((h) => (BY_OID[h.oid] = h));

  const state = {
    step: 1,
    maxStep: 1,
    hotel: null,
    date: "",
    nights: "",
    rounds: "",
    golfers: 2,
    nonGolfers: 0,
    result: null,
    selected: null,
  };

  let fp = null;
  let extraBuilt = false;
  let dropSeq = 0;

  /* ---------------- Step-Navigation ---------------- */
  function goStep(n) {
    if (n > state.maxStep) return;
    state.step = n;
    $$(".view").forEach((v) => v.classList.remove("is-active"));
    const v = $("#view-" + n);
    if (v) v.classList.add("is-active");
    syncStepper();
    window.scrollTo(0, 0);
  }

  function reach(n) {
    if (n > state.maxStep) state.maxStep = n;
    syncStepper();
  }

  function syncStepper() {
    $$("#stepper .step").forEach((btn) => {
      const n = parseInt(btn.dataset.go, 10);
      btn.classList.toggle("is-current", n === state.step);
      btn.classList.toggle("is-done", n < state.step && n <= state.maxStep);
      btn.classList.toggle("is-locked", n > state.maxStep);
    });
  }

  /* ---------------- Galleries (Step 1) ---------------- */
  function initGalleries() {
    $$(".gal").forEach((gal) => {
      const track = $(".gal-track", gal);
      if (!track) return;
      const imgs = $$("img", track);
      const dotsWrap = $(".gal-dots", gal);
      if (dotsWrap && imgs.length > 1) {
        dotsWrap.innerHTML = "";
        imgs.forEach((_, i) => {
          const d = document.createElement("span");
          if (i === 0) d.classList.add("on");
          dotsWrap.appendChild(d);
        });
      }
      const setActive = () => {
        if (!dotsWrap) return;
        const i = Math.round(track.scrollLeft / Math.max(1, track.clientWidth));
        $$("span", dotsWrap).forEach((d, di) => d.classList.toggle("on", di === i));
      };
      let raf = null;
      track.addEventListener("scroll", () => {
        if (raf) return;
        raf = requestAnimationFrame(() => {
          raf = null;
          setActive();
        });
      }, { passive: true });

      const prev = $(".gal-nav.prev", gal);
      const next = $(".gal-nav.next", gal);
      if (prev) prev.addEventListener("click", (e) => {
        e.stopPropagation();
        track.scrollLeft = Math.max(0, track.scrollLeft - track.clientWidth);
      });
      if (next) next.addEventListener("click", (e) => {
        e.stopPropagation();
        track.scrollLeft = track.scrollLeft + track.clientWidth;
      });
    });
  }

  /* ---------------- Select hotel ---------------- */
  function pickHotel(oid) {
    const h = BY_OID[oid];
    if (!h) return;
    // Reset downstream state when a different hotel is chosen.
    if (!state.hotel || state.hotel.oid !== oid) {
      state.result = null;
      state.selected = null;
      state.date = "";
      state.nights = "";
      state.rounds = "";
      if (fp) fp.clear(false);
      state.maxStep = 2;
    }
    state.hotel = h;
    renderSelHotel();
    reach(2);
    goStep(2);
    ensureDatePicker();
    // Nights/rounds depend on the date -> only load after a date is picked.
    if (state.date) {
      loadDropdowns();
    } else {
      resetDropdowns();
    }
  }

  function resetDropdowns() {
    dropSeq++;
    const nightsSel = $("#fNights");
    const roundsSel = $("#fRounds");
    nightsSel.innerHTML = '<option value="">Pick a date first</option>';
    roundsSel.innerHTML = '<option value="">Pick a date first</option>';
    state.nights = "";
    state.rounds = "";
  }

  function stars(n) {
    return "★".repeat(n);
  }

  function renderSelHotel() {
    const h = state.hotel;
    const img = h.cover ? `<img src="${h.cover}" alt="${esc(h.name)}">` : "";
    $("#selHotel").innerHTML =
      img +
      `<div><div class="sh-stars">${stars(h.stars)}</div>` +
      `<div class="sh-name">${esc(h.name)}</div>` +
      `<div class="sh-meta">Belek, Antalya · Green fees and airport transfers in the package</div></div>`;
  }

  /* ---------------- Datepicker ---------------- */
  function ensureDatePicker() {
    if (fp || typeof flatpickr === "undefined") return;
    fp = flatpickr("#fDate", {
      dateFormat: "d-m-Y",
      altInput: true,
      altFormat: "D, j F Y",
      minDate: "today",
      disableMobile: true,
      onChange: (sel, str) => {
        state.date = str;
        // Reload nights + rounds for the exact chosen date (all sources).
        loadDropdowns();
      },
    });
  }

  /* ---------------- Load dropdowns ---------------- */
  async function loadDropdowns() {
    const oid = state.hotel.oid;
    const seq = ++dropSeq;
    const nightsSel = $("#fNights");
    const roundsSel = $("#fRounds");
    nightsSel.innerHTML = '<option value="">Loading…</option>';
    roundsSel.innerHTML = '<option value="">Loading…</option>';
    try {
      const td = state.date ? `&tdate=${encodeURIComponent(state.date)}` : "";
      const [nRes, rRes] = await Promise.all([
        fetch(`/api/nights?hotel_oid=${oid}${td}`).then((r) => r.json()),
        fetch(`/api/rounds?hotel_oid=${oid}${td}`).then((r) => r.json()),
      ]);
      if (seq !== dropSeq) return; // stale response, a newer date selection is running
      fillSelect(nightsSel, nRes.options || [], "7");
      fillSelect(roundsSel, rRes.options || [], "3");
      state.nights = nightsSel.value;
      state.rounds = roundsSel.value;
      const hint = $("#searchHint");
      if (hint) {
        const noOpts = !(nRes.options || []).length && !(rRes.options || []).length;
        if (state.date && noOpts) {
          hint.className = "hint";
          hint.textContent =
            "For this date there are currently no golf packages available (e.g. in high summer). Pick a different date, or send us a personal request right away.";
        } else {
          hint.className = "hint";
          hint.textContent = "";
        }
      }
    } catch (e) {
      if (seq !== dropSeq) return;
      nightsSel.innerHTML = '<option value="">Not available</option>';
      roundsSel.innerHTML = '<option value="">Not available</option>';
    }
  }

  function fillSelect(sel, options, preferValue) {
    if (!options.length) {
      sel.innerHTML = '<option value="">No options</option>';
      return;
    }
    sel.innerHTML = "";
    options.forEach((o) => {
      const op = document.createElement("option");
      op.value = o.value;
      op.textContent = o.label || o.value;
      sel.appendChild(op);
    });
    const pref = options.find((o) => String(o.value) === String(preferValue));
    sel.value = pref ? pref.value : options[0].value;
  }

  /* ---------------- Search ---------------- */
  async function runSearch() {
    const hint = $("#searchHint");
    state.nights = $("#fNights").value;
    state.rounds = $("#fRounds").value;
    if (!state.date) {
      hint.className = "hint err";
      hint.textContent = "Please pick a check-in date first.";
      return;
    }
    if (!state.nights || !state.rounds) {
      // No packages for this date -> go straight to the personal request.
      startInquiry();
      return;
    }
    hint.className = "hint";
    hint.textContent = "";

    const btn = $("#btnSearch");
    btn.disabled = true;
    btn.textContent = "Fetching live prices…";

    reach(3);
    goStep(3);
    $("#offersWrap").innerHTML = '<div class="loading">We are loading the prices. This takes a moment…</div>';
    $("#offersTitle").textContent = state.hotel.name;
    $("#offersSub").textContent = "";

    try {
      const url =
        `/api/search?hotel_oid=${state.hotel.oid}` +
        `&travel_date=${encodeURIComponent(state.date)}` +
        `&nights=${encodeURIComponent(state.nights)}` +
        `&rounds=${encodeURIComponent(state.rounds)}` +
        `&currency=EUR`;
      const res = await fetch(url).then((r) => r.json());
      state.result = res;
      renderOffers(res);
    } catch (e) {
      $("#offersWrap").innerHTML =
        '<div class="noprice"><h3>Price lookup not possible right now</h3>' +
        "<p>The live lookup didn't work. Just send us a request and we'll check the prices personally.</p>" +
        '<button class="btn btn-primary" id="noPriceInq">Request an offer</button></div>';
      const b = $("#noPriceInq");
      if (b) b.addEventListener("click", () => startInquiry());
    } finally {
      btn.disabled = false;
      btn.textContent = "Show current prices";
    }
  }

  /* ---------------- Normalize inclusions ---------------- */
  // Flights out. VIP & CIP fast track out. Raw transfer out, replaced by our own
  // airport-transfer line that is included in the package price.
  function normalizeInclusions(bullets) {
    const out = [];
    const seen = new Set();
    (bullets || []).forEach((raw) => {
      const b = String(raw).trim();
      if (!b) return;
      const low = b.toLowerCase();
      if (/flug|flight|airfare/.test(low)) return;
      if (/vip|cip|fast.?track/.test(low)) return;
      if (/transfer|abhol|shuttle/.test(low)) return; // we add our own
      if (/live-preis|live price/.test(low)) return;
      if (seen.has(low)) return;
      seen.add(low);
      out.push(b);
    });
    out.push("Airport transfers (round trip) included in the package");
    return out;
  }

  function priceDisplay(p) {
    if (!p) return "";
    if (typeof p === "string") return p;
    if (p.display) return p.display;
    if (typeof p.amount === "number") {
      const cur = p.currency === "EUR" ? "€" : p.currency === "GBP" ? "£" : p.currency === "USD" ? "$" : "";
      return cur + " " + Math.round(p.amount).toLocaleString("en-GB");
    }
    return "";
  }

  function renderOffers(res) {
    const wrap = $("#offersWrap");
    const h = state.hotel;
    const withPrice = (res.packages || []).filter((p) => p.price_options && p.price_options.length);

    $("#offersTitle").textContent = h.name;
    if (withPrice.length) {
      $("#offersSub").textContent =
        `${withPrice.length} live offer${withPrice.length === 1 ? "" : "s"} · ` +
        `${state.nights} Night${String(state.nights) === "1" ? "" : "s"} · ${roundsLabel(state.rounds)} · from ${fmtDate(state.date)}`;
    } else {
      $("#offersSub").textContent = "";
    }

    if (!withPrice.length) {
      wrap.innerHTML =
        '<div class="noprice"><h3>No live price available for this combination right now</h3>' +
        "<p>No problem. Send us a request with your preferred dates, we'll get you the best offer personally and reply within 10 to 30 minutes.</p>" +
        '<button class="btn btn-primary" id="noPriceInq">Request an offer</button></div>';
      const b = $("#noPriceInq");
      if (b) b.addEventListener("click", () => startInquiry());
      return;
    }

    wrap.innerHTML = "";
    withPrice.forEach((pkg) => {
      const primary = pkg.primary_price || pkg.price_options[0];
      const actual = priceDisplay(primary.price_actual);
      const original = primary.price_original ? priceDisplay(primary.price_original) : "";
      const total = primary.total ? priceDisplay(primary.total) : "";
      const incl = normalizeInclusions(pkg.bullets);
      const badges = (pkg.promo_badges || []).map((b) => `<span class="badge">${esc(b)}</span>`).join("");
      const inclLi = incl.map((i) => `<li>${esc(i)}</li>`).join("");
      const cover = h.cover ? `<img src="${h.cover}" alt="${esc(h.name)}">` : "";
      const srcLabel = "Live price";

      const card = document.createElement("article");
      card.className = "offer";
      card.innerHTML =
        `<div class="offer-img"><span class="offer-src">${srcLabel}</span>${cover}</div>` +
        `<div class="offer-body">` +
          `<div class="offer-top"><div>` +
            `<div class="offer-nr">${esc(pkg.nights_rounds || (state.nights + " Nights"))}</div>` +
            `<div class="offer-date">Check-in ${esc(fmtDate(state.date))}</div>` +
          `</div>${badges ? `<div class="offer-badges">${badges}</div>` : ""}</div>` +
          `<ul class="incl">${inclLi}</ul>` +
          `<div class="offer-foot">` +
            `<div class="price"><span class="price-from">${esc(primary.label || "from")}</span>` +
              `<span class="price-main">${esc(actual)}${original ? `<span class="price-old">${esc(original)}</span>` : ""}</span>` +
              (total ? `<span class="price-total">Total approx. ${esc(total)}</span>` : "") +
            `</div>` +
            `<button class="btn btn-primary offer-inq" type="button">Request this offer</button>` +
          `</div>` +
        `</div>`;
      card.querySelector(".offer-inq").addEventListener("click", () => startInquiry(pkg));
      wrap.appendChild(card);
    });
  }

  function roundsLabel(r) {
    if (!r) return "";
    if (String(r).toUpperCase() === "UNLIMITED") return "Unlimited Rounds";
    return r + " Round" + (String(r) === "1" ? "" : "s");
  }

  function fmtDate(d) {
    // d-m-Y -> j M Y (short)
    const m = /^(\d{2})-(\d{2})-(\d{4})$/.exec(d || "");
    if (!m) return d || "";
    const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    return `${parseInt(m[1], 10)} ${months[parseInt(m[2], 10) - 1]} ${m[3]}`;
  }

  /* ---------------- Inquiry ---------------- */
  function startInquiry(pkg) {
    const h = state.hotel;
    state.selected = pkg || null;
    $("#iqOid").value = h.oid;
    $("#iqDate").value = state.date;
    $("#iqNights").value = state.nights;
    $("#iqRounds").value = state.rounds;
    $("#iqGolfers").value = state.golfers;
    $("#iqNonGolfers").value = state.nonGolfers;

    // Carry the selected offer's price + source into the checkout.
    let priceStr = "";
    let srcStr = "";
    if (pkg) {
      const primary = pkg.primary_price || (pkg.price_options && pkg.price_options[0]);
      if (primary) {
        const actual = priceDisplay(primary.price_actual);
        priceStr = (primary.label ? primary.label + " " : "") + actual;
        if (primary.total) priceStr += " (total approx. " + priceDisplay(primary.total) + ")";
      }
      srcStr = pkg.source || ""; // interner Slug für DB-Audit, nicht kundenseitig sichtbar
    }
    $("#iqPrice").value = priceStr;
    $("#iqSource").value = srcStr;

    $("#recap").innerHTML = recapHtml();
    buildExtraGrid();
    reach(4);
    goStep(4);
  }

  function recapHtml() {
    const h = state.hotel;
    const sel = state.selected;
    let priceRow = "";
    if (sel) {
      const primary = sel.primary_price || (sel.price_options && sel.price_options[0]);
      if (primary) {
        const actual = priceDisplay(primary.price_actual);
        const original = primary.price_original ? priceDisplay(primary.price_original) : "";
        priceRow =
          `<span class="ri">${esc(primary.label || "Price")}: <b>${esc(actual)}</b>` +
          (original ? ` <small class="ri-old">${esc(original)}</small>` : "") +
          `</span>`;
      }
    }
    return (
      `<span class="ri"><b>${esc(h.name)}</b></span>` +
      (state.date ? `<span class="ri">Check-in: <b>${esc(fmtDate(state.date))}</b></span>` : "") +
      (state.nights ? `<span class="ri">Nights: <b>${esc(state.nights)}</b></span>` : "") +
      (state.rounds ? `<span class="ri">Rounds: <b>${esc(String(state.rounds).toUpperCase() === "UNLIMITED" ? "Unlimited" : state.rounds)}</b></span>` : "") +
      priceRow
    );
  }

  function buildExtraGrid() {
    if (extraBuilt) return;
    const grid = $("#extraGrid");
    grid.innerHTML = "";
    COURSES.forEach((c, i) => {
      const id = "xr" + i;
      const label = document.createElement("label");
      label.className = "check";
      label.innerHTML =
        `<input type="checkbox" name="extra_rounds" id="${id}" value="${esc(c)}">` +
        `<span class="check-box" aria-hidden="true"></span>` +
        `<span class="check-text">${esc(c)}</span>`;
      grid.appendChild(label);
    });
    extraBuilt = true;
  }

  function setCounter(which, delta) {
    if (which === "golfers") {
      state.golfers = Math.max(1, Math.min(50, state.golfers + delta));
      $("#cGolfers").textContent = state.golfers;
      $("#iqGolfers").value = state.golfers;
    } else {
      state.nonGolfers = Math.max(0, Math.min(50, state.nonGolfers + delta));
      $("#cNonGolfers").textContent = state.nonGolfers;
      $("#iqNonGolfers").value = state.nonGolfers;
    }
  }

  async function submitInquiry(e) {
    e.preventDefault();
    const form = $("#inqForm");
    const hint = $("#inqHint");
    const name = form.full_name.value.trim();
    const email = form.email.value.trim();
    const phone = form.phone.value.trim();
    if (!name || !email || !phone) {
      hint.className = "hint err";
      hint.textContent = "Please fill in name, email and phone.";
      return;
    }
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
      hint.className = "hint err";
      hint.textContent = "Please enter a valid email address.";
      return;
    }
    hint.className = "hint";
    hint.textContent = "";
    const btn = $("#btnSend");
    btn.disabled = true;
    btn.textContent = "Sending…";
    try {
      const fd = new FormData(form);
      const res = await fetch("/api/inquiry", { method: "POST", body: fd }).then((r) => r.json());
      if (res && res.ok) {
        $("#confirmRecap").innerHTML =
          recapHtml() +
          `<span class="ri">Golfers: <b>${state.golfers}</b></span>` +
          (state.nonGolfers ? `<span class="ri">Additional guests: <b>${state.nonGolfers}</b></span>` : "");
        reach(5);
        goStep(5);
      } else {
        throw new Error("fail");
      }
    } catch (err) {
      hint.className = "hint err";
      hint.textContent = "Sending failed. Please try again in a moment.";
    } finally {
      btn.disabled = false;
      btn.textContent = "Send my request";
    }
  }

  /* ---------------- Utils ---------------- */
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /* ---------------- Wire-up ---------------- */
  function init() {
    initGalleries();
    syncStepper();

    $$("[data-pick]").forEach((b) =>
      b.addEventListener("click", () => pickHotel(parseInt(b.dataset.pick, 10)))
    );
    $$("[data-go]").forEach((b) =>
      b.addEventListener("click", () => goStep(parseInt(b.dataset.go, 10)))
    );
    $("#btnSearch").addEventListener("click", runSearch);
    $$(".cstep").forEach((b) =>
      b.addEventListener("click", () => setCounter(b.dataset.c, parseInt(b.dataset.d, 10)))
    );
    $("#inqForm").addEventListener("submit", submitInquiry);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
