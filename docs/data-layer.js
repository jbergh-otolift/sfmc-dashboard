/* data-layer.js — vult het dashboard vanuit data.json.
 *
 * Geen enkele API-call vanuit de browser: dit bestand doet één fetch naar het
 * statische data.json dat GitHub Actions server-side heeft gegenereerd.
 *
 * Contract: data.json = { generated_at, period, prev_period, markets: { nl: <markt>, be: <markt>, ... } }
 * waarbij <markt> = { marketLabel, _sources, kernKpis, reactivation, emailHealth, acquisition }
 *
 * Bindings in de HTML:
 *   data-bind="pad.naar.waarde"  data-fmt="int|pct0|pct1|pct1nosign|eur0|mult1"
 *   data-bind-delta="pad"        data-delta-dir="up-good|down-good"  data-delta-mode="abs-pct|eur-abs"
 *   data-bind-width="pad"        -> zet style.width op de waarde als percentage
 *
 * Waarden die null zijn krijgen een streepje en de class .nodata. Dat is
 * bewust: "niet aangesloten" mag er niet uitzien als "gemeten nul".
 */

const NODATA = "–";

/* ---------- helpers ---------- */

function getPath(obj, path) {
  // Ondersteunt zowel a.b.c als a.b[0].c
  return path
    .replace(/\[(\d+)\]/g, ".$1")
    .split(".")
    .reduce((acc, key) => (acc === null || acc === undefined ? undefined : acc[key]), obj);
}

function nlNum(value, decimals) {
  return value.toLocaleString("nl-NL", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function format(value, fmt) {
  if (value === null || value === undefined || Number.isNaN(value)) return null;
  switch (fmt) {
    case "int":
      return nlNum(Math.round(value), 0);
    case "pct0":
      return nlNum(Math.round(value), 0) + "%";
    case "pct1":
      // De HTML zet de %-tekens zelf in een <small>, dus hier alleen het getal.
      return nlNum(value, 1);
    case "pct1nosign":
      return nlNum(value, 1) + "%";
    case "eur0":
      return "€" + nlNum(Math.round(value), 0);
    case "mult1":
      return nlNum(value, 1) + "×";
    default:
      return String(value);
  }
}

// Schrijft tekst in het <span class="bindval">-kind als dat bestaat, anders in
// het element zelf. Zo blijven omliggende <small>%</small>-tekens intact.
function writeValue(el, text, isNull) {
  const target = el.querySelector(".bindval") || el;
  target.textContent = text;
  el.classList.toggle("nodata", !!isNull);
}

/* ---------- bindings ---------- */

function applyBindings(root, data) {
  root.querySelectorAll("[data-bind]").forEach((el) => {
    const raw = getPath(data, el.dataset.bind);
    const text = format(raw, el.dataset.fmt);
    writeValue(el, text === null ? NODATA : text, text === null);
  });

  root.querySelectorAll("[data-bind-width]").forEach((el) => {
    const raw = getPath(data, el.dataset.bindWidth);
    el.style.width = typeof raw === "number" ? Math.max(0, Math.min(100, raw)) + "%" : "0%";
  });

  // Halve-cirkel gauge: de boog is één pad met stroke-dasharray = volle lengte,
  // dus een offset van len (100% weg) t/m 0 (helemaal vol) tekent het percentage.
  root.querySelectorAll("[data-bind-arc]").forEach((el) => {
    const raw = getPath(data, el.dataset.bindArc);
    const len = parseFloat(el.dataset.arcLen) || 0;
    const pct = typeof raw === "number" ? Math.max(0, Math.min(100, raw)) : 0;
    el.setAttribute("stroke-dashoffset", (len * (1 - pct / 100)).toFixed(1));
  });

  root.querySelectorAll("[data-bind-delta]").forEach((el) => {
    renderBoundDelta(el, data);
  });
}

function renderBoundDelta(el, data) {
  const path = el.dataset.bindDelta;
  const mode = el.dataset.deltaMode || "pct";
  const downIsGood = el.dataset.deltaDir === "down-good";

  let pct = null;
  let absText = null;

  if (mode === "abs-pct") {
    // Blok "groei actieve database": absolute toename + procentuele MoM.
    const node = getPath(data, path) || {};
    pct = typeof node.deltaPct === "number" ? node.deltaPct : null;
    if (typeof node.deltaAbs === "number") absText = nlNum(node.deltaAbs, 0);
  } else if (mode === "eur-abs") {
    const raw = getPath(data, path);
    if (typeof raw === "number") absText = "€" + nlNum(Math.abs(Math.round(raw)), 0);
    pct = typeof raw === "number" ? raw : null;
  } else {
    const raw = getPath(data, path);
    pct = typeof raw === "number" ? raw : null;
  }

  const arrow = el.querySelector(".d-arw") || el.querySelector(".a");
  const val = el.querySelector(".bindval");

  if (pct === null) {
    el.classList.remove("up", "down");
    el.classList.add("nodata");
    if (arrow) arrow.textContent = "";
    if (val) val.textContent = NODATA;
    else el.textContent = NODATA;
    return;
  }

  el.classList.remove("nodata");
  const rising = pct > 0;
  const positive = downIsGood ? !rising : rising;
  el.classList.toggle("up", positive);
  el.classList.toggle("down", !positive);
  if (arrow) arrow.textContent = pct === 0 ? "–" : rising ? "▲" : "▼";

  const sign = rising ? "+" : "-";
  const text =
    mode === "eur-abs" || (mode === "abs-pct" && absText)
      ? sign + (absText !== null ? absText.replace("-", "") : nlNum(Math.abs(pct), 1) + "%")
      : sign + nlNum(Math.abs(pct), 1) + "%";
  if (val) val.textContent = text;
  else el.textContent = text;
}

/* ---------- Email Health: dynamische flow-chips ---------- */

const benchTxt = { ok: "Gezond", warn: "Let op", bad: "Actie nodig" };
const openBench = { ok: "Sterk", warn: "Redelijk", bad: "Laag" };

// Drempels per metric: [grens_ok, grens_warn]. `higherIsBetter` bepaalt de richting.
const thresholds = {
  delivery: { bounds: [98, 95], higherIsBetter: true },
  ctor: { bounds: [12, 8], higherIsBetter: true },
  ctr: { bounds: [2.5, 1.5], higherIsBetter: true },
  open: { bounds: [35, 25], higherIsBetter: true },
  unsub: { bounds: [0.3, 0.5], higherIsBetter: false },
  bounce: { bounds: [1, 2], higherIsBetter: false },
  spam: { bounds: [0.05, 0.1], higherIsBetter: false },
};

function benchLevel(key, value) {
  const t = thresholds[key];
  if (!t || value === null || value === undefined) return null;
  const [ok, warn] = t.bounds;
  if (t.higherIsBetter) return value >= ok ? "ok" : value >= warn ? "warn" : "bad";
  return value <= ok ? "ok" : value <= warn ? "warn" : "bad";
}

const METRICS = ["delivery", "ctor", "ctr", "open", "unsub", "bounce", "spam", "sent"];
const UNITS = { delivery: "%", ctor: "%", ctr: "%", open: "%", unsub: "%", bounce: "%", spam: "%", sent: "" };
const badWhenUp = { unsub: true, bounce: true, spam: true };

function renderFlowDelta(el, key, pct) {
  if (!el) return;
  if (pct === null || pct === undefined) {
    el.className = "delta nodata";
    el.innerHTML = NODATA;
    return;
  }
  if (pct === 0) {
    el.className = "delta";
    el.innerHTML = '<span class="d-arw">–</span>0%';
    return;
  }
  const rising = pct > 0;
  const positive = badWhenUp[key] ? !rising : rising;
  el.className = "delta " + (positive ? "up" : "down");
  el.innerHTML =
    '<span class="d-arw">' + (rising ? "▲" : "▼") + "</span>" +
    (rising ? "+" : "-") + nlNum(Math.abs(pct), 1) + "%";
}

function applyFlow(emailHealth, flowKey) {
  const flow = emailHealth[flowKey];
  if (!flow) return;
  const deltas = flow.deltas || {};

  METRICS.forEach((key) => {
    const el = document.querySelector('[data-k="' + key + '"]');
    if (el) {
      const value = flow[key];
      if (value === null || value === undefined) {
        el.innerHTML = NODATA;
        el.classList.add("nodata");
      } else {
        el.classList.remove("nodata");
        const num = key === "sent" ? nlNum(Math.round(value), 0) : nlNum(value, key === "spam" || key === "unsub" ? 2 : 1);
        el.innerHTML = num + (UNITS[key] ? "<small>" + UNITS[key] + "</small>" : "");
      }
    }
    renderFlowDelta(document.querySelector('[data-d="' + key + '"]'), key, deltas[key]);
  });

  ["delivery", "ctor", "ctr", "unsub", "bounce", "spam"].forEach((key) => {
    const el = document.querySelector('[data-b="' + key + '"]');
    if (!el) return;
    const level = benchLevel(key, flow[key]);
    if (!level) {
      el.className = "bench nodata";
      el.textContent = "geen bron";
      return;
    }
    el.className = "bench " + level;
    el.textContent = key === "ctor" ? openBench[level] : benchTxt[level];
  });
}

function buildFlowChips(emailHealth) {
  const bar = document.querySelector("[data-flow-chips]");
  if (!bar) return;
  bar.innerHTML = "";

  // "all" altijd eerst, daarna de journeys op naam gesorteerd.
  const keys = Object.keys(emailHealth).sort((a, b) => {
    if (a === "all") return -1;
    if (b === "all") return 1;
    return (emailHealth[a].label || a).localeCompare(emailHealth[b].label || b, "nl");
  });

  keys.forEach((key, i) => {
    const btn = document.createElement("button");
    btn.className = "flowchip" + (i === 0 ? " active" : "");
    btn.dataset.flow = key;
    btn.textContent = emailHealth[key].label || key;
    const sent = emailHealth[key].sent;
    if (typeof sent === "number") btn.title = nlNum(sent, 0) + " verzonden in deze periode";
    btn.addEventListener("click", () => {
      bar.querySelectorAll(".flowchip").forEach((c) => c.classList.remove("active"));
      btn.classList.add("active");
      applyFlow(emailHealth, key);
    });
    bar.appendChild(btn);
  });

  if (keys.length) applyFlow(emailHealth, keys[0]);
}

/* ---------- coverage: meetbaar of niet ---------- */

// Coverage heeft twee bronnen nodig: de noemer (contacten met consent, uit het
// CRM) en de teller (unieke bereikte contacten, uit de SFMC-sends). Ontbreekt
// de teller omdat een markt nog geen Business Unit heeft, dan is coverage niet
// 0% maar niet meetbaar — dat onderscheid moet zichtbaar zijn.
function applyCoverageNote(coverage) {
  const el = document.querySelector("[data-coverage-note]");
  if (!el) return;

  if (!coverage || coverage.consentTotal === null || coverage.consentTotal === undefined) {
    el.innerHTML = "Geen consent-cijfer beschikbaar voor deze markt.";
    el.classList.add("nodata");
    return;
  }

  if (!coverage.measurable) {
    el.classList.add("nodata");
    const consent =
      nlNum(coverage.consentTotal || 0, 0) + " leads met consent";
    const why = {
      no_business_unit:
        ", maar deze markt heeft nog geen Business Unit in Marketing Cloud — " +
        "hoeveel daarvan bereikt zijn is er dus niet.",
      numerator_missing:
        ", maar het aantal bereikte contacten ontbreekt nog in de tracking-export.",
      no_consent_data: " — geen consent-cijfer beschikbaar.",
      nobody_to_mail:
        ", maar geen enkele lead staat op status Mailjourney mét consent, " +
        "dus er valt niets te bereiken.",
    }[coverage.reason] || " — teller nog niet beschikbaar.";
    el.innerHTML = "<b>Nog niet meetbaar.</b> " + consent + why;
    return;
  }

  el.classList.remove("nodata");
  el.innerHTML =
    "Nog <b>" + nlNum(coverage.toActivate, 0) + " leads</b> te bereiken" +
    '<span style="display:block;font-size:11.5px;margin-top:6px">Noemer: leads met ' +
    "status <b>Mailjourney</b> én consent — de mensen die we horen te mailen, " +
    "niet de hele database.</span>";
}

/* ---------- opbrengst per flow ---------- */

function eur(value) {
  if (value === null || value === undefined) return NODATA;
  return "€" + nlNum(Math.round(value), 0);
}

// Tabel met wat elke journey opleverde. Gesorteerd op omzet, want dat is waar
// de vraag "welke flow doet ertoe" mee beantwoord wordt. Een flow die veel
// mensen raakt maar niets oplevert valt zo meteen op.
function applyOutcomes(market) {
  const table = document.querySelector("[data-outcome-table] tbody");
  if (!table) return;
  table.innerHTML = "";

  const health = market.emailHealth || {};
  const all = Object.keys(health)
    .filter((key) => key !== "all" && health[key].outcome)
    .map((key) => ({ label: health[key].label || key, o: health[key].outcome, g: health[key].goal }));

  // Transactionele flows (bevestigingen, herinneringen) horen te werken, niet
  // te presteren. Ze staan onderaan, zonder doel, puur als volumecontext.
  const marketing = all
    .filter((r) => !r.g || r.g.type !== "transactional")
    .sort((a, b) => (a.g?.attainment ?? 1e9) - (b.g?.attainment ?? 1e9));
  const transactional = all
    .filter((r) => r.g && r.g.type === "transactional")
    .sort((a, b) => (b.o.touched || 0) - (a.o.touched || 0));

  const cell = (text, cls) =>
    "<td" + (cls ? ' class="' + cls + '"' : "") + ">" + text + "</td>";

  const addGroup = (title, sub) => {
    const tr = document.createElement("tr");
    tr.className = "group";
    tr.innerHTML =
      '<td class="l" colspan="6">' + title +
      (sub ? "<small>" + sub + "</small>" : "") + "</td>";
    table.appendChild(tr);
  };

  const addRow = (row, withGoal) => {
    const o = row.o;
    const g = row.g;
    const tr = document.createElement("tr");

    let goalCell = '<span class="nodata">geen doel</span>';
    let actual = NODATA;
    let target = '<span class="nodata">niet vastgesteld</span>';
    let attain = NODATA;

    if (withGoal && g && g.label) {
      goalCell = (g.configured ? "" : '<span class="goal-default">standaard · </span>') + g.label;
      actual =
        nlNum(g.actual || 0, 0) +
        (g.rate !== null ? "<small>" + nlNum(g.rate, 1) + "%</small>" : "");
      if (g.target !== null && g.target !== undefined) {
        target = nlNum(Math.round(g.target), 0);
      }
      if (g.attainment !== null && g.attainment !== undefined) {
        const level = g.attainment >= 90 ? "ok" : g.attainment >= 50 ? "warn" : "bad";
        attain = '<span class="attain ' + level + '">' + nlNum(g.attainment, 0) + "%</span>";
        if (level === "bad") tr.className = "weak";
      }
    } else if (!withGoal) {
      // Transactioneel: alleen deliverability is relevant, geen doelkolom.
      goalCell = '<span class="nodata">transactioneel</span>';
      actual = "";
      target = "";
      attain = "";
    }

    tr.innerHTML =
      cell(row.label + (g && g.note ? "<small>" + g.note + "</small>" : ""), "l") +
      cell(goalCell, "l") +
      cell(nlNum(o.touched, 0)) +
      cell(actual) +
      cell(target) +
      cell(attain);
    table.appendChild(tr);
  };

  if (marketing.length) {
    addGroup("Marketingflows", "sturen op resultaat");
    marketing.forEach((r) => addRow(r, true));
  }
  if (transactional.length) {
    addGroup("Transactioneel", "bevestigingen en herinneringen — horen te werken, niet te presteren");
    transactional.forEach((r) => addRow(r, false));
  }

  const total = market.outcomeTotal;
  if (total) {
    const tr = document.createElement("tr");
    tr.className = "total";
    tr.innerHTML =
      cell("Totaal · ontdubbeld", "l") + cell("", "l") +
      cell(nlNum(total.touched, 0)) +
      cell(nlNum(total.toAppointment, 0) + "<small>afspraken</small>") +
      cell(nlNum(total.toSql, 0) + "<small>SQL</small>") + cell("");
    table.appendChild(tr);
  }

  if (!all.length && !total) {
    table.innerHTML =
      '<tr><td class="l nodata" colspan="6">Geen resultaatcijfers voor deze markt.</td></tr>';
  }
}

/* ---------- bronnen-badges ---------- */

// Zet per sectie zichtbaar of de data live is of nog niet aangesloten, zodat
// een lezer nooit een placeholder voor een meting aanziet.
function applySourceBadges(sources) {
  document.querySelectorAll("[data-source-for]").forEach((el) => {
    const state = (sources || {})[el.dataset.sourceFor];
    if (state === "live") {
      el.className = "srcbadge live";
      el.textContent = "Live data";
    } else if (state === "partial") {
      el.className = "srcbadge partial";
      el.textContent = "Deels aangesloten";
    } else {
      el.className = "srcbadge off";
      el.textContent = "Niet aangesloten";
    }
  });
}

/* ---------- markt + periode ---------- */

let DATA = null;
let currentMarket = "nl";

function applyMarket(marketKey) {
  if (!DATA) return;
  const market = DATA.markets[marketKey];
  currentMarket = marketKey;

  const label = document.querySelector("[data-country-label]");
  if (label) label.textContent = (market && market.marketLabel) || marketKey.toUpperCase();

  const empty = document.querySelector("[data-market-empty]");
  if (!market || market.available === false) {
    if (empty) empty.hidden = false;
    applyBindings(document, {});
    // Chips van de vorige markt weghalen, anders blijven die van NL/BE staan
    // bij een markt die nog niet bestaat.
    const bar = document.querySelector("[data-flow-chips]");
    if (bar) bar.innerHTML = "";
    applyFlow({ all: {} }, "all");
    applyOutcomes({});
    applySourceBadges({});
    return;
  }
  if (empty) empty.hidden = true;

  applyBindings(document, market);
  applyCoverageNote(market.kernKpis && market.kernKpis.coverage);
  applyOutcomes(market);
  applySourceBadges(market._sources);
  buildFlowChips(market.emailHealth || { all: { label: "Alle flows" } });
}

function applyMeta(data) {
  const fmtDate = (iso) =>
    new Date(iso).toLocaleDateString("nl-NL", { day: "numeric", month: "short", year: "numeric" });

  const stamp = document.querySelector("[data-stamp]");
  if (stamp && data.generated_at) stamp.textContent = "Bijgewerkt: " + fmtDate(data.generated_at);

  const periodEl = document.querySelector("[data-period-label]");
  if (periodEl && data.period) {
    // period.end is exclusief; toon de laatste dag die er wél in zit.
    const end = new Date(data.period.end);
    end.setDate(end.getDate() - 1);
    periodEl.textContent = fmtDate(data.period.start) + " t/m " + fmtDate(end.toISOString());
  }

  const daysEl = document.querySelector("[data-outcome-days]");
  if (daysEl && data.outcome_lookback_days) daysEl.textContent = data.outcome_lookback_days;

  const prevEl = document.querySelector("[data-prev-period-label]");
  if (prevEl && data.prev_period) {
    const end = new Date(data.prev_period.end);
    end.setDate(end.getDate() - 1);
    prevEl.textContent = fmtDate(data.prev_period.start) + " t/m " + fmtDate(end.toISOString());
  }
}

function wireCountryToggle() {
  document.querySelectorAll(".flag-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".flag-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      applyMarket(btn.dataset.country);
    });
  });
}

async function init() {
  wireCountryToggle();
  try {
    const resp = await fetch("data.json", { cache: "no-store" });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    DATA = await resp.json();
  } catch (err) {
    const banner = document.querySelector("[data-load-error]");
    if (banner) {
      banner.hidden = false;
      banner.textContent = "Kon data.json niet laden (" + err.message + ").";
    }
    return;
  }
  applyMeta(DATA);
  applyMarket(currentMarket);
}

document.addEventListener("DOMContentLoaded", init);
