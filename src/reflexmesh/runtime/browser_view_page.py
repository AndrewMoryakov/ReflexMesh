"""The single page of the run view. No external resources; every value from the run is shown as
text (textContent), never as markup, because option names come from pages the run visited."""

PAGE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ReflexMesh: ход прогона</title>
<style>
:root{--bg:#f6f7f9;--fg:#1c2128;--muted:#667085;--card:#fff;--line:#dfe3e8;--accent:#2457d6;
--obs:#9aa4b2;--dec:#2457d6;--adm:#e08a1e;--drv:#2e9d5b;--bad:#c0392b;--ok:#1f7a45}
@media(prefers-color-scheme:dark){:root{--bg:#14171c;--fg:#e6e9ee;--muted:#99a3b3;--card:#1c2128;--line:#2c333d;
--accent:#6b9bff;--obs:#6b7686;--dec:#6b9bff;--adm:#f0a640;--drv:#4cc47f;--bad:#ff7b6b;--ok:#4cc47f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}
main{max-width:1180px;margin:0 auto;padding:16px}
h1{font-size:18px;margin:0 0 4px}.sub{color:var(--muted);margin-bottom:14px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.card b{display:block;font-size:20px}.card span{color:var(--muted);font-size:12px}
.legend{display:flex;gap:14px;flex-wrap:wrap;margin:0 0 10px;color:var(--muted);font-size:12px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600}tr.step{cursor:pointer}tr.step:hover{background:rgba(120,140,180,.08)}
.badge{display:inline-block;padding:1px 7px;border-radius:10px;border:1px solid var(--line);font-size:12px}
.badge.model{border-color:var(--accent);color:var(--accent)}.badge.script{color:var(--muted)}
.ok{color:var(--ok)}.bad{color:var(--bad)}.muted{color:var(--muted)}
.bar{display:flex;height:10px;border-radius:3px;overflow:hidden;background:var(--line);min-width:140px}
.bar span{display:block;height:100%}.ms{font-size:12px;color:var(--muted);margin-top:3px}
tr.detail td{background:rgba(120,140,180,.06)}.detail ul{margin:4px 0 8px;padding-left:18px}
.detail li.chosen{font-weight:700;color:var(--accent)}.kv{display:grid;grid-template-columns:200px 1fr;gap:2px 12px}
.kv div:nth-child(odd){color:var(--muted)}
.state{display:inline-block;padding:2px 9px;border-radius:10px;font-size:12px;background:var(--line)}
.state.running{background:var(--accent);color:#fff}
@media(max-width:700px){.hide-s{display:none}}
</style>
</head>
<body>
<main>
<h1>ReflexMesh: ход прогона <span id="state" class="state">…</span></h1>
<div class="sub" id="meta"></div>
<div class="cards" id="cards"></div>
<div class="legend">
<span><i style="background:var(--obs)"></i>наблюдение</span>
<span><i style="background:var(--dec)"></i>решение (запрос к модели)</span>
<span><i style="background:var(--adm)"></i>допуск (перепроверка страницы)</span>
<span><i style="background:var(--drv)"></i>браузер (само действие)</span>
</div>
<table>
<thead><tr><th>№</th><th>Кто решил</th><th>Решение</th><th class="hide-s">Уверенность / порог</th><th>Итог</th><th>Время</th></tr></thead>
<tbody id="rows"></tbody>
</table>
<p class="muted" id="foot"></p>
</main>
<script nonce="__NONCE__">
"use strict";
const $ = (id) => document.getElementById(id);
const expanded = new Set();
let timer = null;

function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = String(text);
  if (cls) e.className = cls;
  return e;
}
const ms = (v) => v === null || v === undefined ? "—" : v >= 1000 ? (v / 1000).toFixed(2) + " с" : v + " мс";

function renderCards(view) {
  const s = view.summary, box = $("cards");
  box.replaceChildren();
  const items = [
    ["Решений принято", s.decisions + " из " + s.steps + " шагов"],
    ["Время на решения", ms(s.decision_ms_total) + (s.decision_share !== null ? " (" + Math.round(s.decision_share * 100) + "% прогона)" : "")],
    ["Медиана / p95", ms(s.decision_ms_median) + " / " + ms(s.decision_ms_p95)],
    ["Самое долгое", ms(s.decision_ms_max)],
    ["Весь прогон", s.run_ms ? ms(s.run_ms) : "идёт"],
    ["Авторы решений", Object.keys(s.authors).length ? Object.entries(s.authors).map(([k, v]) => k + " × " + v).join(", ") : "—"],
  ];
  for (const [label, value] of items) {
    const c = el("div", null, "card");
    c.append(el("b", value), el("span", label));
    box.append(c);
  }
}

function bar(step) {
  const parts = [["observe_ms", "var(--obs)", "наблюдение"], ["decision_ms", "var(--dec)", "решение"],
                 ["admission_ms", "var(--adm)", "допуск"], ["driver_ms", "var(--drv)", "браузер"]];
  const total = parts.reduce((a, p) => a + (step[p[0]] || 0), 0) || 1;
  const wrap = el("div"), b = el("div", null, "bar");
  b.style.width = Math.max(8, Math.round(100 * total / (window.__maxStep || total))) + "%";
  for (const [key, color, name] of parts) {
    if (!step[key]) continue;
    const seg = el("span");
    seg.style.width = (100 * step[key] / total) + "%";
    seg.style.background = color;
    seg.title = name + ": " + ms(step[key]);
    b.append(seg);
  }
  wrap.append(b, el("div", (step.decision_ms !== undefined ? "решение " + ms(step.decision_ms) + " · " : "") + "всего " + ms(total), "ms"));
  return wrap;
}

function outcome(step) {
  const o = step.outcome || {}, c = el("td");
  const map = {dispatched: ["выполняется", ""], done: ["выполнено", "ok"], driver_error: ["сбой браузера, эффект неизвестен", "bad"],
    rejected: ["отклонено: " + (o.reason || ""), "bad"], finish: ["модель сочла цель достигнутой", "ok"],
    escalate: ["запрошена помощь", "bad"], refused: ["уверенность ниже порога", "bad"], decision_failed: ["решение не получено", "bad"]};
  const m = map[o.kind] || ["—", "muted"];
  c.append(el("span", m[0], m[1]));
  if (step.effect) {
    const e = step.effect, bits = [];
    if (e.url_changed) bits.push("адрес сменился");
    if (e.content_changed) bits.push("страница изменилась");
    if (e.field_shows_slot_value === true) bits.push("поле содержит значение слота");
    if (bits.length) c.append(el("div", "после: " + bits.join(", "), "ms"));
  }
  return c;
}

function decisionCell(step) {
  const c = el("td"), d = step.decision;
  if (!d) {
    const text = step.decision_error ? "ошибка: " + step.decision_error.error
      : step.kind === "final_observation" ? "заключительный взгляд на страницу после finish"
      : step.kind === "awaiting_decision" ? "ждём решения…" : "—";
    c.append(el("span", text, "muted")); return c;
  }
  c.append(el("b", d.action));
  for (const ch of step.chosen) c.append(el("div", ch.label ? ch.label : ch.param + " = " + ch.value, "ms"));
  return c;
}

function detail(step) {
  const tr = el("tr", null, "detail"), td = el("td");
  td.colSpan = 6;
  const kv = el("div", null, "kv"), d = step.decision || {};
  const add = (k, v) => { if (v !== undefined && v !== null && v !== "") kv.append(el("div", k), el("div", v)); };
  add("страница", step.page);
  add("охват наблюдения", step.coverage + (step.coverage_reasons.length ? " (" + step.coverage_reasons.join(", ") + ")" : ""));
  add("запрошенная модель", d.requested_model || step.requested_model);
  add("ответила модель", d.author);
  add("запрос, id", d.request_id);
  add("время запроса по данным провайдера", d.provider_ms !== undefined ? ms(d.provider_ms) : undefined);
  add("время решения у нас", ms(step.decision_ms));
  add("токены (вход / выход)", d.usage ? d.usage.input_tokens + " / " + d.usage.output_tokens : undefined);
  add("цель достигнута, оценка", d.goal_reached);
  if (step.dispatch) add("команда", step.dispatch.operation + (step.dispatch.slot_ref ? " ← " + step.dispatch.slot_ref : ""));
  td.append(kv, el("div", "Что было в меню (выбранное выделено):", "muted"));
  const ul = el("ul");
  const picked = new Set((step.chosen || []).map((x) => String(x.value)));
  for (const o of step.options) {
    const key = String(o.ref).split(":").slice(2).join(":");
    const li = el("li", (o.role || o.operation) + " " + (o.name || ""));
    if (picked.has(key)) li.className = "chosen";
    ul.append(li);
  }
  td.append(ul);
  tr.append(td);
  return tr;
}

function renderRows(view) {
  const tbody = $("rows");
  window.__maxStep = Math.max(1, ...view.steps.map((s) => (s.observe_ms || 0) + (s.decision_ms || 0) + (s.admission_ms || 0) + (s.driver_ms || 0)));
  tbody.replaceChildren();
  for (const step of view.steps) {
    const tr = el("tr", null, "step");
    tr.append(el("td", step.n));
    const who = el("td"), d = step.decision;
    if (d) who.append(el("span", d.author, "badge " + (String(d.author).startsWith("script") ? "script" : "model")));
    else who.append(el("span", "—", "muted"));
    tr.append(who, decisionCell(step));
    const conf = el("td", null, "hide-s");
    if (d) {
      const j = step.judged || {};
      conf.append(el("div", d.confidence !== null && d.confidence !== undefined ? Number(d.confidence).toFixed(2) : "—"));
      if (j.threshold !== null && j.threshold !== undefined) conf.append(el("div", "слабейшее звено " + Number(j.weakest).toFixed(2) + " при пороге " + j.threshold, "ms"));
    }
    tr.append(conf, outcome(step));
    const timeCell = el("td"); timeCell.append(bar(step)); tr.append(timeCell);
    tr.addEventListener("click", () => { expanded.has(step.n) ? expanded.delete(step.n) : expanded.add(step.n); renderRows(view); });
    tbody.append(tr);
    if (expanded.has(step.n)) tbody.append(detail(step));
  }
}

function renderHead(view) {
  const st = $("state");
  st.textContent = {waiting: "ждём начала", running: "идёт", finished: "завершён"}[view.state] || view.state;
  st.className = "state " + view.state;
  const m = view.meta, parts = [];
  if (m.task_id) parts.push("задача " + m.task_id);
  if (m.config_id) parts.push("провайдер " + m.config_id);
  if (view.result) parts.push("итог: " + view.result.attempt_status + " / " + view.result.stop_reason);
  else if (m.terminal) parts.push("итог: " + m.terminal.attempt_status + " / " + m.terminal.stop_reason);
  $("meta").textContent = parts.join(" · ");
  $("foot").textContent = m.start ? "Первая навигация (с запуском браузера): " + ms(m.start.driver_ms) + ". Время решения — разница между записями «запрос» и «ответ» в журнале; оно включает проверку ответа и учёт бюджета." : "";
}

async function tick() {
  let view;
  try {
    const r = await fetch("/api/run", {cache: "no-store"});
    view = await r.json();
  } catch (e) { $("state").textContent = "нет связи с просмотром"; return schedule(3000); }
  renderHead(view); renderCards(view); renderRows(view);
  schedule(view.state === "finished" ? 0 : 1000);
}
function schedule(delay) { clearTimeout(timer); if (delay) timer = setTimeout(tick, delay); }
tick();
</script>
</body>
</html>
"""
