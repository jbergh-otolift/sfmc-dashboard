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

// Streefwaarde voor automation coverage, in procenten. Coverage meet of de
// instroom van een periode ook daadwerkelijk gemaild is; 93-97% is normaal
// functioneren, dus een doel van 95% slaat alleen aan als er iets wegzakt.
// Eén plek wijzigen volstaat: de meter tekent zijn markering hieruit.
const COVERAGE_TARGET = 95;

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
    case "dec1":
      return nlNum(value, 1);
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

  drawGoalMark();
}

// Zet het doelstreepje op de meter. De positie wordt uit het booglengte-pad
// zelf afgeleid in plaats van met de hand uitgerekend, zodat het streepje
// meteen klopt als de streefwaarde verandert.
function drawGoalMark() {
  const arc = document.getElementById("covArc");
  const mark = document.querySelector("[data-goal-mark]");
  const label = document.querySelector("[data-goal-label]");
  const text = document.querySelector("[data-goal-text]");
  if (text) text.textContent = COVERAGE_TARGET + "%+";
  if (!arc || !mark || !label || !arc.getPointAtLength) return;

  const length = arc.getTotalLength();
  const at = arc.getPointAtLength((COVERAGE_TARGET / 100) * length);
  // Richting van de boog op dat punt, om loodrecht naar buiten te wijzen.
  const before = arc.getPointAtLength(Math.max((COVERAGE_TARGET / 100) * length - 1, 0));
  const dx = at.x - before.x;
  const dy = at.y - before.y;
  const len = Math.hypot(dx, dy) || 1;
  const nx = dy / len;
  const ny = -dx / len;

  mark.setAttribute("x1", (at.x + nx * 14).toFixed(1));
  mark.setAttribute("y1", (at.y + ny * 14).toFixed(1));
  mark.setAttribute("x2", (at.x + nx * 30).toFixed(1));
  mark.setAttribute("y2", (at.y + ny * 30).toFixed(1));

  label.setAttribute("x", (at.x + nx * 38).toFixed(1));
  label.setAttribute("y", (at.y + ny * 38 + 4).toFixed(1));
  label.setAttribute("text-anchor", at.x > 150 ? "start" : "end");
  label.textContent = COVERAGE_TARGET + "%";
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
    if (key !== "all" && (emailHealth[key].emailBreakdown || []).length) {
      btn.classList.add("has-detail");
      btn.title = (btn.title ? btn.title + " · " : "") + "klik voor de mails in deze flow";
    }
    btn.addEventListener("click", () => {
      bar.querySelectorAll(".flowchip").forEach((c) => c.classList.remove("active"));
      btn.classList.add("active");
      applyFlow(emailHealth, key);
      // "Alle flows" heeft geen mailopbouw; de losse journeys wel.
      if (key === "all") closeFlowDetail();
      else renderFlowDetail(emailHealth[key]);
    });
    bar.appendChild(btn);
  });

  if (keys.length) applyFlow(emailHealth, keys[0]);
  closeFlowDetail();
}

/* ---------- coverage: meetbaar of niet ---------- */

// Coverage heeft twee bronnen nodig: de noemer (contacten met consent, uit het
// CRM) en de teller (unieke bereikte contacten, uit de SFMC-sends). Ontbreekt
// de teller omdat een markt nog geen Business Unit heeft, dan is coverage niet
// 0% maar niet meetbaar — dat onderscheid moet zichtbaar zijn.
// Laat zien hoe de noemer is opgebouwd: van iedereen in de mailflow naar de
// groep die je werkelijk kunt mailen. Zonder die opbouw is een coverage van
// 32% niet te beoordelen — je weet niet of het gat aan bereik ligt of aan
// ontbrekende adressen en consent.
function renderCoverageBuild(coverage, cohort) {
  const box = document.querySelector("[data-coverage-build]");
  if (!box) return;
  const wrap = document.querySelector("[data-coverage-details]");
  if (!cohort || !cohort.mailable) {
    if (wrap) wrap.hidden = true;
    return;
  }
  if (wrap) wrap.hidden = false;
  box.innerHTML = "";

  const row = (label, value, cls) => {
    const el = document.createElement("div");
    el.className = "cb-row" + (cls ? " " + cls : "");
    const l = document.createElement("div");
    l.className = "cb-lbl";
    l.textContent = label;
    const n = document.createElement("div");
    n.className = "cb-num";
    n.textContent = value;
    el.append(l, n);
    box.appendChild(el);
  };

  const notMailable = (cohort.entered || 0) - (cohort.mailable || 0);
  row("Kwamen in de mailflow", nlNum(cohort.entered, 0));
  row("Geen adres, geen consent of afgemeld", "− " + nlNum(notMailable, 0), "minus");
  row("Mochten we mailen", nlNum(cohort.mailable, 0), "result");
  row("Hebben we gemaild", nlNum(cohort.reached, 0), "result");
}

// Bouwt de coverage-waarden voor de gekozen periode. Het getoonde percentage
// is dat van het cohort: van de instroom van deze periode, wie is er gemaild.
function coverageForPeriod(base, cohort) {
  // Zonder cohort is er niets te tonen voor dit bereik. Terugvallen op de
  // staande voorraad zou een getal opleveren dat over iets anders gaat.
  if (!cohort || !cohort.mailable) {
    return {
      value: null,
      shouldMail: null,
      reached: null,
      toActivate: null,
      measurable: false,
      reason: "no_data_in_range",
    };
  }
  return {
    ...(base || {}),
    value: cohort.coverage,
    shouldMail: cohort.mailable,
    reached: cohort.reached,
    toActivate: Math.max((cohort.mailable || 0) - (cohort.reached || 0), 0),
    measurable: cohort.coverage !== null && cohort.coverage !== undefined,
    reason: null,
  };
}

// Toont wat her-activatie heeft opgeleverd in het gekozen bereik, per route.
// Alleen orders waarvan de conversie ná het heractivatiesignaal ligt.
// Waar komen de leads in de mailflow vandaan? De funnel leest als een keten,
// maar maar een deel komt via Not reached — de rest via Follow-up of
// rechtstreeks vanuit New. Dat hoort erbij te staan.
function renderMailjourneyOrigin(stage) {
  const el = document.querySelector("[data-mj-herkomst]");
  if (!el) return;
  // De bronnen staan nu als zijtakken bóven het blok; hier alleen nog wat
  // het blok zelf zegt, anders staat hetzelfde rijtje er twee keer.
  // Het percentage staat al op de verbinding ernaast; hier alleen waar het
  // getal over gaat.
  el.textContent = "in een actieve flow";
}

// De instroom heeft twee bronnen: nieuwe leads, en aanvragen van mensen die
// al in het systeem staan. Die laatste krijgen geen New maar Re-entered, en
// zouden zonder deze regel onzichtbaar blijven.
function renderIntakeSplit(stage) {
  const el = document.querySelector("[data-intake-split]");
  if (!el) return;
  if (!stage || !stage.opnieuw) {
    el.textContent = "startpunt";
    return;
  }
  el.textContent =
    nlNum(stage.nieuw, 0) + " nieuw · " + nlNum(stage.opnieuw, 0) + " opnieuw";
}

// Niet elke Re-entered komt uit de mailflow: sales zet die status ook met de
// hand vanuit Lost of Not Qualified. Alleen de mailflow-route telt hier.
function renderSqlOrigin(stage) {
  const el = document.querySelector("[data-sql-herkomst]");
  if (!el) return;
  const buiten = stage && stage.herkomst && stage.herkomst.buitenMailflow;
  el.textContent = buiten
    ? nlNum(buiten, 0) + " buiten de mailflow niet geteld"
    : "uit de mailflow";
}

// De afspraken uitgelegd. Gegroepeerd op instroomreden, want dat is waar een
// flow aan hangt: elke reden is een eigen journey. De statuspaden staan
// uitklapbaar in de kaart van hun eigen reden, zonder die reden te herhalen.
function renderApptBlock(split, reasons, reasonPaths, routes) {
  const block = document.querySelector("[data-appt-block]");
  if (!block) return;

  const bak = split || {};
  const total = ["notReached", "mailjourney", "direct", "other"].reduce(
    (sum, key) => sum + (bak[key] || 0),
    0
  );
  if (!total) {
    block.hidden = true;
    return;
  }
  block.hidden = false;

  const val = block.querySelector("[data-appt-total]");
  const sub = block.querySelector("[data-appt-sub]");
  if (val) val.textContent = nlNum(total, 0);
  if (sub) {
    const viaFlow =
      ((routes || {})["Re-entered"] || 0) + ((routes || {})["Nummer gewijzigd"] || 0);
    sub.textContent =
      "afspraken · waarvan " + nlNum(viaFlow, 0) +
      " via een heractivatiestatus";
  }

  const box = block.querySelector("[data-appt-routes]");
  box.innerHTML = "";
  Object.entries(reasons || {})
    .sort((a, b) => b[1] - a[1])
    .forEach(([reason, n]) => {
      const paden = Object.entries((reasonPaths || {})[reason] || {}).sort(
        (a, b) => b[1] - a[1]
      );

      const card = document.createElement(paden.length ? "details" : "div");
      card.className = "vb-route";

      const head = document.createElement(paden.length ? "summary" : "div");
      head.className = "vb-r-head";
      const name = document.createElement("div");
      name.className = "vb-r-name";
      // De reden is de kop; zonder reden is het een lead die alleen via de
      // nummerwijziging in beeld kwam.
      // Alleen de reden. Dat het om een mailjourney gaat staat al in de kop
      // van het blok; het er bij elke kaart voor zetten is ruis. Leads zonder
      // reden krijgen de status waarmee ze instroomden als naam.
      name.textContent = reason;
      const amount = document.createElement("div");
      amount.className = "vb-r-val";
      amount.textContent = nlNum(n, 0);
      const note = document.createElement("div");
      note.className = "vb-r-sub";
      note.textContent = n === 1 ? "afspraak" : "afspraken";
      head.append(name, amount, note);
      card.appendChild(head);

      if (paden.length) {
        const list = document.createElement("div");
        list.className = "vb-reasons";
        paden.forEach(([pad, count]) => {
          const line = document.createElement("div");
          line.className = "vb-reason";
          const label = document.createElement("span");
          label.textContent = pad;
          const amt = document.createElement("span");
          amt.textContent = nlNum(count, 0);
          line.append(label, amt);
          list.appendChild(line);
        });
        card.appendChild(list);
      }
      box.appendChild(card);
    });
}

function renderValueBlock(value) {
  const block = document.querySelector("[data-value-block]");
  if (!block) return;
  if (!value || !value.mailedOrders) {
    block.hidden = true;
    return;
  }
  block.hidden = false;

  const total = block.querySelector("[data-value-total]");
  const sub = block.querySelector("[data-value-sub]");
  // Orders in plaats van euro's: de gemiddelde orderwaarde varieert sterk,
  // waardoor een bedrag meer over de mix zegt dan over de prestatie.
  if (total) total.textContent = nlNum(value.mailedOrders, 0);
  if (sub) {
    sub.textContent =
      "orders · waarvan " + nlNum(value.viaOrders, 0) +
      " met een aantoonbaar heractivatiepad";
  }

  const box = block.querySelector("[data-value-routes]");
  box.innerHTML = "";
  const labels = (DATA && DATA.routeLabels) || {};
  const routes = Object.entries(value.byRoute || {}).sort(
    (a, b) => b[1].orders - a[1].orders
  );

  // Elke route uitklapbaar: welke instroomredenen zitten erachter.
  routes.forEach(([route, stats]) => {
    const reasons = Object.entries((value.byRouteReason || {})[route] || {}).sort(
      (a, b) => b[1].orders - a[1].orders
    );

    const card = document.createElement(reasons.length ? "details" : "div");
    card.className = "vb-route";

    const head = document.createElement(reasons.length ? "summary" : "div");
    head.className = "vb-r-head";
    const name = document.createElement("div");
    name.className = "vb-r-name";
    name.textContent = labels[route] || route;
    const val = document.createElement("div");
    val.className = "vb-r-val";
    val.textContent = nlNum(stats.orders, 0);
    const note = document.createElement("div");
    note.className = "vb-r-sub";
    note.textContent = stats.orders === 1 ? "order" : "orders";
    head.append(name, val, note);
    card.appendChild(head);

    if (reasons.length) {
      const list = document.createElement("div");
      list.className = "vb-reasons";
      reasons.forEach(([reason, v]) => {
        const row = document.createElement("div");
        row.className = "vb-reason";
        const label = document.createElement("span");
        label.textContent = reason;
        const amount = document.createElement("span");
        amount.textContent =
          nlNum(v.orders, 0) + (v.orders === 1 ? " order" : " orders");
        row.append(label, amount);
        list.appendChild(row);
      });
      card.appendChild(list);
    }
    box.appendChild(card);
  });

  renderDetailList(
    "[data-value-paths]",
    value.byPath,
    "Geen paden in dit bereik."
  );
  renderDetailList(
    "[data-value-flows]",
    value.byFlow,
    "Geen orders toe te rekenen aan een losse flow in dit bereik."
  );
}

// Lijstje van pad of flow met omzet, gesorteerd op opbrengst.
function renderDetailList(selector, data, emptyText) {
  const host = document.querySelector(selector);
  if (!host) return;
  host.innerHTML = "";
  const rows = Object.entries(data || {}).sort((a, b) => b[1].orders - a[1].orders);

  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "vb-empty";
    empty.textContent = emptyText;
    host.appendChild(empty);
    return;
  }

  rows.forEach(([label, v]) => {
    const row = document.createElement("div");
    row.className = "vb-path";
    const name = document.createElement("span");
    name.className = "vb-p-name";
    // Padnamen komen uit de data; via textContent de DOM in.
    name.textContent = label;
    const amount = document.createElement("span");
    amount.className = "vb-p-val";
    amount.textContent =
      nlNum(v.orders, 0) + (v.orders === 1 ? " order" : " orders");
    row.append(name, amount);
    host.appendChild(row);
  });
}

function eur(value) {
  if (value === null || value === undefined) return NODATA;
  return "€" + nlNum(Math.round(value), 0);
}

function applyCoverageNote(coverage, cohort) {
  const el = document.querySelector("[data-coverage-note]");
  if (!el) return;

  if (coverage && coverage.reason === "no_data_in_range") {
    el.classList.add("nodata");
    el.textContent = "Geen instroom in de mailflow binnen dit bereik.";
    const wrap = document.querySelector("[data-coverage-details]");
    if (wrap) wrap.hidden = true;
    return;
  }

  if (!coverage || coverage.shouldMail === null || coverage.shouldMail === undefined) {
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
      no_data_in_range: "",
      nobody_to_mail:
        ", maar geen enkele lead staat op status Mailjourney mét consent, " +
        "dus er valt niets te bereiken.",
    }[coverage.reason] || " — teller nog niet beschikbaar.";
    el.innerHTML = "<b>Nog niet meetbaar.</b> " + consent + why;
    return;
  }

  el.classList.remove("nodata");
  renderCoverageBuild(coverage, cohort);
  el.innerHTML =
    "Nog <b>" + nlNum(coverage.toActivate, 0) + " leads</b> te bereiken" +
    '<span style="display:block;font-size:11.5px;margin-top:6px">Van de leads die ' +
    "<b>in deze periode</b> in de mailflow kwamen en die we mochten mailen.</span>";
}

/* ---------- detail per flow: welke mails zitten erin ---------- */

// Eén tooltip-element voor alle mailregels. Labels komen uit de API en zijn
// dus onvertrouwde tekst: altijd via textContent, nooit via innerHTML.
let fdTip = null;

function showTip(anchor, email) {
  if (!fdTip) {
    fdTip = document.createElement("div");
    fdTip.className = "fd-tip";
    document.body.appendChild(fdTip);
  }
  fdTip.innerHTML = "";

  const title = document.createElement("b");
  title.textContent = email.name || "(naamloos)";
  fdTip.appendChild(title);

  const rows = [
    ["Verstuurd", nlNum(email.sent || 0, 0)],
    ["Aangekomen", nlNum(email.delivered || 0, 0)],
    ["Geopend", nlNum(email.opens || 0, 0)],
    ["Geklikt", nlNum(email.clicks || 0, 0)],
    ["Afgemeld", nlNum(email.unsubs || 0, 0)],
    ["Bounces", nlNum(email.bounces || 0, 0)],
  ];
  rows.forEach(([label, value]) => {
    const row = document.createElement("div");
    row.className = "t-row";
    const left = document.createElement("span");
    left.textContent = label;
    const right = document.createElement("span");
    right.textContent = value;
    row.append(left, right);
    fdTip.appendChild(row);
  });

  const box = anchor.getBoundingClientRect();
  fdTip.style.left = Math.min(box.left + 40, window.innerWidth - 300) + "px";
  fdTip.style.top = Math.max(box.top - 8, 8) + "px";
  fdTip.hidden = false;
}

function hideTip() {
  if (fdTip) fdTip.hidden = true;
}

// Tekent de journey als verticale tijdlijn: instroom bovenaan, dan elke mail
// als kaart met zijn cijfers, en de wachttijden als label op de verbinding.
// Wachtstappen krijgen geen eigen kaart — dat zijn geen touchpoints, en een
// lezer wil de mails zien, niet de techniek eromheen.
function renderFlowMap(flow, structure) {
  const map = document.querySelector("[data-fd-map]");
  if (!map) return;
  map.innerHTML = "";

  if (!structure || !(structure.nodes || []).length) {
    map.hidden = true;
    return;
  }
  map.hidden = false;

  // Cijfers per mail opzoeken op TSD-id, met de naam als terugval.
  const byTsd = {};
  const byName = {};
  (flow.emailBreakdown || []).forEach((email) => {
    // Een samengevoegde mail draagt de id's van al zijn journeyversies.
    (email.tsdIds || []).forEach((id) => {
      byTsd[id] = email;
    });
    if (email.tsdId) byTsd[email.tsdId] = email;
    if (email.name) byName[email.name] = email;
  });

  const nodes = structure.nodes;
  const maxSent = Math.max(
    ...(flow.emailBreakdown || []).map((e) => e.sent || 0),
    1
  );
  // Beste en zwakste alleen bepalen over mails met genoeg volume. Anders
  // wordt een testmail naar 16 mensen als "zwakste" bestempeld, wat niets
  // over de flow zegt.
  const MIN_FOR_FLAG = 100;
  const ctors = (flow.emailBreakdown || [])
    .filter((e) => (e.sent || 0) >= MIN_FOR_FLAG && typeof e.ctor === "number")
    .map((e) => e.ctor);
  const best = ctors.length > 2 ? Math.max(...ctors) : null;
  const worst = ctors.length > 2 ? Math.min(...ctors) : null;

  // Op diepte groeperen: alles op dezelfde diepte staat naast elkaar.
  const levels = new Map();
  nodes.forEach((node) => {
    const depth = typeof node.depth === "number" ? node.depth : 0;
    if (!levels.has(depth)) levels.set(depth, []);
    levels.get(depth).push(node);
  });
  const depths = [...levels.keys()].sort((a, b) => a - b);

  // Instroomkaart
  const entry = document.createElement("div");
  entry.className = "fd-lvl";
  const entryCard = document.createElement("div");
  entryCard.className = "fd-node fd-entry";
  const eLbl = document.createElement("div");
  eLbl.className = "n-name";
  eLbl.textContent = "Instroom";
  const eBig = document.createElement("div");
  eBig.className = "n-big";
  eBig.textContent =
    typeof flow.firstTouch === "number" ? nlNum(flow.firstTouch, 0) : NODATA;
  const eSub = document.createElement("div");
  eSub.className = "n-recip";
  eSub.textContent = "mensen nieuw in deze flow";
  entryCard.append(eLbl, eBig, eSub);
  entry.appendChild(entryCard);
  map.appendChild(entry);

  let pendingWaitDays = 0;
  let pendingSplit = null;

  depths.forEach((depth) => {
    const group = levels.get(depth);
    const emails = group.filter((n) => n.type === "EMAILV2");
    const waits = group.filter((n) => n.type === "WAIT");
    const splits = group.filter((n) => n.type === "DECISION");

    // Wachttijden en splitsingen dragen we mee naar de volgende mailrij.
    waits.forEach((w) => {
      pendingWaitDays += w.waitDays || 0;
    });
    if (splits.length) pendingSplit = splits.length;

    if (!emails.length) return;

    const link = document.createElement("div");
    link.className = "fd-link";
    const line1 = document.createElement("div");
    line1.className = "l-line";
    link.appendChild(line1);
    if (pendingWaitDays > 0) {
      const lbl = document.createElement("div");
      lbl.className = "l-lbl";
      lbl.textContent =
        pendingWaitDays === 1 ? "1 dag later" : nlNum(pendingWaitDays, 0) + " dagen later";
      link.appendChild(lbl);
      const line2 = document.createElement("div");
      line2.className = "l-line";
      link.appendChild(line2);
    }
    map.appendChild(link);

    if (pendingSplit || emails.length > 1) {
      const split = document.createElement("div");
      split.className = "fd-split";
      split.textContent =
        emails.length > 1
          ? "splitsing · " + emails.length + " varianten"
          : "keuzepunt";
      map.appendChild(split);
    }
    pendingWaitDays = 0;
    pendingSplit = null;

    const row = document.createElement("div");
    row.className = "fd-lvl";
    emails.forEach((node) => {
      const stats = byTsd[node.tsdId] || byName[node.name] || null;
      const card = document.createElement("div");
      card.className = "fd-node";

      const name = document.createElement("div");
      name.className = "n-name";
      name.textContent = node.name || "(naamloze mail)";
      card.appendChild(name);

      if (!stats || !stats.sent) {
        card.classList.add("nosend");
        const none = document.createElement("div");
        none.className = "n-recip";
        none.textContent = "niet verstuurd in deze periode";
        card.appendChild(none);
        row.appendChild(card);
        return;
      }

      const recip = document.createElement("div");
      recip.className = "n-recip";
      recip.textContent = nlNum(stats.sent, 0) + " ontvangers";
      card.appendChild(recip);

      const track = document.createElement("div");
      track.className = "n-bar";
      const fill = document.createElement("i");
      fill.style.width = Math.max((stats.sent / maxSent) * 100, 2) + "%";
      track.appendChild(fill);
      card.appendChild(track);

      const metrics = document.createElement("div");
      metrics.className = "n-metrics";
      const metric = (value, label, cls) => {
        const box = document.createElement("div");
        box.className = "n-m";
        const val = document.createElement("div");
        val.className = "n-m-val" + (cls ? " " + cls : "");
        val.textContent = typeof value === "number" ? nlNum(value, 1) + "%" : NODATA;
        const lbl = document.createElement("div");
        lbl.className = "n-m-lbl";
        lbl.textContent = label;
        box.append(val, lbl);
        return box;
      };
      metrics.append(
        metric(stats.open, "geopend", ""),
        metric(stats.ctor, "doorgeklikt", "ctor")
      );
      card.appendChild(metrics);

      if (best !== null && best !== worst && (stats.sent || 0) >= MIN_FOR_FLAG &&
          typeof stats.ctor === "number") {
        if (stats.ctor === best) {
          card.classList.add("best");
          const flag = document.createElement("div");
          flag.className = "n-flag";
          flag.textContent = "beste";
          card.appendChild(flag);
        } else if (stats.ctor === worst) {
          card.classList.add("worst");
          const flag = document.createElement("div");
          flag.className = "n-flag";
          flag.textContent = "zwakste";
          card.appendChild(flag);
        }
      }

      card.tabIndex = 0;
      card.addEventListener("pointerenter", () => showTip(card, stats));
      card.addEventListener("pointerleave", hideTip);
      card.addEventListener("focus", () => showTip(card, stats));
      card.addEventListener("blur", hideTip);

      row.appendChild(card);
    });
    map.appendChild(row);
  });
}

function renderFlowDetail(flow) {
  const panel = document.querySelector("[data-flow-detail]");
  if (!panel) return;

  const emails = (flow && flow.emailBreakdown) || [];
  const reach = flow && flow.firstTouch;
  const sent = flow && flow.sent;

  panel.hidden = false;
  const set = (sel, text) => {
    const el = panel.querySelector(sel);
    if (el) el.textContent = text;
  };
  set("[data-fd-name]", (flow && flow.label) || "–");
  set(
    "[data-fd-sub]",
    emails.length
      ? emails.length + (emails.length === 1 ? " mail in deze flow" : " mails in deze flow")
      : "geen losse mails gevonden"
  );
  set("[data-fd-reach]", typeof reach === "number" ? nlNum(reach, 0) : NODATA);
  set("[data-fd-sent]", typeof sent === "number" ? nlNum(sent, 0) : NODATA);
  const profile = ((DATA.markets[currentMarket] || {}).flowProfile || {})[flow && flow.label];
  const perPerson = profile && profile.mailsPerPerson;
  set("[data-fd-per]", typeof perPerson === "number" ? nlNum(perPerson, 1) : NODATA);

  renderFlowMap(flow, (DATA && DATA.journeyStructures &&
    DATA.journeyStructures[currentMarket] || {})[flow && flow.label]);

  const list = panel.querySelector("[data-fd-steps]");
  const empty = panel.querySelector("[data-fd-empty]");
  list.innerHTML = "";
  empty.hidden = emails.length > 0;
  if (!emails.length) return;

  // Eén tint voor alle balken: dit is één reeks, geen losse categorieën. De
  // balklengte is relatief aan de grootste mail in deze flow.
  const maxSent = Math.max(...emails.map((e) => e.sent || 0), 1);
  const MIN_FOR_FLAG = 100;
  const ctors = emails
    .filter((e) => (e.sent || 0) >= MIN_FOR_FLAG && typeof e.ctor === "number")
    .map((e) => e.ctor);
  const best = ctors.length > 2 ? Math.max(...ctors) : null;
  const worst = ctors.length > 2 ? Math.min(...ctors) : null;

  const headerRow = document.createElement("div");
  headerRow.className = "fd-step head";
  ["", "Mail", "Ontvangers", "Geopend", "Doorgeklikt"].forEach((label, i) => {
    const span = document.createElement("span");
    if (i >= 3) span.className = "r";
    span.textContent = label;
    headerRow.appendChild(span);
  });
  list.appendChild(headerRow);

  emails.forEach((email, index) => {
    const row = document.createElement("div");
    row.className = "fd-step";
    // Nadruk op de uitschieters in plaats van kleur per mail: bij 21 mails
    // zou een eigen tint per stap onleesbaar worden.
    if (best !== null && best !== worst && (email.sent || 0) >= MIN_FOR_FLAG &&
        typeof email.ctor === "number") {
      if (email.ctor === worst) row.classList.add("weak");
      if (email.ctor === best) row.classList.add("strong");
    }

    const num = document.createElement("div");
    num.className = "fd-num";
    num.textContent = String(index + 1);

    const nameBox = document.createElement("div");
    const name = document.createElement("div");
    name.className = "fd-mail-name";
    name.textContent = email.name || "(naamloos)";
    nameBox.appendChild(name);
    if (email.versions > 1) {
      const note = document.createElement("div");
      note.className = "fd-mail-date";
      note.textContent =
        email.versions + " versies van deze mail samengevoegd";
      nameBox.appendChild(note);
    }

    const barWrap = document.createElement("div");
    barWrap.className = "fd-bar-wrap";
    const track = document.createElement("div");
    track.className = "fd-bar-track";
    const bar = document.createElement("i");
    bar.className = "fd-bar";
    bar.style.width = Math.max((email.sent || 0) / maxSent * 100, 2) + "%";
    track.appendChild(bar);
    const barNum = document.createElement("div");
    barNum.className = "fd-bar-num";
    barNum.textContent = nlNum(email.sent || 0, 0);
    barWrap.append(track, barNum);

    const metric = (value, absolute, cls) => {
      const box = document.createElement("div");
      box.className = "fd-metric";
      const val = document.createElement("div");
      val.className = "fd-m-val" + (cls ? " " + cls : "");
      val.textContent = typeof value === "number" ? nlNum(value, 1) + "%" : NODATA;
      const sub = document.createElement("div");
      sub.className = "fd-m-sub";
      sub.textContent = absolute;
      box.append(val, sub);
      return box;
    };

    row.append(
      num,
      nameBox,
      barWrap,
      metric(email.open, nlNum(email.opens || 0, 0) + " mensen", ""),
      metric(email.ctor, nlNum(email.clicks || 0, 0) + " klikken", "ctor")
    );

    // De hele regel is het hover-doel, niet alleen de balk.
    row.addEventListener("pointerenter", () => showTip(row, email));
    row.addEventListener("pointerleave", hideTip);
    row.tabIndex = 0;
    row.addEventListener("focus", () => showTip(row, email));
    row.addEventListener("blur", hideTip);

    list.appendChild(row);
  });
}

function closeFlowDetail() {
  const panel = document.querySelector("[data-flow-detail]");
  if (panel) panel.hidden = true;
  hideTip();
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
// Gekozen periode in dagen. Stuurt Email Health, de funnel en het flow-detail
// aan. Coverage heeft bewust een eigen venster van 90 dagen, omdat de noemer
// daar een momentopname is die niet met de periode meebeweegt.
let currentPeriod = "30";
// Gekozen bereik als {start, end}; end is exclusief. Wordt uit het preset of
// uit de datumvelden afgeleid.
let currentRange = null;

/* ---------- dagbuckets optellen ---------- */

// firstTouch = mensen die die dag voor het eerst mail uit deze flow kregen.
// Optelbaar over elk bereik, in tegenstelling tot 'unieke ontvangers': wie op
// twee dagen mail kreeg zou daar dubbel tellen.
const COUNTERS = [
  "sent", "delivered", "opens", "clicks", "bounces", "soft_bounces", "unsubs", "firstTouch",
];

function addDays(iso, delta) {
  const d = new Date(iso + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + delta);
  return d.toISOString().slice(0, 10);
}

function daysBetween(start, end) {
  return Math.round(
    (new Date(end + "T00:00:00Z") - new Date(start + "T00:00:00Z")) / 86400000
  );
}

// Zet een preset om naar een bereik. `end` is exclusief, zoals overal.
//
// De kalenderpresets rekenen vanaf de laatste dag mét data, niet vanaf de
// kalenderdatum. Op 1 oktober is "deze maand" anders een leeg bereik, terwijl
// de lezer september bedoelt: de maand waar de cijfers over gaan.
// De vroegste dag waarvoor deze markt CRM-data heeft. De LeadHistory-export
// gaat minder ver terug dan de mailtracking, en de funnel hangt helemaal aan
// die eerste bron. Een bereik dat verder terugloopt doet alsof er maanden
// meetellen die niet bestaan, en telt stilzwijgend nul.
function firstDayWithData(market) {
  const days = Object.entries((market && market.days) || {})
    .filter(([, bucket]) => bucket && bucket.crm)
    .map(([day]) => day);
  return days.length ? days.sort()[0] : null;
}

function clampRange(range, firstDay) {
  if (!firstDay || !range || range.start >= firstDay) {
    return { start: range.start, end: range.end, clamped: false };
  }
  return { start: firstDay, end: range.end, clamped: true };
}

function rangeForPreset(preset, endDate) {
  const end = endDate;
  const lastDay = addDays(end, -1);

  if (preset === "month") {
    return { start: lastDay.slice(0, 8) + "01", end };
  }
  if (preset === "lastmonth") {
    const firstOfThis = lastDay.slice(0, 8) + "01";
    const d = new Date(firstOfThis + "T00:00:00Z");
    d.setUTCMonth(d.getUTCMonth() - 1);
    return { start: d.toISOString().slice(0, 10), end: firstOfThis };
  }
  if (preset === "quarter") {
    const month = parseInt(lastDay.slice(5, 7), 10) - 1;
    const qStart = Math.floor(month / 3) * 3;
    return {
      start: lastDay.slice(0, 4) + "-" + String(qStart + 1).padStart(2, "0") + "-01",
      end,
    };
  }
  if (preset === "ytd") {
    return { start: lastDay.slice(0, 4) + "-01-01", end };
  }
  return { start: addDays(end, -parseInt(preset, 10)), end };
}

// Telt alle dagbuckets binnen [start, end) op. Alles is toegerekend aan de
// verzenddatum, dus optellen levert exact hetzelfde als een aparte
// aggregatie over dat bereik — ook voor unieke opens en clicks.
function sumRange(days, start, end) {
  const flows = {};
  const crm = {
    toStatus: {}, transitions: {},
    // Alleen leads met een reden waar een actieve flow op zit.
    toStatusActive: {}, transitionsActive: {},
  };
  const cohort = { entered: 0, mailable: 0, reached: 0, hasData: false };
  // Omzet is toegerekend aan de conversiedatum en dus gewoon optelbaar.
  // Twee vaste routes door de funnel; per stap optelbaar over het bereik.
  const paths = {};
  const apptSplit = {};
  const enrich = {};
  const apptRoutes = {};
  const apptReasons = {};
  const apptReasonPaths = {};
  const apptRouteReason = {};
  const value = {
    orders: 0, revenue: 0,
    // Breed: gemaild door een flow uit de doelgroep en daarna geconverteerd.
    mailedOrders: 0, mailedRevenue: 0,
    // Streng: daarbovenop een aantoonbaar heractivatiepad.
    viaOrders: 0, viaRevenue: 0,
    byRoute: {}, byPath: {}, byRouteReason: {}, byFlow: {}, hasData: false,
  };

  Object.keys(days || {}).forEach((day) => {
    if (day < start || day >= end) return;
    const bucket = days[day];

    Object.entries(bucket.flows || {}).forEach(([name, flow]) => {
      const target = (flows[name] = flows[name] || {
        label: name === "all" ? "Alle flows" : name,
        emails: {},
        ...Object.fromEntries(COUNTERS.map((c) => [c, 0])),
      });
      COUNTERS.forEach((c) => {
        target[c] += flow[c] || 0;
      });
      Object.entries(flow.emails || {}).forEach(([mail, stats]) => {
        const box = (target.emails[mail] =
          target.emails[mail] || Object.fromEntries(COUNTERS.map((c) => [c, 0])));
        COUNTERS.forEach((c) => {
          box[c] += stats[c] || 0;
        });
      });
    });

    ["toStatus", "transitions", "toStatusActive", "transitionsActive"].forEach((field) => {
      Object.entries((bucket.crm || {})[field] || {}).forEach(([key, n]) => {
        crm[field][key] = (crm[field][key] || 0) + n;
      });
    });

    Object.entries(bucket.paths || {}).forEach(([funnel, stages]) => {
      const target = (paths[funnel] = paths[funnel] || {});
      Object.entries(stages).forEach(([stage, n]) => {
        target[stage] = (target[stage] || 0) + (n || 0);
      });
    });

    Object.entries(bucket.enrich || {}).forEach(([k, n]) => {
      enrich[k] = (enrich[k] || 0) + (n || 0);
    });
    Object.entries(bucket.apptSplit || {}).forEach(([bak, n]) => {
      apptSplit[bak] = (apptSplit[bak] || 0) + (n || 0);
    });
    Object.entries(bucket.apptRoutes || {}).forEach(([route, n]) => {
      apptRoutes[route] = (apptRoutes[route] || 0) + (n || 0);
    });
    Object.entries(bucket.apptReasons || {}).forEach(([reason, n]) => {
      apptReasons[reason] = (apptReasons[reason] || 0) + (n || 0);
    });
    Object.entries(bucket.apptReasonPaths || {}).forEach(([reason, paden]) => {
      const target = (apptReasonPaths[reason] = apptReasonPaths[reason] || {});
      Object.entries(paden).forEach(([pad, n]) => {
        target[pad] = (target[pad] || 0) + (n || 0);
      });
    });
    Object.entries(bucket.apptRouteReason || {}).forEach(([route, reasons]) => {
      const target = (apptRouteReason[route] = apptRouteReason[route] || {});
      Object.entries(reasons).forEach(([reason, n]) => {
        target[reason] = (target[reason] || 0) + (n || 0);
      });
    });

    if (bucket.value) {
      value.hasData = true;
      value.orders += bucket.value.orders || 0;
      value.revenue += bucket.value.revenue || 0;
      value.viaOrders += bucket.value.viaAutomationOrders || 0;
      value.viaRevenue += bucket.value.viaAutomationRevenue || 0;
      value.mailedOrders += bucket.value.mailedOrders || 0;
      value.mailedRevenue += bucket.value.mailedRevenue || 0;

      const addInto = (target, source) => {
        Object.entries(source || {}).forEach(([key, v]) => {
          const box = (target[key] = target[key] || { orders: 0, revenue: 0 });
          box.orders += v.orders || 0;
          box.revenue += v.revenue || 0;
        });
      };
      addInto(value.byRoute, bucket.value.byRoute);
      addInto(value.byPath, bucket.value.byPath);
      addInto(value.byFlow, bucket.value.byFlow);
      Object.entries(bucket.value.byRouteReason || {}).forEach(([route, reasons]) => {
        value.byRouteReason[route] = value.byRouteReason[route] || {};
        addInto(value.byRouteReason[route], reasons);
      });
    }

    if (bucket.cohort) {
      cohort.hasData = true;
      cohort.entered += bucket.cohort.entered || 0;
      cohort.mailable += bucket.cohort.mailable || 0;
      cohort.reached += bucket.cohort.reached || 0;
    }
  });

  if (cohort.mailable) cohort.coverage = round1(cohort.reached / cohort.mailable * 100);
  return {
    flows,
    crm,
    cohort: cohort.hasData ? cohort : null,
    value: value.hasData ? value : null,
    paths,
    apptSplit,
    enrich,
    apptRoutes,
    apptReasons,
    apptReasonPaths,
    apptRouteReason,
  };
}

// Her-activatie en lead funnel uit de opgetelde statusovergangen. Dezelfde
// definities als server-side: aandelen van de instroom, en een ratio wordt
// onderdrukt bij een te kleine noemer of boven de 100% — dat kan alleen als
// overgangen bij een instroom van voor het bereik horen.
const MIN_DENOM = 25;
const BRANCH_MIN = 10;

function safeRatio(num, den, minimum) {
  if (!den || den < (minimum || MIN_DENOM)) return null;
  const value = Math.round((num / den) * 10000) / 100;
  return value > 100 ? null : value;
}

// Statussen waarvandaan een Re-entered een nieuwe aanvraag is en geen
// heractivatie uit de mailflow. Mailjourney en Not reached staan er bewust
// niet bij: die lead zat in de flow en wordt daardoor teruggehaald.
const REOPEN_FROM = ["Lost", "Not Qualified", "Converted Old SF", "Follow-up"];
const S_PHONE = "Re-entered - Phone Number Changed";

function crmFromSums(crm, prevCrm, value, apptSplit, enrich) {
  enrich = enrich || {};
  const to = crm.toStatus || {};
  const pairs = crm.transitions || {};
  const at = (k) => to[k] || 0;
  const via = (a, b) => pairs[a + ">" + b] || 0;

  // Vanaf de mailflow tellen alleen leads met een actieve reden mee: wie geen
  // journey achter zijn status heeft, is niet door automation bewerkt.
  const toA = crm.toStatusActive || {};
  const pairsA = crm.transitionsActive || {};
  const atA = (k) => toA[k] || 0;
  const viaA = (a, b) => pairsA[a + ">" + b] || 0;

  // Instroom = leads die vanuit New de molen in gaan. Eerder telde dit álle
  // statusovergangen bij elkaar, waardoor een lead met drie wijzigingen drie
  // keer meetelde en het label "leads" niet klopte.
  const newIntake = Object.entries(pairs)
    .filter(([pair]) => pair.startsWith("New>"))
    .reduce((sum, [, n]) => sum + n, 0);
  // Tweede startpunt: een aanvraag van iemand die al in het systeem staat
  // krijgt geen New maar Re-entered. Dat is instroom, geen heractivatie, dus
  // het telt bij de bovenkant van de funnel. Welke vorige statussen daarvoor
  // gelden staat in REOPEN_FROM: afgesloten of slapende dossiers.
  const reopened = REOPEN_FROM.reduce(
    (sum, from) => sum + via(from, "Re-entered") + via(from, S_PHONE),
    0
  );
  const intake = newIntake + reopened;
  const notReached = at("Not reached");
  const mailjourney = atA("Mailjourney");
  // Alleen heropleving vanuit de mailflow telt als SQL. Van alle overgangen
  // naar Re-entered komt maar iets meer dan de helft daarvandaan; de rest
  // wordt vanuit Lost, Not Qualified of Follow-up gezet, meestal met de hand.
  // Heropleving vanuit de mailflow: dat is Mailjourney én Not reached. Alleen
  // Mailjourney tellen liet de leads uit Not reached vallen. Nummerverrijking
  // blijft erbuiten; die staat als eigen aftakking onder de funnel.
  const sql = viaA("Mailjourney", "Re-entered") + viaA("Not reached", "Re-entered");
  const appointment = at("Appointment");
  const phone = atA("Re-entered - Phone Number Changed");
  const mjToSql = sql;
  // Som van de vier bakken uit appointment_split: elke afspraak van een lead
  // die met een actieve reden in de mailflow stond, elk precies een keer.
  const allAppointments = apptSplit
    ? ["notReached", "mailjourney", "direct", "other"]
        .reduce((sum, key) => sum + (apptSplit[key] || 0), 0)
    : null;
  const sqlToAppointment =
    viaA("Re-entered", "Appointment") +
    viaA("Re-entered - Phone Number Changed", "Appointment");

  const delta = (a, b) => (a !== null && b) ? round1(((a - b) / b) * 100) : null;
  const prev = prevCrm ? crmFromSums(prevCrm, null, null, null, null) : null;

  const reactivation = {
    leadIntake: {
      abs: intake,
      share: 100,
      nieuw: newIntake,
      opnieuw: reopened,
    },
    notContact: {
      abs: notReached,
      share: safeRatio(notReached, intake),
      ratio: safeRatio(notReached, intake),
      // Geen uitstroomgetal meer. Not reached is een wachtstand van maximaal
      // vier belpogingen, geen eindstation: wie hier staat gaat daarna naar
      // een andere status, vaak alsnog de mailflow in. Het verschil tussen
      // deze stap en de volgende als "afval" tellen was dus onjuist.
      deltaPct: prev ? delta(safeRatio(notReached, intake), prev.reactivation.notContact.ratio) : null,
    },
    mailjourney: {
      abs: mailjourney,
      share: safeRatio(mailjourney, intake),
      // Niet iedereen komt via Not reached binnen: de grootste groep komt uit
      // Follow-up en een kwart gaat rechtstreeks vanuit New. De pijl toont
      // daarom de instroom in de mailflow als aandeel van de leadinstroom.
      // De mailflow hangt niet onder Not reached maar onder de instroom: hij
      // wordt uit vier richtingen gevoed. Dit is dus het aandeel van de
      // instroom dat in een actieve flow terechtkomt.
      ratio: safeRatio(mailjourney, intake),
      viaNotReached: viaA("Not reached", "Mailjourney"),
      uit: Math.max(mailjourney - sql, 0),
      deltaPct: null,
      herkomst: {
        vanNotReached: viaA("Not reached", "Mailjourney"),
        vanFollowUp: viaA("Follow-up", "Mailjourney"),
        direct: viaA("New", "Mailjourney"),
        overig:
          mailjourney -
          viaA("Not reached", "Mailjourney") -
          viaA("Follow-up", "Mailjourney") -
          viaA("New", "Mailjourney"),
      },
    },
    sql: {
      abs: sql,
      share: safeRatio(sql, intake),
      ratio: safeRatio(mjToSql, mailjourney),
      deltaPct: prev ? delta(safeRatio(mjToSql, mailjourney), prev.reactivation.sql.ratio) : null,
      uit: Math.max(sql - viaA("Re-entered", "Appointment"), 0),
      herkomst: { buitenMailflow: atA("Re-entered") - sql },
    },
    enrichment: {
      fromMailjourney: viaA("Mailjourney", S_PHONE),
      fromNotReached: viaA("Not reached", S_PHONE),
      // Uit de export: de afspraak telt ook als er nog een belpoging of een
      // follow-up tussen zat. Alleen de directe stap tellen laat ruim een
      // derde van het resultaat vallen.
      enrichedLeads: enrich.enriched || 0,
      toAppointment: enrich.appointment || 0,
      toAppointmentDirect: enrich.direct || 0,
      toAppointmentViaDetour: (enrich.appointment || 0) - (enrich.direct || 0),
      toAppointmentRatio: safeRatio(enrich.appointment, enrich.enriched, BRANCH_MIN),
      backToNotReached: viaA(S_PHONE, "Not reached"),
    },
    // Worden die heropgeleefde leads ook echt afspraken?
    appointment: {
      // Alle afspraken uit de mailflow, ook die zonder tussenstap. Het
      // uitklapblok eronder legt uit langs welke route ze binnenkwamen.
      abs: allAppointments !== null ? allAppointments : sqlToAppointment,
      share: safeRatio(
        allAppointments !== null ? allAppointments : sqlToAppointment, intake),
      // De pijl ernaast gaat wél over de ene stap waar hij tussen staat:
      // van een heractivatiestatus rechtstreeks naar een afspraak.
      direct: viaA("Re-entered", "Appointment"),
      // Afspraken die de stap Re-entered oversloegen. Dat is het grootste
      // deel, en precies wat een rechte lijn verzwijgt.
      zonderSql: Math.max(
        (allAppointments !== null ? allAppointments : sqlToAppointment) -
          viaA("Re-entered", "Appointment"), 0),
      // Het tweede pad uit de mailflow: rechtstreeks een afspraak, zonder dat
      // de lead ooit op Re-entered stond. Afgezet tegen de mailflow, want dat
      // is waar dit pad uit vertrekt.
      zonderSqlRatio: safeRatio(
        Math.max((allAppointments !== null ? allAppointments : sqlToAppointment) -
          viaA("Re-entered", "Appointment"), 0), mailjourney),
      ratio: safeRatio(viaA("Re-entered", "Appointment"), sql, BRANCH_MIN),
      deltaPct: null,
    },
    recovered: { count: mjToSql, cpl: null, cac: null },
  };

  const acquisition = {
    funnel: {
      mql: { abs: intake, share: 100, convDeltaPct: null },
      sql: { abs: sql, share: safeRatio(sql, intake), convDeltaPct: null },
      r1: {
        abs: appointment,
        share: safeRatio(appointment, intake),
        convDeltaPct: prev ? delta(safeRatio(appointment, intake), prev.acquisition.funnel.r1.share) : null,
      },
      // Alleen orders die aantoonbaar via de mailflow zijn binnengekomen.
      // Alle orders in het CRM tellen zou hier niets zeggen: daar zit alles
      // in wat nooit een mail heeft gezien.
      order: {
        abs: value ? value.viaOrders : null,
        share: value ? safeRatio(value.viaOrders, intake) : null,
        convDeltaPct: null,
      },
    },
    channels: [],
    totals: { contacts: null, avgCpa: null, avgCpql: null },
  };

  const kern = {
    reactivationRatio: {
      value: safeRatio(mjToSql, mailjourney),
      deltaPct: prev ? delta(safeRatio(mjToSql, mailjourney), prev.kernKpis.reactivationRatio.value) : null,
    },
    mqlToSql: {
      value: safeRatio(sql, intake),
      deltaPct: prev ? delta(safeRatio(sql, intake), prev.kernKpis.mqlToSql.value) : null,
    },
  };

  return { reactivation, acquisition, kernKpis: kern };
}

function round1(value) {
  return Math.round(value * 10) / 10;
}

function pct(num, den) {
  return den ? Math.round((num / den) * 10000) / 100 : null;
}

// Zet opgetelde tellers om naar het emailHealth-contract dat het dashboard al
// kent, inclusief percentages en de mailopbouw per flow.
// Flows die geen echte journey zijn: testopzetten en sends waarvan de
// herkomst niet te bepalen was.
function isNoiseFlow(name) {
  const lowered = (name || "").toLowerCase();
  return lowered.includes("test") || lowered.includes("niet toegewezen");
}

function healthFromSums(summed, prevSummed) {
  const out = {};
  Object.entries(summed.flows || {}).forEach(([key, flow]) => {
    if (key !== "all" && (flow.sent || 0) < 50) return;
    // Testjourneys en sends die niet aan een journey te koppelen zijn horen
    // niet in een overzicht waarop gestuurd wordt.
    if (key !== "all" && isNoiseFlow(key)) return;
    const prev = (prevSummed && prevSummed.flows && prevSummed.flows[key]) || null;
    const rates = (f) => ({
      delivery: pct(f.delivered, f.sent),
      open: pct(f.opens, f.delivered),
      ctr: pct(f.clicks, f.delivered),
      ctor: pct(f.clicks, f.opens),
      unsub: pct(f.unsubs, f.delivered),
      bounce: pct(f.bounces, f.sent),
    });
    const now = rates(flow);
    const before = prev ? rates(prev) : {};

    out[key] = {
      label: flow.label,
      ...Object.fromEntries(COUNTERS.map((c) => [c, flow[c]])),
      ...now,
      spam: null,
      emails: Object.keys(flow.emails || {}).length,
      emailBreakdown: Object.entries(flow.emails || {})
        .map(([name, stats]) => ({ name, ...stats, ...rates(stats) }))
        .filter((e) => e.sent > 0)
        .sort((a, b) => b.sent - a.sent),
      deltas: Object.fromEntries(
        ["delivery", "ctor", "ctr", "open", "unsub", "bounce", "sent"].map((metric) => {
          const a = metric === "sent" ? flow.sent : now[metric];
          const b = metric === "sent" ? (prev && prev.sent) : before[metric];
          return [metric, a !== null && b ? round1(((a - b) / b) * 100) : null];
        })
      ),
    };
  });
  return Object.keys(out).length ? out : { all: { label: "Alle flows" } };
}
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
    applySourceBadges({});
    return;
  }
  if (empty) empty.hidden = true;

  // Alles voor het gekozen bereik uit de dagbuckets optellen. Daardoor kan
  // de lezer elke periode kiezen in plaats van drie vaste vensters.
  const range = clampRange(
    currentRange || rangeForPreset("30", DATA.endDate),
    firstDayWithData(market)
  );
  const span = Math.max(daysBetween(range.start, range.end), 1);
  const prevRange = {
    start: addDays(range.start, -span),
    end: range.start,
  };
  const summed = sumRange(market.days, range.start, range.end);
  const prevSummed = sumRange(market.days, prevRange.start, prevRange.end);
  const derived = crmFromSums(summed.crm, prevSummed.crm, summed.value, summed.apptSplit,
                              summed.enrich);

  // Instroom over het bereik: optelbaar, dus werkt bij elke keuze.
  // 'all' uit de export bevat ook test- en niet-toegewezen sends; daarom
  // hier opnieuw optellen over de flows die het dashboard wél toont.
  const shown = Object.entries(summed.flows || {}).filter(
    ([key]) => key !== "all" && !isNoiseFlow(key)
  );
  const sentAll = shown.reduce((sum, [, f]) => sum + (f.sent || 0), 0);
  const reachAll = ((summed.flows || {}).all || {}).firstTouch;

  const view = {
    ...market,
    emailHealth: healthFromSums(summed, prevSummed),
    // Mails per persoon komt uit het hele meetvenster, niet uit het gekozen
    // bereik. Over een kort bereik zou je alle verzendingen delen door alleen
    // de nieuwe instroom — voor een lange flow geeft dat onmogelijke
    // uitkomsten (26 mails per persoon bij een flow van 15 mails).
    mailsPerPerson: ((market.flowProfile || {}).all || {}).mailsPerPerson,
    reactivation: derived.reactivation,
    acquisition: derived.acquisition,
    kernKpis: {
      ...market.kernKpis,
      ...derived.kernKpis,
      coverage: coverageForPeriod(
        market.kernKpis && market.kernKpis.coverage,
        summed.cohort
      ),
      databaseGrowth: market.kernKpis && market.kernKpis.databaseGrowth,
    },
  };
  const period = { range, prevRange, cohort: summed.cohort, comparable: true };

  applyBindings(document, view);
  // Coverage toont het cohort van de gekozen periode: van de instroom van
  // die periode, wie is er gemaild. De staande voorraad staat eronder als
  // context — dat is een ander getal dat een andere vraag beantwoordt.
  applyCoverageNote(view.kernKpis && view.kernKpis.coverage, period && period.cohort);
  renderValueBlock(summed.value);
  renderApptBlock(summed.apptSplit, summed.apptReasons, summed.apptReasonPaths,
                  summed.apptRoutes);
  renderIntakeSplit(view.reactivation && view.reactivation.leadIntake);
  renderMailjourneyOrigin(view.reactivation && view.reactivation.mailjourney);
  renderSqlOrigin(view.reactivation && view.reactivation.sql);
  // De aftakking alleen tonen als er in dit bereik iets verrijkt is.
  const branch = document.querySelector("[data-enrich-branch]");
  if (branch) {
    const e = (view.reactivation || {}).enrichment || {};
    branch.hidden = !e.enrichedLeads;
  }
  applySourceBadges(market._sources);
  applyPeriodWarning(period);
  buildFlowChips(view.emailHealth || { all: { label: "Alle flows" } });
}

// Sommige periodes hebben nog te weinig historie om met de vorige periode te
// vergelijken. Dat moet zichtbaar zijn, anders lijken lege deltas op nul.
function applyPeriodWarning(period) {
  let el = document.querySelector("[data-period-warn]");
  const header = document.querySelector(".head-meta");
  if (!el && header) {
    el = document.createElement("div");
    el.className = "period-warn";
    el.setAttribute("data-period-warn", "");
    header.appendChild(el);
  }
  if (!el) return;
  if (period && period.comparable === false) {
    el.hidden = false;
    el.textContent = "Geen vergelijking: te weinig historie voor de vorige periode";
  } else {
    el.hidden = true;
  }
}

function applyMeta(data) {
  const fmtDate = (iso) =>
    new Date(iso).toLocaleDateString("nl-NL", { day: "numeric", month: "short", year: "numeric" });

  const stamp = document.querySelector("[data-stamp]");
  if (stamp && data.generated_at) stamp.textContent = "Bijgewerkt: " + fmtDate(data.generated_at);

  // Bereik van de gekozen periode, uit de markt die nu getoond wordt.
  const range = clampRange(
    currentRange || rangeForPreset("30", data.endDate),
    firstDayWithData((data.markets || {})[currentMarket])
  );
  const span = Math.max(daysBetween(range.start, range.end), 1);
  const block = {
    range,
    prevRange: { start: addDays(range.start, -span), end: range.start },
  };
  const showRange = (selector, value) => {
    // Alle plekken bijwerken, niet alleen de eerste: op mobiel staat het
    // periodelabel in de rode balk omdat de kop daar geen ruimte voor heeft.
    const els = document.querySelectorAll(selector);
    if (!els.length || !value) return;
    let tekst;
    if (daysBetween(value.start, value.end) < 1) {
      tekst = "geen dagen in dit bereik";
    } else {
      // end is exclusief; toon de laatste dag die er wél in zit.
      const end = new Date(value.end);
      end.setDate(end.getDate() - 1);
      tekst =
        fmtDate(value.start) + " t/m " + fmtDate(end.toISOString()) +
        // Zeg het als het bereik is ingekort, anders lijkt een half jaar een heel jaar.
        (value.clamped ? " · data begint " + fmtDate(value.start) : "");
    }
    els.forEach((el) => {
      el.textContent = tekst;
    });
  };
  showRange("[data-period-label]", (block && block.range) || data.period);

  // Datumvelden begrenzen tot wat er aan data is.
  const from = document.querySelector("[data-date-from]");
  const to = document.querySelector("[data-date-to]");
  const last = data.endDate ? addDays(data.endDate, -1) : null;
  [from, to].forEach((input) => {
    if (!input) return;
    if (data.firstDay) input.min = data.firstDay;
    if (last) input.max = last;
  });

  const daysEl = document.querySelector("[data-outcome-days]");
  if (daysEl && data.outcome_lookback_days) daysEl.textContent = data.outcome_lookback_days;

  showRange("[data-prev-period-label]", (block && block.prevRange) || data.prev_period);
}

function wirePeriodPicker() {
  const preset = document.querySelector("[data-preset]");
  const custom = document.querySelector("[data-custom-range]");
  const from = document.querySelector("[data-date-from]");
  const to = document.querySelector("[data-date-to]");
  if (!preset) return;

  const refresh = () => {
    applyMeta(DATA);
    applyMarket(currentMarket);
  };

  preset.addEventListener("change", () => {
    if (preset.value === "custom") {
      if (custom) custom.hidden = false;
      // Begin met het huidige bereik, zodat de velden niet leeg staan.
      if (from && to && currentRange) {
        from.value = currentRange.start;
        to.value = addDays(currentRange.end, -1);
      }
      return;
    }
    if (custom) custom.hidden = true;
    currentRange = rangeForPreset(preset.value, DATA.endDate);
    refresh();
  });

  const applyCustom = () => {
    if (!from || !to || !from.value || !to.value) return;
    if (from.value > to.value) return;
    // De datumvelden zijn inclusief; intern is end exclusief.
    currentRange = { start: from.value, end: addDays(to.value, 1) };
    refresh();
  };
  if (from) from.addEventListener("change", applyCustom);
  if (to) to.addEventListener("change", applyCustom);
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
  wirePeriodPicker();
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
  currentRange = rangeForPreset("30", DATA.endDate);
  applyMeta(DATA);
  applyMarket(currentMarket);
}

document.addEventListener("DOMContentLoaded", () => {
  const close = document.querySelector("[data-fd-close]");
  if (close) close.addEventListener("click", closeFlowDetail);
  init();
});


// Het sectiemenu: de link van de sectie die je leest licht op, en de menubalk
// plakt onder de kop in plaats van eroverheen. De hoogte van de kop wordt
// gemeten in plaats van vastgezet, want die verschilt per schermbreedte.
function wireSectionNav() {
  const nav = document.querySelector(".secnav");
  const header = document.querySelector("header");
  if (!nav) return;

  const place = () => {
    const h = header ? header.getBoundingClientRect().height : 0;
    // Zonder opmaak meet de browser nul. Dan niets overschrijven, anders komt
    // de sectie onder de kop terecht; de CSS-waarde is dan de terugval.
    if (!h) return;
    document.documentElement.style.setProperty("--hdr", Math.round(h) + "px");
    document.querySelectorAll("section[id]").forEach((el) => {
      el.style.scrollMarginTop = Math.round(h + nav.offsetHeight + 12) + "px";
    });
  };
  place();
  window.addEventListener("resize", place);

  const links = [...nav.querySelectorAll("a")];
  const sections = links
    .map((a) => document.querySelector(a.getAttribute("href")))
    .filter(Boolean);
  if (!sections.length) return;

  const mark = () => {
    const line = (header ? header.offsetHeight : 0) + nav.offsetHeight + 24;
    let current = 0;
    sections.forEach((el, i) => {
      if (el.getBoundingClientRect().top <= line) current = i;
    });
    links.forEach((a, i) => a.classList.toggle("here", i === current));
  };
  mark();
  window.addEventListener("scroll", mark, { passive: true });
}

wireSectionNav();


// Blokken met meerdere uitklappers krijgen een knop om ze in een keer open of
// dicht te zetten. Zonder die knop zit je bij negen instroomredenen negen keer
// te klikken om een beeld te krijgen.
function wireExpandAll() {
  const blokken = [
    ["[data-appt-block] .vb-head", "[data-appt-block]"],
    ["[data-value-block] .vb-head", "[data-value-block]"],
  ];

  blokken.forEach(([waar, bereik]) => {
    const kop = document.querySelector(waar);
    const blok = document.querySelector(bereik);
    if (!kop || !blok || kop.querySelector("[data-expand-all]")) return;

    const knop = document.createElement("button");
    knop.type = "button";
    knop.className = "expand-all";
    knop.setAttribute("data-expand-all", "");
    kop.appendChild(knop);

    const alle = () => [...blok.querySelectorAll("details")];
    const bijwerken = () => {
      const lijst = alle();
      if (!lijst.length) {
        knop.hidden = true;
        return;
      }
      knop.hidden = false;
      const open = lijst.every((d) => d.open);
      const tekst = open ? "Alles inklappen" : "Alles uitklappen";
      // Alleen schrijven als er iets verandert. De observer hieronder kijkt
      // naar wijzigingen in dit blok, en de knop staat er zelf in: blind
      // toekennen zou hem eindeloos opnieuw laten afgaan.
      if (knop.textContent !== tekst) knop.textContent = tekst;
    };

    knop.addEventListener("click", () => {
      const lijst = alle();
      const openzetten = !lijst.every((d) => d.open);
      lijst.forEach((d) => {
        d.open = openzetten;
      });
      bijwerken();
    });
    // Een losse uitklapper die met de hand opengaat verandert de knoptekst mee.
    blok.addEventListener("toggle", bijwerken, true);
    bijwerken();

    // De kaarten worden bij elke periodewissel opnieuw getekend, dus de knop
    // moet daarna opnieuw kijken wat er staat.
    const kijker = new MutationObserver((muts) => {
      // Wijzigingen in de knop zelf negeren, anders houdt hij zichzelf bezig.
      if (muts.every((m) => knop.contains(m.target))) return;
      bijwerken();
    });
    kijker.observe(blok, { childList: true, subtree: true });
  });
}

wireExpandAll();
