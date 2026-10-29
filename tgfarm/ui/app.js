/* =========================================================================
   tgfarm dashboard — vanilla, no build step, no framework.
   ========================================================================= */
(() => {
  "use strict";

  const API = "";
  const state = {
    view: "overview",
    postFilter: "",
    crosspostFilter: "all",
    data: {},
    refreshTimer: null,
    inFlight: false,
    bestHour: null,
    memoryQuery: "",
    topicQuery: "",
    topicDossier: null,
    platformData: null,
    platformQuery: "",
    platformFilter: "all",
    platformCategory: "Все",
    platformBusy: new Set(),
    authPlatformId: "",
    authSaving: false,
    dzenStatus: null,
    redditMetrics: null,
    redditCandidates: [],
    redditPreview: null,
    redditBusy: false,
  };

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  const fmt = new Intl.NumberFormat("ru-RU");
  const n = (v) => fmt.format(Math.round(Number(v) || 0));
  const pct = (v) => `${((Number(v) || 0) * 100).toFixed(1)}%`;

  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const safeExternalUrl = (value) => {
    try {
      const url = new URL(String(value || ""));
      return url.protocol === "http:" || url.protocol === "https:" ? url.href : "";
    } catch (_) {
      return "";
    }
  };

  const dt = (v) => {
    if (!v) return "—";
    const d = new Date(v);
    if (Number.isNaN(+d)) return "—";
    const diff = (Date.now() - d.getTime()) / 1000;
    if (diff < 60) return "только что";
    if (diff < 3600) return `${Math.floor(diff / 60)} мин назад`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} ч назад`;
    if (diff < 86400 * 6) return `${Math.floor(diff / 86400)} дн назад`;
    return d.toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
  };

  const clock = (v) => {
    if (!v) return "—";
    const d = new Date(v);
    return Number.isNaN(+d) ? "—"
      : d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  };

  // The admin API can force-publish, so it requires a token. The server injects
  // it into this page; a custom header keeps a foreign web page from forging
  // calls to 127.0.0.1.
  const BOOT = window.__TGFARM__ || {};
  const authHeaders = () => {
    const h = { Accept: "application/json" };
    if (BOOT.token && BOOT.tokenRequired) h[BOOT.tokenHeader || "X-TGFarm-Token"] = BOOT.token;
    return h;
  };

  async function api(path) {
    const res = await fetch(API + path, { headers: authHeaders() });
    if (!res.ok) throw new Error(`${path} -> ${res.status}`);
    return res.json();
  }

  function toast(message, kind = "") {
    const el = document.createElement("div");
    el.className = `toast ${kind ? "toast-" + kind : ""}`;
    el.textContent = message;
    $("#toasts").appendChild(el);
    setTimeout(() => {
      el.style.transition = "opacity .3s, transform .3s";
      el.style.opacity = "0";
      el.style.transform = "translateX(14px)";
      setTimeout(() => el.remove(), 320);
    }, 4200);
  }

  /* ------------------------------------------------------------- rendering */
  function renderKPIs(s) {
    const fleet = s.fleet || {};
    const items = [
      { label: "Источников", value: n(s.sources), hint: `из ${s.sources_total || s.sources} активно` },
      { label: "Трендов", value: n(s.ready_clusters), hint: `из ${n(s.clusters)} кластеров` },
      { label: "Постов в очереди", value: n(s.scheduled), hint: `${n(s.gated)} прошли гейты` },
      { label: "Опубликовано", value: n(s.published), hint: `из ${n(s.posts)} всего` },
      { label: "Отклонено", value: n(s.rejected), hint: pct(s.reject_rate) + " от черновиков", kind: s.reject_rate > 0.5 ? "warn" : "" },
      { label: "ERR (средний)", value: pct(s.avg_er), hint: "вовлечённость", kind: s.avg_er >= 0.05 ? "ok" : "" },
      { label: "Обложек", value: n(s.covers), hint: s.videos ? `+ ${n(s.videos)} видео` : "отрисовано" },
      { label: "Кросспостов", value: n(s.crossposts), hint: "во внешние платформы" },
    ];
    $("#kpis").innerHTML = items.map((i) => `
      <div class="kpi ${i.kind ? "k-" + i.kind : ""}">
        <div class="kpi-label">${esc(i.label)}</div>
        <div class="kpi-value">${esc(i.value)}</div>
        <div class="kpi-hint">${esc(i.hint)}</div>
      </div>`).join("");
  }

  function renderReport(report) {
    const hours = report.hours || {};
    const entries = Object.entries(hours)
      .map(([h, v]) => [Number(h), v])
      .sort((a, b) => a[0] - b[0]);
    const best = entries.length
      ? entries.reduce((a, b) => (b[1] > a[1] ? b : a), entries[0])
      : null;
    state.bestHour = best ? best[0] : null;

    const box = $("#report-chart");
    if (!entries.length) {
      box.innerHTML = `<div class="bar-empty">Нет опубликованных постов с метриками</div>`;
    } else {
      const max = Math.max(...entries.map((e) => e[1])) || 1;
      // Always draw the full 0–23 axis. Plotting only the hours that have data
      // turns a single active hour into one full-width block that reads as a
      // solid rectangle instead of a distribution.
      const byHour = new Map(entries.map(([h, v]) => [h, v]));
      const cols = [];
      for (let h = 0; h < 24; h++) {
        const v = byHour.get(h);
        const isBest = best && h === best[0];
        const height = v ? Math.max(4, (v / max) * 100) : 0;
        const tip = v
          ? `${h}:00 — качество ${pct(v)}`
          : `${h}:00 — нет данных`;
        cols.push(`<div class="bar-col ${isBest ? "is-best" : ""} ${v ? "" : "is-empty"}"
             title="${tip}">
          <div class="bar" style="height:${height}%"></div>
          ${h % 3 === 0 || isBest ? `<div class="bar-label">${h}</div>` : `<div class="bar-label"></div>`}
        </div>`);
      }
      box.innerHTML = `<div class="bars">${cols.join("")}</div>`;
    }

    const angles = report.angles || {};
    const angleScore = (v) => v.quality ?? v.er ?? 0;
    const topAngle = Object.entries(angles).sort((a, b) => angleScore(b[1]) - angleScore(a[1]))[0];
    const fleet = report.fleet || {};
    $("#report-breakdown").innerHTML = `
      ${kv("Постов за окно", n(fleet.posts))}
      ${kv("Просмотров", n(fleet.views_total))}
      ${kv("Вовлечений", n(fleet.engagement_total))}
      ${kv("Средний ER", pct(fleet.avg_er))}
      ${kv("Лучший угол", topAngle ? `${esc(topAngle[0])} (${pct(angleScore(topAngle[1]))})` : "—")}
      ${kv("Лучший час", best ? `${best[0]}:00 (${pct(best[1])})` : "—")}
      ${experimentsBlock(report)}`;
    renderPortfolio(report.portfolio || {});
  }

  function renderPortfolio(portfolio) {
    const root = $("#portfolio-insights");
    if (!root) return;
    const channels = portfolio.channels || [];
    const meta = $("#portfolio-meta");
    if (meta) {
      meta.textContent = `${n(portfolio.measured_posts)} зрелых постов · ${channels.length} каналов · окно ${n(portfolio.window_days)} дн.`;
    }

    const priority = { promote: 0, reduce: 1, test: 2, hold: 3 };
    const action = {
      promote: "Усилить тест",
      reduce: "Сократить долю",
      test: "Проверить ещё",
      hold: "Пока наблюдать",
    };
    const dimension = { angle: "угол", hook_style: "стиль хука", format: "формат" };
    const rows = channels.flatMap((channel) => (channel.recommendations || [])
      .filter((item) => item.decision !== "hold")
      .map((item) => ({ ...item, channel_title: channel.title || channel.channel_id })))
      .sort((a, b) => (priority[a.decision] ?? 9) - (priority[b.decision] ?? 9) || Math.abs(b.lift) - Math.abs(a.lift))
      .slice(0, 12);

    if (!portfolio.measured_posts) {
      root.innerHTML = `<div class="empty">Пока нет зрелых постов с измеримыми метриками. Для сравнения нужны данные и минимум 24 часа после публикации.</div>`;
      return;
    }
    if (!rows.length) {
      root.innerHTML = `<div class="empty">Пока нет устойчивых отличий между темами и форматами. Ферма не будет подстраиваться под случайные всплески.</div>`;
      return;
    }
    root.innerHTML = rows.map((item) => {
      const change = item.lift > 0 ? "+" : "";
      const sample = `${n(item.sample_size)} постов · качество ${pct(item.adjusted_quality)} против базы ${pct(item.baseline_quality)} · ${Math.round((item.confidence || 0) * 100)}% веса от наблюдений`;
      return `<div class="item">
        <div class="item-main">
          <div class="item-title">${esc(item.channel_title)} · ${esc(dimension[item.dimension] || item.dimension)}: ${esc(item.key)}</div>
          <div class="item-meta">${esc(sample)}</div>
        </div>
        <div class="item-side">
          <span class="pill">${esc(action[item.decision] || item.decision)}</span>
          <div class="item-meta">${change}${pct(Math.abs(item.lift))}</div>
        </div>
      </div>`;
    }).join("");
  }

  function renderMemory(data) {
    const root = $("#memory-results");
    if (!root) return;
    const index = data.index || {};
    const meta = $("#memory-meta");
    if (meta) {
      const stateText = index.available ? "FTS5 индекс активен" : "резервный поиск";
      meta.textContent = `${n(index.published_posts)} опубликованных постов · ${stateText}`;
    }
    const count = $("#memory-count");
    if (count) {
      count.textContent = data.query
        ? `Найдено: ${n(data.total)} · показано ${n(data.returned)}`
        : `Архив · ${n(data.total)} публикаций`;
    }
    const results = data.results || [];
    if (!results.length) {
      root.innerHTML = `<div class="empty">${data.query ? "Ничего не найдено. Попробуй имя, термин или ключевой факт из старой публикации." : "В архиве пока нет опубликованных постов, сохранённых TgFarm."}</div>`;
      return;
    }
    root.innerHTML = results.map((row) => `
      <div class="memory-result" data-memory-post="${esc(row.id)}" role="button" tabindex="0" title="Открыть полную публикацию">
        <div class="memory-result-head">
          <div>
            <div class="item-title">${esc(row.hook || "(без заголовка)")}</div>
            <div class="item-meta">${esc(row.channel_title || row.channel_id)} · ${esc(dt(row.published_at || row.created_at))} · ${esc(FORMAT_RU[row.post_format] || row.post_format || "пост")}</div>
          </div>
          <span class="tag">${row.score ? `совпадение ${pct(row.score)}` : "в архиве"}</span>
        </div>
        <div class="memory-excerpt">${esc(row.excerpt || (row.body || "").slice(0, 320))}</div>
        <div class="item-meta memory-provenance">${n((row.claims || []).length)} утверждений · ${n((row.sources || []).length)} источников ${row.angle ? "· угол: " + esc(row.angle) : ""}</div>
      </div>`).join("");
    $$("[data-memory-post]").forEach((el) => {
      el.addEventListener("click", () => openDrawer(el.dataset.memoryPost));
      el.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openDrawer(el.dataset.memoryPost);
        }
      });
    });
  }

  const TOPIC_CLAIM_STATUS = {
    reappears: "Есть похожие свежие данные",
    possible_change: "Возможное изменение",
    not_rechecked: "Не перепроверено",
  };

  function renderTopicDossier(dossier) {
    const root = $("#topic-dossier-results");
    if (!root) return;
    if (!dossier) {
      root.innerHTML = `<div class="empty">Введи тему и нажми «Собрать досье», чтобы соединить старые публикации со свежими источниками.</div>`;
      return;
    }
    if (dossier.status !== "ok") {
      root.innerHTML = `<div class="empty">${esc(dossier.message || "Недостаточно данных для досье.")}</div>`;
      return;
    }

    const timeline = dossier.timeline || [];
    const current = dossier.current_sources || [];
    const claims = dossier.claim_checks || [];
    const ideas = dossier.continuation_ideas || [];
    const stats = [
      [ "История", n(dossier.history_count) + (dossier.history_truncated ? "+ постов" : " постов") ],
      [ "Свежие источники", n(dossier.current_source_count) ],
      [ "После последнего поста", n(dossier.updates_found) ],
      [ "Давность публикации", dossier.days_since_latest_post == null ? "нет истории" : n(dossier.days_since_latest_post) + " дн." ],
    ];
    root.innerHTML = `
      <div class="topic-dossier-summary">
        ${stats.map(([label, value]) => `<div class="topic-stat"><div class="item-meta">${esc(label)}</div><strong>${esc(value)}</strong></div>`).join("")}
      </div>
      ${timeline.length ? `<section class="topic-block">
        <h4>Хронология TgFarm</h4>
        ${timeline.slice(-16).reverse().map((row) => `<div class="topic-timeline-row">
          <div class="topic-date">${esc(dt(row.published_at || row.created_at || row.date))}</div>
          <div class="topic-timeline-main">
            <div class="item-title">${esc(row.hook || "(без заголовка)")}</div>
            <div class="item-meta">${esc(row.channel_title || row.channel_id)} · ${esc(row.angle || "—")} · ${esc(FORMAT_RU[row.post_format] || row.post_format || "пост")}</div>
            ${(row.claims || []).length ? `<div class="topic-small">Утверждения: ${esc(row.claims.join(" · "))}</div>` : ""}
          </div>
          <button class="btn btn-ghost btn-sm" type="button" data-topic-post="${esc(row.id)}">Открыть</button>
        </div>`).join("")}
      </section>` : `<div class="topic-block"><div class="empty">В архиве не найдено подходящих публикаций. Тема может быть новой для фермы.</div></div>`}
      ${current.length ? `<section class="topic-block">
        <h4>Свежие материалы по теме</h4>
        ${current.slice(0, 10).map((source) => `<div class="topic-source-row">
          <div class="item-main">
            <div class="item-title">${esc(source.title || "Материал без заголовка")}</div>
            <div class="item-meta">${esc(source.source_id || "источник")} · ${esc(dt(source.date || source.published_at || source.fetched_at))} · релевантность ${pct(source.score || 0)}</div>
            ${source.excerpt ? `<div class="topic-small">${esc(source.excerpt.slice(0, 300))}</div>` : ""}
          </div>
          ${safeExternalUrl(source.url) ? `<a class="btn btn-ghost btn-sm" href="${esc(safeExternalUrl(source.url))}" target="_blank" rel="noopener">Источник</a>` : ""}
        </div>`).join("")}
      </section>` : `<div class="topic-block"><h4>Свежие материалы</h4><div class="empty">В сохранённых источниках выбранного периода не найдено достаточно совпадений. Это не означает, что обновлений нет.</div></div>`}
      ${claims.length ? `<section class="topic-block">
        <h4>Старые утверждения и свежие данные</h4>
        <p class="card-sub topic-disclaimer">Автоматическая текстовая сверка, не окончательный фактчек. Разные единицы, периоды и метрики могут объяснять расхождения.</p>
        ${claims.slice(0, 12).map((claim) => `<div class="topic-claim-row">
          <div><span class="tag ${claim.current_status === "possible_change" ? "tag-warn" : claim.current_status === "reappears" ? "tag-ok" : ""}">${esc(TOPIC_CLAIM_STATUS[claim.current_status] || claim.current_status)}</span>
            <div class="topic-claim-text">${esc(claim.claim)}</div>
            <div class="item-meta">Исторически подтверждено: ${claim.historically_verified ? "да" : "нет"} · совпадение ${pct(claim.evidence_score || 0)}</div>
            <div class="topic-small">${esc(claim.current_note || "")}</div>
            ${(claim.current_sources || []).map((url) => safeExternalUrl(url) ? `<a class="topic-source-link" href="${esc(safeExternalUrl(url))}" target="_blank" rel="noopener">Открыть свежий источник</a>` : "").join("")}
          </div>
        </div>`).join("")}
      </section>` : ""}
      ${ideas.length ? `<section class="topic-block">
        <h4>Варианты продолжения</h4>
        ${ideas.map((idea) => `<div class="topic-idea">
          <strong>${esc(idea.title)}</strong>
          <div class="topic-small">${esc(idea.reason)}</div>
          ${safeExternalUrl(idea.source_url) ? `<a class="topic-source-link" href="${esc(safeExternalUrl(idea.source_url))}" target="_blank" rel="noopener">Опорный источник</a>` : ""}
        </div>`).join("")}
      </section>` : ""}
      <details class="topic-block topic-brief">
        <summary>Контекст для следующей генерации</summary>
        <pre>${esc(dossier.brief || "")}</pre>
        <button class="btn btn-ghost btn-sm" type="button" id="copy-topic-brief">Скопировать контекст</button>
      </details>
      <p class="card-sub topic-disclaimer">${(dossier.limitations || []).map(esc).join(" ")}</p>
    `;

    $$("[data-topic-post]").forEach((el) => {
      el.addEventListener("click", () => openDrawer(el.dataset.topicPost));
    });
    $("#copy-topic-brief")?.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(dossier.brief || "");
        toast("Контекст для продолжения скопирован", "ok");
      } catch (_) {
        toast("Не удалось скопировать контекст", "err");
      }
    });
  }

  async function submitTopicDossier() {
    const field = $("#topic-query");
    state.topicQuery = (field?.value || $("#memory-query")?.value || "").trim();
    if (field && state.topicQuery) $("#memory-query").value = state.topicQuery;
    switchView("memory");
    const root = $("#topic-dossier-results");
    if (!state.topicQuery) {
      if (root) root.innerHTML = `<div class="empty">Сначала введи тему: например, название продукта, компании или исследования.</div>`;
      return;
    }
    if (root) root.innerHTML = `<div class="empty">Собираю историю, свежие источники и проверку старых утверждений…</div>`;
    try {
      const dossier = await api(`/api/memory/topic?days=30&archive_limit=300&query=${encodeURIComponent(state.topicQuery)}`);
      state.topicDossier = dossier;
      renderTopicDossier(dossier);
    } catch (error) {
      if (root) root.innerHTML = `<div class="empty">Не удалось собрать досье: ${esc(error.message)}</div>`;
      toast("Ошибка досье темы: " + error.message, "err");
    }
  }

  async function submitMemorySearch() {
    const field = $("#memory-query");
    state.memoryQuery = (field?.value || "").trim();
    switchView("memory");
    const root = $("#memory-results");
    if (root) root.innerHTML = `<div class="empty">Ищу по всему архиву…</div>`;
    try {
      const data = await api(`/api/memory?limit=20&query=${encodeURIComponent(state.memoryQuery)}`);
      renderMemory(data);
    } catch (error) {
      if (root) root.innerHTML = `<div class="empty">Поиск не удался: ${esc(error.message)}</div>`;
      toast("Ошибка поиска архива: " + error.message, "err");
    }
  }

  function experimentsBlock(report) {
    const hold = report.holdout || {};
    const a = hold.a || hold;
    const ho = hold.holdout || {};
    const guard = report.guardrail || {};
    const styles = report.hook_styles || {};
    const styleLine = Object.entries(styles).map(([k, v]) => `${esc(k)} ${pct(v.quality || 0)}`).join(" · ");
    const parts = [];
    if (ho.n) parts.push(`holdout: ${n(ho.n)} постов, качество ${pct(ho.quality || 0)} против ${pct(a.quality || 0)} у основных`);
    if (guard.posts) parts.push(`разнообразие: ${pct(guard.duplicate_gram_share || 0)} повторов 6-грамм`);
    if (styleLine) parts.push(`стили хуков: ${styleLine}`);
    if (!parts.length) return "";
    return `<div class="kv-item" style="grid-column:1/-1"><div class="kv-key">Эксперименты</div>
      <div class="kv-val">${parts.map(esc).join("<br>")}</div></div>`;
  }

  const kv = (k, v) => `<div class="kv-item"><div class="kv-key">${esc(k)}</div><div class="kv-val">${esc(v)}</div></div>`;

  function renderAIUsage(data) {
    const today = data.today || {};
    const totals = data.totals || {};
    const budget = Math.max(0, Number(data.daily_budget) || 0);
    const remaining = Math.max(0, Number(today.remaining) || 0);
    const charged = Math.max(0, Number(today.charged) || 0);
    const stale = Math.max(0, Number(today.stale_reservations) || 0, Number(totals.stale_reservations) || 0);
    const remainingTone = budget === 0 || remaining === 0 ? "k-err"
      : remaining / budget <= 0.2 ? "k-warn" : "k-ok";
    const cards = [
      { label: "Лимит сегодня", value: `${n(charged)} / ${n(budget)}`, hint: "учтённые и активные резервы", tone: remainingTone },
      { label: "Осталось вызовов", value: n(remaining), hint: "до дневного лимита", tone: remainingTone },
      { label: "Онлайн за период", value: n(totals.used), hint: `вызовов за ${n(data.period_days || 7)} дн.`, tone: "k-ok" },
      { label: "Офлайн-фолбэки", value: n(today.fallbacks), hint: `всего за период: ${n(totals.fallbacks)}`, tone: today.fallbacks ? "k-warn" : "k-ok" },
      { label: "Зависшие резервы", value: n(stale), hint: "старше 15 минут", tone: stale ? "k-err" : "k-ok" },
      { label: "Средняя задержка", value: `${n(totals.average_latency_ms)} мс`, hint: "только успешные онлайн-вызовы", tone: "" },
    ];
    $("#ai-usage-kpis").innerHTML = cards.map((item) => `
      <div class="kpi ${item.tone}">
        <div class="kpi-label">${esc(item.label)}</div>
        <div class="kpi-value">${esc(item.value)}</div>
        <div class="kpi-hint">${esc(item.hint)}</div>
      </div>`).join("");
    $("#ai-usage-meta").textContent =
      `За последние ${n(data.period_days || 7)} дней · лимит считает онлайн-вызовы, не деньги`;

    const providers = (data.by_provider || []).slice(0, 4);
    const stages = (data.by_stage || []).slice(0, 4);
    const providerLine = providers.length
      ? providers.map((item) => `${item.name}: ${n(item.used)} онлайн, ${n(item.fallbacks)} fallback`).join(" · ")
      : "пока нет вызовов";
    const stageLine = stages.length
      ? stages.map((item) => `${item.name}: ${n(item.calls)}`).join(" · ")
      : "пока нет вызовов";
    $("#ai-usage-breakdown").innerHTML =
      kv("Провайдеры", providerLine) + kv("Этапы", stageLine) +
      (stale ? kv("Требует внимания", `${n(stale)} резерв(ов) не завершены больше 15 минут. Проверь зависший процесс.`) : "");
  }

  function renderPipeline(runs) {
    const byRun = new Map();
    for (const r of runs) {
      if (r.stage === "cycle") continue;
      if (!byRun.has(r.run_id)) byRun.set(r.run_id, []);
      byRun.get(r.run_id).push(r);
    }
    const latest = runs.find((r) => r.stage === "cycle");
    const stages = byRun.get(latest?.run_id) || [];
    const order = ["s1_collect", "s2_trends", "s4_generate", "s5_quality", "s6_visual",
                   "s7_publish", "s9_crosspost", "s8_analytics"];
    const names = { s1_collect: "сбор", s2_trends: "тренды", s4_generate: "генерация",
                    s5_quality: "качество", s6_visual: "обложки", s7_publish: "публикация",
                    s9_crosspost: "кросспост", s8_analytics: "аналитика" };

    if (!stages.length) {
      $("#pipeline-visual").innerHTML = `<div class="empty">Прогонов ещё не было</div>`;
      return;
    }
    const map = Object.fromEntries(stages.map((s) => [s.stage, s]));
    $("#pipeline-visual").innerHTML = order.map((key) => {
      const s = map[key];
      const cls = !s ? "" : s.status === "ok" ? "stage-ok" : "stage-err";
      const detail = s ? (s.status === "ok" ? `${s.out} шт · ${s.ms} мс` : esc((s.error || "").slice(0, 40))) : "не запускался";
      return `<div class="stage ${cls}"><span class="stage-dot"></span>
                <span><b>${names[key]}</b><br>${detail}</span></div>`;
    }).join("");
  }

  function renderTrends(trends) {
    const html = trends.length ? trends.map((t) => `
      <div class="item">
        <div class="item-main">
          <div class="item-title">${esc(t.label || t.id)}</div>
          <div class="item-meta">${esc(t.niche || "—")} · ${t.items} материалов · ${t.outlets.length} изданий: ${esc((t.outlets || []).slice(0, 3).join(", "))}</div>
        </div>
        <div class="item-side">
          <span class="tag ${t.status === "ready" ? "tag-ok" : ""}">${esc(t.status)}</span>
          <div class="heat"><i style="width:${Math.round((t.heat || 0) * 100)}%"></i></div>
          <div class="heat-val">${(t.heat || 0).toFixed(2)}</div>
        </div>
      </div>`).join("")
      : `<div class="empty">Трендов нет — тепло ниже порога</div>`;
    $("#trends-list").innerHTML = html;
    $("#overview-trends").innerHTML = trends.slice(0, 6).map((t) => `
      <div class="item">
        <div class="item-main">
          <div class="item-title">${esc(t.label || t.id)}</div>
          <div class="item-meta">${esc(t.niche || "—")} · ${t.items} материалов</div>
        </div>
        <div class="item-side">
          <div class="heat"><i style="width:${Math.round((t.heat || 0) * 100)}%"></i></div>
          <div class="heat-val">${(t.heat || 0).toFixed(2)}</div>
        </div>
      </div>`).join("") || `<div class="empty">Трендов нет — тепло ниже порога</div>`;
  }

  const STATUS_TAG = {
    published: "tag-ok", scheduled: "tag-acc", gated: "tag-ok",
    publish_unknown: "tag-warn", rejected: "tag-err", failed: "tag-err", draft: "",
  };
  const STATUS_RU = {
    published: "опубликован", scheduled: "в очереди", gated: "прошёл гейты",
    publish_unknown: "нужна сверка", rejected: "отклонён", failed: "ошибка публикации", draft: "черновик",
  };
  const FORMAT_RU = {
    flash: "картинка + коротко", stat: "цифра", news: "новость",
    analysis: "разбор", article: "статья", video: "видео",
  };
  const PLATFORM_RU = { telegram: "Telegram", dzen: "Дзен", vk: "VK", max: "MAX", x: "X" };

  function postCard(p) {
    const cover = p.cover
      ? `<img src="/api/media/${encodeURIComponent(p.cover.id)}" alt="" loading="lazy">`
      : `<div class="no-cover">${p.post_format === "video" ? "▶ видео" : "обложка не отрисована"}</div>`;
    const reason = p.reject_reason ? `<span class="tag tag-err">${esc(p.reject_reason.split(":")[0])}</span>` : "";
    const when = p.published_at ? dt(p.published_at)
               : p.scheduled_at ? `в ${clock(p.scheduled_at)}`
               : dt(p.created_at);
    return `
      <article class="pcard" data-post="${esc(p.id)}">
        <div class="pcard-cover">${cover}</div>
        <div class="pcard-body">
          <div class="item-meta">${esc(p.channel_title || p.channel_id)} · ${esc(when)}</div>
          <div class="pcard-hook">${esc(p.hook)}</div>
          <div class="pcard-text">${esc(p.excerpt || "")}</div>
          <div class="pcard-foot">
            <span class="tag ${STATUS_TAG[p.status] || ""}">${esc(STATUS_RU[p.status] || p.status)}</span>
            <span class="tag">${esc(FORMAT_RU[p.post_format] || p.post_format)}</span>
            ${p.platform === "dzen" ? `<span class="tag">Дзен</span>` : ""}
<span class="tag">${n(p.chars)} симв.</span>
            ${reason}
            <span class="pcard-score">${p.impressions ? `показы ${n(p.impressions)} · открытия ${n(p.opens)}` : `ER ${pct(p.er)}`}</span>
          </div>
        </div>
      </article>`;
  }

  function renderPosts(posts) {
    $("#posts-grid").innerHTML = posts.length
      ? posts.map(postCard).join("")
      : `<div class="empty">Постов нет</div>`;

    const queue = posts.filter((p) => ["publish_unknown", "scheduled", "gated", "failed"].includes(p.status)).slice(0, 6);
    // With nothing waiting, an empty panel tells the operator nothing. Fall back
    // to the most recent posts so the card always reflects the last real output.
    const title = $("#overview-queue-title");
    if (queue.length) {
      title.textContent = "Ближайшие публикации";
      $("#overview-queue").innerHTML = queue.map((p) => `
      <div class="item" data-post="${esc(p.id)}">
        <div class="item-main">
          <div class="item-title">${esc(p.hook)}</div>
          <div class="item-meta">${esc(p.channel_title || p.channel_id)} · ${p.status === "publish_unknown" ? "проверьте площадку перед повтором" : p.status === "failed" ? "исправьте причину и повторите вручную" : (p.scheduled_at ? clock(p.scheduled_at) : "ждёт слота")}</div>
        </div>
        <div class="item-side"><span class="tag ${STATUS_TAG[p.status] || ""}">${esc(STATUS_RU[p.status] || FORMAT_RU[p.post_format] || p.post_format)}</span></div>
      </div>`).join("");
    } else if (posts.length) {
      title.textContent = "Последние публикации";
      $("#overview-queue").innerHTML = posts.slice(0, 6).map((p) => `
      <div class="item" data-post="${esc(p.id)}">
        <div class="item-main">
          <div class="item-title">${esc(p.hook)}</div>
          <div class="item-meta">${esc(p.channel_title || p.channel_id)} · ${p.published_at ? clock(p.published_at) : "—"}</div>
        </div>
        <div class="item-side"><span class="tag ${STATUS_TAG[p.status] || ""}">${esc(STATUS_RU[p.status] || p.status)}</span></div>
      </div>`).join("");
    } else {
      title.textContent = "Ближайшие публикации";
      $("#overview-queue").innerHTML = `<div class="empty">Постов пока нет</div>`;
    }

    $$(".pcard, #overview-queue .item").forEach((el) => {
      el.addEventListener("click", () => openDrawer(el.dataset.post));
    });
  }

  const NICHE_COLOR = {
    tech: "#5b8cff", software: "#9b5cf6", business: "#34d399", crypto: "#f5a524",
    science: "#38bdf8", health: "#10b981", local: "#f97316", general: "#ffcc00",
  };

  function renderChannels(channels, personas) {
    $("#channels-grid").innerHTML = channels.map((c) => {
      const color = NICHE_COLOR[c.niche] || "#5b8cff";
      const quota = c.posts_per_day || 1;
      const used = c.posts_today || 0;
      const letter = (c.title || c.id).trim().charAt(0).toUpperCase();
      const platform = c.platform || "telegram";
      const platformLabel = PLATFORM_RU[platform] || platform;
      const connected = platform === "dzen"
        ? `<span class="tag tag-ok">Дзен API</span>`
        : (platform === "vk" || platform === "max" || platform === "x")
          ? `<span class="tag tag-ok">кросспост</span>`
          : c.tg_chat_id
          ? `<span class="tag tag-ok">подключён</span>`
          : `<span class="tag tag-warn">нет chat_id</span>`;
      return `
        <article class="chcard" style="--ch:${color}">
          <div class="chcard-top">
            <div class="chdot">${esc(letter)}</div>
            <div>
              <h3>${esc(c.title)}</h3>
              <div class="chcard-sub">${esc(platformLabel)} · ${esc(c.niche)} · ${esc(c.timezone)} · ${c.enabled ? "включён" : "выключен"}</div>
            </div>
          </div>
          <div>
            <div class="chcard-sub" style="display:flex;justify-content:space-between">
              <span>Сегодня: ${used} из ${quota}</span><span>${Math.round((used / quota) * 100)}%</span>
            </div>
            <div class="meter"><i style="width:${Math.min(100, (used / quota) * 100)}%"></i></div>
          </div>
          <div class="pcard-foot">
            <span class="tag">${esc(c.id)}</span>
            ${connected}
          </div>
        </article>`;
    }).join("") || `<div class="empty">Каналы не сконфигурированы</div>`;
  }

  const PLATFORM_PRIORITY_ORDER = { core: 0, P1: 1, P2: 2, P3: 3 };
  const PLATFORM_STATUS_CLASS = {
    configured: "ok",
    needs_setup: "warn",
    paused: "muted",
    planned: "plan",
    not_implemented: "muted",
  };

  function renderPlatforms(payload) {
    state.platformData = payload || { platforms: [], summary: {} };
    const summary = state.platformData.summary || {};
    const items = state.platformData.platforms || [];
    const stats = [
      { label: "Включено", value: n(summary.enabled), hint: "управляется переключателями" },
      { label: "Настройки найдены", value: n(summary.configured), hint: "наличие ключей / профиля" },
      { label: "В плане", value: n(summary.planned), hint: "следующие интеграции" },
      { label: "Нужна настройка", value: n(summary.needs_setup), hint: "включены, но не готовы" },
    ];
    $("#platform-summary").innerHTML = stats.map((item) => `
      <div class="platform-summary-card">
        <span>${esc(item.label)}</span><strong>${esc(item.value)}</strong><small>${esc(item.hint)}</small>
      </div>`).join("");
    $("#platform-catalog-count").textContent = `${n(items.length)} площадок · ${n(summary.supported)} с рабочим адаптером`;
    const navCount = $("#platforms-nav-count");
    if (navCount) navCount.textContent = n(summary.enabled);
    const note = $("#platform-note");
    if (note) note.textContent = state.platformData.note || "Для будущих интеграций нужен отдельный проверенный адаптер.";
    renderPlatformCategories(items);
    renderPlatformCards();
  }

  function renderPlatformCategories(items) {
    const categories = ["Все", ...new Set(items.map((item) => item.category).filter(Boolean))];
    if (!categories.includes(state.platformCategory)) state.platformCategory = "Все";
    const container = $("#platform-categories");
    if (!container) return;
    container.innerHTML = categories.map((category) => `
      <button type="button" class="platform-category ${state.platformCategory === category ? "is-active" : ""}"
        data-platform-category="${esc(category)}">${esc(category)}</button>`).join("");
  }

  function renderPlatformCards() {
    const container = $("#platforms-grid");
    if (!container) return;
    const all = (state.platformData && state.platformData.platforms) || [];
    const query = (state.platformQuery || "").trim().toLocaleLowerCase("ru-RU");
    const visible = all.filter((item) => {
      const haystack = [
        item.name, item.id, item.category, item.market, item.description,
        item.requirements, item.roles_label, item.status_label,
      ].join(" ").toLocaleLowerCase("ru-RU");
      if (query && !haystack.includes(query)) return false;
      if (state.platformCategory !== "Все" && item.category !== state.platformCategory) return false;
      if (state.platformFilter === "enabled" && !(item.supported && item.enabled)) return false;
      if (state.platformFilter === "setup" && !(item.supported && item.enabled && !item.configured)) return false;
      if (state.platformFilter === "planned" && !item.planned) return false;
      return true;
    }).sort((a, b) => {
      const group = (p) => p.supported ? (p.enabled ? 0 : 1) : (p.planned ? 2 : 3);
      return group(a) - group(b)
        || (PLATFORM_PRIORITY_ORDER[a.priority] ?? 9) - (PLATFORM_PRIORITY_ORDER[b.priority] ?? 9)
        || a.name.localeCompare(b.name, "ru");
    });

    if (!visible.length) {
      container.innerHTML = `<div class="card platform-empty">
        <div class="platform-empty-mark">⌕</div>
        <h3>Ничего не найдено</h3>
        <p>Измени запрос или выбранный фильтр.</p>
        <button class="btn btn-ghost btn-sm" data-platform-reset>Сбросить фильтры</button>
      </div>`;
      return;
    }

    container.innerHTML = visible.map((item) => {
      const supported = Boolean(item.supported);
      const busy = state.platformBusy.has(item.id);
      const statusClass = PLATFORM_STATUS_CLASS[item.status] || "muted";
      const priority = item.priority === "core" ? "Встроено"
        : item.priority === "P1" ? "Приоритет 1"
        : item.priority === "P2" ? "Приоритет 2" : "Приоритет 3";
      const switchMarkup = supported
        ? `<button type="button" class="platform-switch ${item.enabled ? "is-on" : ""} ${busy ? "is-busy" : ""}"
             data-platform-toggle="${esc(item.id)}" role="switch" aria-checked="${item.enabled ? "true" : "false"}"
             aria-label="${item.enabled ? "Отключить" : "Включить"} ${esc(item.name)}" ${busy ? "disabled" : ""}>
             <span class="platform-switch-track"><span></span></span>
             <span class="platform-switch-label">${item.enabled ? "ВКЛ" : "ВЫКЛ"}</span>
           </button>`
        : `<span class="platform-no-adapter"><span></span> Нет адаптера</span>`;
      const footerAction = supported
        ? `<a class="platform-doc-link" href="${esc(item.docs_url || "#")}" target="_blank" rel="noopener noreferrer">Документация <span aria-hidden="true">↗</span></a>`
        : `<button type="button" class="btn ${item.planned ? "btn-plan-added" : "btn-ghost"} btn-sm"
             data-platform-plan="${esc(item.id)}" ${busy ? "disabled" : ""}>
             ${item.planned ? "✓ В плане" : "+ В план"}
           </button>`;
      const auth = item.auth || {};
      const authCheckedDate = auth.checked_at ? new Date(auth.checked_at) : null;
      const authCheckedLabel = authCheckedDate && !Number.isNaN(authCheckedDate.getTime())
        ? `Проверено: ${authCheckedDate.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}`
        : "";
      const authCheckedMarkup = supported && authCheckedLabel
        ? `<small class="platform-auth-checked">${esc(authCheckedLabel)}</small>`
        : "";
      let authStatus = auth.status || (supported ? "needs_setup" : "not_integrated");
      let authLabel = auth.status_label || (supported ? "Не настроено" : "Интеграция не готова");
      if (item.id === "dzen" && state.dzenStatus) {
        if (state.dzenStatus.authorized === true) {
          authStatus = "authorized";
          authLabel = "Дзен: авторизован";
        } else if (state.dzenStatus.authorized === false) {
          authStatus = "needs_setup";
          authLabel = "Нужен вход в Дзен";
        }
      }
      const authStyle = authStatus === "authorized" ? "is-authorized"
        : authStatus === "failed" ? "is-failed"
        : authStatus === "not_integrated" ? "is-not-integrated" : "is-pending";
      const authSetupLabel = auth.method === "browser" ? "Войти в Дзен"
        : auth.method === "oauth" ? "Подключить через OAuth"
        : auth.method === "credentials" ? "Настроить доступ" : "Открыть сервис ↗";
      const authSetup = `<button type="button" class="btn btn-ghost btn-sm platform-auth-button"
          data-platform-auth="${esc(item.id)}" ${busy ? "disabled" : ""}>${authSetupLabel}</button>`;
      const authConfig = supported && ["x", "threads", "linkedin", "youtube"].includes(item.id) && auth.method === "oauth"
        ? `<button type="button" class="btn btn-ghost btn-sm platform-auth-config" data-platform-auth-config="${esc(item.id)}" ${busy ? "disabled" : ""}>Настроить приложение</button>`
        : "";
      const authCheck = supported && (auth.method === "credentials" || (auth.method === "oauth" && item.configured))
        ? `<button type="button" class="btn btn-primary btn-sm platform-auth-check"
             data-platform-auth-check="${esc(item.id)}" ${busy ? "disabled" : ""}>Проверить</button>`
        : "";
      const readyHint = supported
        ? (item.configured ? item.connection_detail : item.connection_detail)
        : (item.planned ? "Выбрана для следующей волны интеграций." : "Можно добавить в список следующих интеграций.");
      return `
        <article class="platform-card ${supported ? "is-supported" : "is-planned"} ${item.enabled && supported ? "is-enabled" : ""}"
          style="--platform-color:${esc(item.color || "#7890b2")}">
          <div class="platform-card-head">
            <div class="platform-brand-mark">${esc(item.symbol || item.name.slice(0, 2))}</div>
            <div class="platform-card-title">
              <h3>${esc(item.name)}</h3>
              <div class="platform-card-meta">${esc(item.category)} <span>·</span> ${esc(item.market)}</div>
            </div>
            <div class="platform-card-action">${switchMarkup}</div>
          </div>
          <p class="platform-description">${esc(item.description)}</p>
          <div class="platform-badge-row">
            <span class="platform-status status-${statusClass}"><i></i>${esc(item.status_label || "Статус неизвестен")}</span>
            <span class="platform-priority ${item.priority === "core" ? "priority-core" : ""}">${esc(priority)}</span>
          </div>
          <div class="platform-connection">
            <span class="platform-connection-icon">${supported ? (item.configured ? "✓" : "!") : "↗"}</span>
            <div><strong>${supported ? (item.configured ? "Настройки найдены" : "Нужна настройка") : "Пока не подключена"}</strong>
              <p>${esc(readyHint)}</p>
            </div>
          </div>
          <div class="platform-auth-block ${authStyle}">
            <div class="platform-auth-copy">
              <span class="platform-auth-dot" aria-hidden="true"></span>
              <div><strong>${esc(authLabel)}</strong><p>${esc(auth.detail || auth.note || (supported ? "Статус доступа ещё не проверен." : "Открытие сайта не подключает публикацию к ферме."))}</p>${authCheckedMarkup}</div>
            </div>
            <div class="platform-auth-actions">${authConfig}${authSetup}${authCheck}</div>
          </div>
          <div class="platform-requirements"><span>ТРЕБУЕТСЯ</span><p>${esc(item.requirements || "Информация уточняется.")}</p></div>
          <div class="platform-card-footer">
            <span class="platform-role">${esc(item.roles_label || "")}</span>
            ${footerAction}
          </div>
        </article>`;
    }).join("");
  }


  function setPlatformAuthMessage(message, kind = "") {
    const target = $("#platform-auth-message");
    if (!target) return;
    target.textContent = message || "";
    target.className = "platform-auth-message" + (kind ? " is-" + kind : "");
  }

  function closePlatformAuthDialog() {
    const dialog = $("#platform-auth-dialog");
    if (dialog?.open) dialog.close();
    state.authPlatformId = "";
    state.authSaving = false;
  }

  async function openPlatformAuthorization(platformId, forceCredentials = false) {
    const item = (state.platformData?.platforms || []).find((entry) => entry.id === platformId);
    if (!item) return;
    const auth = item.auth || {};
    if (auth.method === "browser" && platformId === "dzen") {
      window.tgfarmDzenLogin();
      return;
    }
    if (auth.method === "oauth" && platformId === "mastodon" && !forceCredentials) {
      const instance = window.prompt(
        "Укажи адрес Mastodon-сервера (например mastodon.social). Откроется официальное окно согласия на чтение профиля и публикацию постов.",
        auth.instance_url || "https://mastodon.social",
      );
      if (!instance || !instance.trim()) return;
      try {
        const result = await post("/api/platforms/mastodon/auth/start", { instance: instance.trim() });
        const authorizeUrl = safeExternalUrl(result.authorize_url);
        if (!authorizeUrl) throw new Error("Сервер вернул некорректную ссылку авторизации.");
        window.location.assign(authorizeUrl);
      } catch (error) {
        toast("Не удалось начать Mastodon OAuth: " + (error?.message || error), "err");
      }
      return;
    }
    if (auth.method === "oauth" && platformId === "threads" && !forceCredentials) {
      const appId = (auth.fields || []).find((field) => field.key === "THREADS_APP_ID");
      const appSecret = (auth.fields || []).find((field) => field.key === "THREADS_APP_SECRET");
      if (!appId?.has_value || !appSecret?.has_value) {
        toast("Сначала нажми «Настроить приложение» и сохрани Threads App ID/Secret.", "err");
        return;
      }
      const callbackUri = window.location.origin.replace(/\/$/, "") + "/api/platforms/threads/auth/callback";
      const proceed = window.confirm(
        "В Meta Developer Portal → Threads API добавь этот OAuth Redirect URI и разрешения threads_basic и threads_content_publish:\n\n"
        + callbackUri
        + "\n\nВ режиме разработки добавь аккаунт в роли/тестеры приложения. Продолжить?",
      );
      if (!proceed) return;
      try {
        const result = await post("/api/platforms/threads/auth/start", {});
        if (result.redirect_uri !== callbackUri) {
          throw new Error("Адрес callback отличается от текущего адреса панели: " + result.redirect_uri);
        }
        const authorizeUrl = safeExternalUrl(result.authorize_url);
        if (!authorizeUrl) throw new Error("Threads вернул некорректную ссылку авторизации.");
        window.location.assign(authorizeUrl);
      } catch (error) {
        toast("Не удалось начать Threads OAuth: " + (error?.message || error), "err");
      }
      return;
    }

    if (auth.method === "oauth" && platformId === "linkedin" && !forceCredentials) {
      const clientId = (auth.fields || []).find((field) => field.key === "LINKEDIN_CLIENT_ID");
      const clientSecret = (auth.fields || []).find((field) => field.key === "LINKEDIN_CLIENT_SECRET");
      if (!clientId?.has_value || !clientSecret?.has_value) {
        toast("Сначала нажми «Настроить приложение» и сохрани LinkedIn Client ID/Secret.", "err");
        return;
      }
      const callbackUri = window.location.origin.replace(/\/$/, "") + "/api/platforms/linkedin/auth/callback";
      const proceed = window.confirm(
        "В LinkedIn Developer Portal добавь этот Authorized redirect URL, включи OpenID Connect и Share on LinkedIn, а в разрешениях выбери openid, profile и w_member_social:\n\n"
        + callbackUri
        + "\n\nПриложение должно иметь доступ к продукту Share on LinkedIn. Продолжить?",
      );
      if (!proceed) return;
      try {
        const result = await post("/api/platforms/linkedin/auth/start", {});
        if (result.redirect_uri !== callbackUri) {
          throw new Error("Адрес callback отличается от текущего адреса панели: " + result.redirect_uri);
        }
        const authorizeUrl = safeExternalUrl(result.authorize_url);
        if (!authorizeUrl) throw new Error("LinkedIn вернул некорректную ссылку авторизации.");
        window.location.assign(authorizeUrl);
      } catch (error) {
        toast("Не удалось начать LinkedIn OAuth: " + (error?.message || error), "err");
      }
      return;
    }

    if (auth.method === "oauth" && platformId === "youtube" && !forceCredentials) {
      const clientId = (auth.fields || []).find((field) => field.key === "YOUTUBE_CLIENT_ID");
      const clientSecret = (auth.fields || []).find((field) => field.key === "YOUTUBE_CLIENT_SECRET");
      if (!clientId?.has_value || !clientSecret?.has_value) {
        toast("Сначала нажми «Настроить приложение» и сохрани Google OAuth Client ID/Secret.", "err");
        return;
      }
      const callbackUri = window.location.origin.replace(/\/$/, "") + "/api/platforms/youtube/auth/callback";
      const proceed = window.confirm(
        "В Google Cloud Console создай OAuth Client типа Web application, включи YouTube Data API v3 и добавь этот Authorized redirect URI:\n\n"
        + callbackUri
        + "\n\nTgFarm запросит youtube.upload и youtube.readonly. Новые загрузки по умолчанию будут Private. Продолжить?",
      );
      if (!proceed) return;
      try {
        const result = await post("/api/platforms/youtube/auth/start", {});
        if (result.redirect_uri !== callbackUri) {
          throw new Error("Адрес callback отличается от текущего адреса панели: " + result.redirect_uri);
        }
        const authorizeUrl = safeExternalUrl(result.authorize_url);
        if (!authorizeUrl) throw new Error("Google вернул некорректную ссылку авторизации.");
        window.location.assign(authorizeUrl);
      } catch (error) {
        toast("Не удалось начать YouTube OAuth: " + (error?.message || error), "err");
      }
      return;
    }

    if (auth.method === "oauth" && platformId === "x" && !forceCredentials) {
      const clientId = (auth.fields || []).find((field) => field.key === "X_OAUTH_CLIENT_ID");
      const clientSecret = (auth.fields || []).find((field) => field.key === "X_OAUTH_CLIENT_SECRET");
      if (!clientId?.has_value || !clientSecret?.has_value) {
        toast("Сначала нажми «Настроить приложение» и сохрани ключи X Developer Portal.", "err");
        return;
      }
      const callbackUri = window.location.origin.replace(/\/$/, "") + "/api/platforms/x/auth/callback";
      const proceed = window.confirm(
        "Перед продолжением добавь этот Callback URI в X Developer Portal → User authentication settings:\n\n"
        + callbackUri
        + "\\n\\nРазрешения приложения должны включать tweet.read, users.read, tweet.write и offline.access. Продолжить?",
      );
      if (!proceed) return;
      try {
        const result = await post("/api/platforms/x/auth/start", {});
        if (result.redirect_uri !== callbackUri) {
          throw new Error("Адрес callback отличается от текущего адреса панели: " + result.redirect_uri);
        }
        const authorizeUrl = safeExternalUrl(result.authorize_url);
        if (!authorizeUrl) throw new Error("X вернул некорректную ссылку авторизации.");
        window.location.assign(authorizeUrl);
      } catch (error) {
        toast("Не удалось начать X OAuth: " + (error?.message || error), "err");
      }
      return;
    }
    if (auth.method !== "credentials" && !forceCredentials) {
      const url = safeExternalUrl(auth.connect_url);
      if (url) window.open(url, "_blank", "noopener,noreferrer");
      toast(
        item.supported
          ? "Сайт открыт. Чтобы подтвердить доступ, вернись в карточку и проверь интеграцию."
          : "Сайт открыт. Эта сеть пока не публикует через TgFarm, вход на сайт не означает подключение API.",
        item.supported ? "" : "err",
      );
      return;
    }

    state.authPlatformId = platformId;
    const dialog = $("#platform-auth-dialog");
    $("#platform-auth-title").textContent = item.name + " · подключение";
    $("#platform-auth-description").textContent = platformId === "youtube"
      ? "Введи Google OAuth Client ID и Client Secret из Google Cloud Console. Создай Web application client, включи YouTube Data API v3 и добавь callback URI панели. После сохранения нажми «Подключить через OAuth»."
      : platformId === "x"
      ? "Введи Client ID и Client Secret из X Developer Portal. Эти ключи хранятся локально, пароль X не нужен. После сохранения нажми «Подключить через OAuth»."
      : platformId === "threads"
        ? "Введи Threads App ID и App Secret из Meta Developer Portal. Добавь callback URI панели и разрешения threads_basic, threads_content_publish. После сохранения нажми «Подключить через OAuth»."
        : platformId === "linkedin"
          ? "Введи LinkedIn Client ID и Client Secret. Добавь callback URL панели в Developer Portal и включи OpenID Connect + Share on LinkedIn; для публикации нужен w_member_social."
          : "Новые значения будут сохранены в локальный .env. Секреты не возвращаются в интерфейс и не пишутся в журнал.";
    const fields = $("#platform-auth-fields");
    fields.replaceChildren();
    (auth.fields || []).forEach((field) => {
      const label = redditNode("label", "platform-auth-field");
      label.appendChild(redditNode("span", "", field.label + (field.secret ? " · секрет" : "")));
      const input = redditNode("input", "");
      input.type = field.secret ? "password" : "text";
      input.name = field.key;
      input.dataset.platformCredential = field.key;
      input.autocomplete = "off";
      input.spellcheck = false;
      input.placeholder = field.has_value
        ? "Уже сохранено. Введи новое значение, чтобы заменить."
        : (field.placeholder || "");
      input.maxLength = 4096;
      input.setAttribute("aria-label", field.label);
      label.appendChild(input);
      if (field.help) label.appendChild(redditNode("small", "", field.help));
      fields.appendChild(label);
    });
    const save = $("#platform-auth-save");
    save.hidden = false;
    save.disabled = false;
    save.textContent = ["x", "threads", "linkedin", "youtube"].includes(platformId) ? "Сохранить приложение" : "Сохранить и проверить";
    $("#platform-auth-check").hidden = ["x", "threads", "linkedin", "youtube"].includes(platformId);
    setPlatformAuthMessage(
      (auth.fields || []).some((field) => field.has_value)
        ? "Часть полей уже сохранена. Оставь их пустыми, если не хочешь менять."
        : "Заполни поля, которые нужны этой площадке. Пустые значения ничего не стирают.",
    );
    if (!dialog.open) dialog.showModal();
  }

  async function checkPlatformAuthorization(platformId, button = null) {
    if (button) {
      button.disabled = true;
      button.classList.add("is-busy");
    }
    try {
      const result = await post(
        "/api/platforms/" + encodeURIComponent(platformId) + "/auth/check", {},
      );
      const item = (state.platformData?.platforms || []).find((entry) => entry.id === platformId);
      if (item && result.auth) item.auth = result.auth;
      const message = result.authorized
        ? "Доступ подтверждён: " + (result.account || platformId) + ". " + (result.detail || "")
        : "Доступ не подтверждён: " + (result.detail || "проверь настройки.");
      if (state.authPlatformId === platformId) {
        setPlatformAuthMessage(message, result.authorized ? "ok" : "err");
      }
      toast(message, result.authorized ? "ok" : "err");
      renderPlatformCards();
      await refresh();
      return result;
    } catch (error) {
      const message = "Не удалось проверить доступ: " + (error?.message || error);
      if (state.authPlatformId === platformId) setPlatformAuthMessage(message, "err");
      toast(message, "err");
      return null;
    } finally {
      if (button) {
        button.disabled = false;
        button.classList.remove("is-busy");
      }
    }
  }

  async function savePlatformCredentials(event) {
    event?.preventDefault();
    if (state.authSaving || !state.authPlatformId) return;
    const platformId = state.authPlatformId;
    const values = {};
    $("[data-platform-credential]", $("#platform-auth-dialog")).forEach((input) => {
      const value = String(input.value || "").trim();
      if (value) values[input.dataset.platformCredential] = value;
    });
    if (!Object.keys(values).length) {
      setPlatformAuthMessage("Введи хотя бы одно новое значение. Пустые поля оставляют сохранённые ключи без изменений.", "err");
      return;
    }
    state.authSaving = true;
    const save = $("#platform-auth-save");
    save.disabled = true;
    save.textContent = "Сохраняю…";
    try {
      const result = await put(
        "/api/platforms/" + encodeURIComponent(platformId) + "/credentials",
        { values },
      );
      const item = (state.platformData?.platforms || []).find((entry) => entry.id === platformId);
      if (item && result.auth) item.auth = result.auth;
      $("[data-platform-credential]", $("#platform-auth-dialog")).forEach((input) => {
        input.value = "";
        const field = (result.auth?.fields || []).find((entry) => entry.key === input.dataset.platformCredential);
        if (field?.has_value) input.placeholder = "Сохранено. Введи новое значение, чтобы заменить.";
      });
      if (["x", "threads", "linkedin", "youtube"].includes(platformId)) {
        closePlatformAuthDialog();
        const platformLabel = {
          x: "OAuth-приложение X",
          threads: "Threads App",
          linkedin: "LinkedIn App",
          youtube: "Google OAuth-приложение",
        }[platformId] || "OAuth-приложение";
        toast(
          platformLabel + " сохранено. Нажми «Подключить через OAuth» для авторизации аккаунта.",
          "ok",
        );
        await refresh();
        return;
      }
      setPlatformAuthMessage("Ключи сохранены локально. Проверяю доступ через API…");
      await checkPlatformAuthorization(platformId);
    } catch (error) {
      setPlatformAuthMessage("Не удалось сохранить: " + (error?.message || error), "err");
      toast("Не удалось сохранить настройки площадки", "err");
    } finally {
      state.authSaving = false;
      save.disabled = false;
      save.textContent = ["x", "threads", "linkedin", "youtube"].includes(platformId) ? "Сохранить приложение" : "Сохранить и проверить";
      renderPlatformCards();
    }
  }

  function renderRedditMetrics(payload) {
    state.redditMetrics = payload || { communities: [] };
    const rows = state.redditMetrics.communities || [];
    const container = $("#reddit-metrics-grid");
    const status = $("#reddit-metrics-status");
    if (!container || !status) return;
    status.textContent = state.redditMetrics.note || "Статистика обновляется вручную.";
    container.replaceChildren();
    if (!rows.length) {
      container.appendChild(redditNode(
        "div", "reddit-empty",
        "Пока нет успешных Reddit-кросспостов с доступными метриками. Сначала нужна хотя бы одна подтверждённая публикация.",
      ));
      return;
    }
    rows.forEach((row) => {
      const card = redditNode("article", "reddit-metric-card");
      const head = redditNode("div", "reddit-metric-card-head");
      const name = redditNode("strong", "", "r/" + row.subreddit);
      head.appendChild(name);
      const posts = redditNode("span", "", n(row.published_posts) + " постов");
      head.appendChild(posts);
      card.appendChild(head);
      const values = redditNode("div", "reddit-metric-values");
      [
        ["Средний score", row.avg_score === null || row.avg_score === undefined ? "нет данных" : n(row.avg_score)],
        ["Комментарии", row.avg_comments === null || row.avg_comments === undefined ? "нет данных" : n(row.avg_comments)],
        ["Upvote ratio", row.avg_upvote_ratio === null || row.avg_upvote_ratio === undefined ? "нет данных" : Math.round(Number(row.avg_upvote_ratio) * 100) + "%"],
        ["Замеров", n(row.metric_samples)],
      ].forEach(([label, value]) => {
        const metric = redditNode("div", "reddit-metric-value");
        metric.appendChild(redditNode("span", "", label));
        metric.appendChild(redditNode("strong", "", value));
        values.appendChild(metric);
      });
      card.appendChild(values);
      const footer = redditNode("div", "reddit-metric-footer");
      footer.appendChild(redditNode("span", "", "Поправка Scout " + ((Number(row.learning_bonus) > 0 ? "+" : "") + Number(row.learning_bonus || 0).toFixed(1))));
      footer.appendChild(redditNode("span", "", row.small_sample ? "мало данных" : "по истории фермы"));
      card.appendChild(footer);
      const url = safeExternalUrl(row.url);
      if (url) {
        const link = redditNode("a", "", "Открыть сообщество ↗");
        link.href = url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        card.appendChild(link);
      }
      container.appendChild(card);
    });
  }

  async function refreshRedditMetrics() {
    const button = $("#reddit-metrics-refresh");
    if (!button || button.disabled) return;
    button.disabled = true;
    button.classList.add("is-busy");
    $("#reddit-metrics-status").textContent = "Получаю статистику доступных Reddit-постов…";
    try {
      const result = await post("/api/reddit/metrics/refresh", { limit: 50 });
      renderRedditMetrics(result.report || {});
      $("#reddit-metrics-status").textContent = result.message || "Метрики обновлены.";
      toast(result.message || "Метрики Reddit обновлены", result.errors ? "err" : "ok");
    } catch (error) {
      $("#reddit-metrics-status").textContent = "Сбор метрик не удался: " + (error?.message || error);
      toast("Не удалось обновить Reddit-метрики", "err");
    } finally {
      button.disabled = false;
      button.classList.remove("is-busy");
    }
  }

  function redditNode(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  }

  function renderRedditCandidates(items) {
    const container = $("#reddit-scout-results");
    if (!container) return;
    container.replaceChildren();
    if (!items.length) {
      container.appendChild(redditNode("div", "reddit-empty",
        "Пока нет кандидатов. Измени формулировку темы или проверь доступ Reddit API."));
      return;
    }
    items.forEach((item) => {
      const article = redditNode("article", "reddit-community-card");
      const head = redditNode("div", "reddit-community-top");
      head.appendChild(redditNode("div", "reddit-community-mark", "r/"));
      const nameBlock = redditNode("div", "reddit-community-name");
      nameBlock.appendChild(redditNode("h4", "", item.display_name || item.name));
      nameBlock.appendChild(redditNode("p", "", item.title || ""));
      head.appendChild(nameBlock);
      const score = redditNode("div", "reddit-score");
      score.appendChild(redditNode("strong", "", item.score));
      score.appendChild(redditNode("span", "", "совпадение"));
      head.appendChild(score);
      article.appendChild(head);

      const track = redditNode("div", "reddit-score-track");
      const fill = redditNode("i", "");
      fill.style.width = Math.max(0, Math.min(100, Number(item.score) || 0)) + "%";
      track.appendChild(fill);
      article.appendChild(track);

      const meta = redditNode("div", "reddit-community-meta");
      meta.appendChild(redditNode("span", "", item.subscribers ? n(item.subscribers) + " подписчиков" : "аудитория не указана"));
      if (item.active_user_count) meta.appendChild(redditNode("span", "", n(item.active_user_count) + " активных"));
      meta.appendChild(redditNode("span", "", (item.submission_type || "any") + "-формат"));
      article.appendChild(meta);
      article.appendChild(redditNode("p", "reddit-community-description", item.description || "Описание не указано."));

      const reasons = redditNode("div", "reddit-reasons");
      (item.reasons || []).slice(0, 4).forEach((reason) => reasons.appendChild(redditNode("span", "", reason)));
      article.appendChild(reasons);

      const risks = (item.risk_flags || []).slice(0, 5);
      if (risks.length) {
        const riskList = redditNode("div", "reddit-risk-list");
        risks.forEach((risk) => {
          const line = redditNode("p", "");
          line.appendChild(redditNode("span", "", "!"));
          line.appendChild(document.createTextNode(risk));
          riskList.appendChild(line);
        });
        article.appendChild(riskList);
      } else {
        article.appendChild(redditNode("p", "reddit-clear-note",
          "Явных технических ограничений в ответе API не обнаружено. Это не подтверждает, что пост разрешён правилами."));
      }

      const details = redditNode("details", "reddit-rules-details");
      const summary = redditNode("summary", "", "Проверить правила (" + ((item.rules || []).length) + ")");
      details.appendChild(summary);
      const list = redditNode("ul", "");
      const rules = (item.rules || []).slice(0, 4);
      if (rules.length) {
        rules.forEach((rule) => {
          const li = redditNode("li", "");
          li.appendChild(redditNode("strong", "", rule.title || "Правило"));
          li.appendChild(redditNode("span", "", rule.description || rule.violation_reason || ""));
          list.appendChild(li);
        });
      } else {
        list.appendChild(redditNode("li", "", "API не вернул список правил. Открой страницу сообщества и проверь их вручную."));
      }
      details.appendChild(list);
      article.appendChild(details);

      const actions = redditNode("div", "reddit-community-actions");
      const url = safeExternalUrl(item.url);
      if (url) {
        const link = redditNode("a", "", "Открыть сообщество ↗");
        link.href = url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        actions.appendChild(link);
      }
      const button = redditNode("button", "btn btn-primary btn-sm", "Подготовить свою версию");
      button.type = "button";
      button.dataset.redditTailor = item.name || item.display_name;
      actions.appendChild(button);
      article.appendChild(actions);
      container.appendChild(article);
    });
  }

  async function submitRedditScout() {
    if (state.redditBusy) return;
    const query = ($("#reddit-scout-query")?.value || "").trim();
    const postId = ($("#reddit-post-id")?.value || "").trim();
    const button = $("#reddit-scout-submit");
    const status = $("#reddit-scout-status");
    if (query.length < 2) {
      toast("Введи тему поиска, например Python или local AI.", "err");
      return;
    }
    state.redditBusy = true;
    state.redditPreview = null;
    $("#reddit-tailor-panel").hidden = true;
    $("#reddit-publish-result").textContent = "";
    button.disabled = true;
    button.classList.add("is-busy");
    status.textContent = "Ищу сообщества, читаю описания и правила через Reddit API…";
    try {
      const result = await post("/api/reddit/scout", {
        query,
        post_public_id: postId || null,
        limit: 6,
      });
      state.redditCandidates = result.candidates || [];
      renderRedditCandidates(state.redditCandidates);
      status.textContent = "Найдено кандидатов: " + n(result.returned) + ". Оценка учитывает совпадение темы, исходного поста и активность; перед отправкой всё равно проверь правила вручную.";
      if (!state.redditCandidates.length) toast("Reddit не вернул подходящих сообществ.", "err");
    } catch (error) {
      state.redditCandidates = [];
      renderRedditCandidates([]);
      status.textContent = "Поиск не выполнен: " + error.message;
      toast("Не удалось найти сообщества: " + error.message, "err");
    } finally {
      state.redditBusy = false;
      button.disabled = false;
      button.classList.remove("is-busy");
    }
  }

  async function tailorRedditCandidate(subreddit) {
    const postId = ($("#reddit-post-id")?.value || "").trim();
    if (!postId) {
      toast("Укажи ID уже опубликованного поста TgFarm, чтобы подготовить адаптацию.", "err");
      $("#reddit-post-id")?.focus();
      return;
    }
    if (state.redditBusy) return;
    state.redditBusy = true;
    state.redditPreview = null;
    const panel = $("#reddit-tailor-panel");
    panel.hidden = false;
    $("#reddit-tailor-title").textContent = "Готовлю редакторскую версию…";
    $("#reddit-tailor-subtitle").textContent = "r/" + subreddit;
    $("#reddit-tailor-notes").replaceChildren(redditNode("p", "", "Читаю актуальные правила и требования сообщества, затем подготавливаю текст без новых фактов."));
    $("#reddit-tailor-rules").replaceChildren();
    $("#reddit-publish-button").disabled = true;
    $("#reddit-publish-result").textContent = "";
    try {
      const result = await post("/api/reddit/tailor", { post_public_id: postId, subreddit });
      state.redditPreview = result;
      $("#reddit-tailor-title").textContent = "Версия для r/" + result.subreddit;
      const subtitle = $("#reddit-tailor-subtitle");
      subtitle.replaceChildren(document.createTextNode((result.community?.title || result.subreddit) + " · " + n(result.community?.subscribers) + " подписчиков "));
      const communityUrl = safeExternalUrl(result.community?.url);
      if (communityUrl) {
        const link = redditNode("a", "", "страница сообщества ↗");
        link.href = communityUrl;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        subtitle.appendChild(link);
      }
      $("#reddit-draft-title").value = result.draft?.title || "";
      $("#reddit-draft-body").value = result.draft?.body || "";

      const notesBox = $("#reddit-tailor-notes");
      notesBox.replaceChildren();
      const modeLabel = result.adaptation?.mode === "ai"
        ? "AI-адаптация (" + result.adaptation.provider + ")"
        : "Безопасный extractive-черновик, нужна ручная редактура";
      ["Режим: " + modeLabel, ...(result.notes || [])].forEach((note) => {
        const line = redditNode("p", "");
        line.appendChild(redditNode("span", "", "i"));
        line.appendChild(document.createTextNode(note));
        notesBox.appendChild(line);
      });

      const rulesBox = $("#reddit-tailor-rules");
      rulesBox.replaceChildren(redditNode("h4", "", "Правила из Reddit API"));
      const rules = result.rules || [];
      if (rules.length) {
        const list = redditNode("ul", "");
        rules.forEach((rule) => {
          const li = redditNode("li", "");
          li.appendChild(redditNode("strong", "", rule.title || "Правило"));
          li.appendChild(redditNode("span", "", rule.description || rule.violation_reason || "Описание не предоставлено."));
          list.appendChild(li);
        });
        rulesBox.appendChild(list);
      } else {
        rulesBox.appendChild(redditNode("p", "", "Список правил не вернулся из API. Открой сообщество и проверь правила вручную перед публикацией."));
      }
      $("#reddit-publish-button").disabled = false;
      panel.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      $("#reddit-tailor-title").textContent = "Не удалось подготовить версию";
      $("#reddit-tailor-notes").replaceChildren(redditNode("p", "", error.message));
      toast("Адаптация не выполнена: " + error.message, "err");
    } finally {
      state.redditBusy = false;
    }
  }

  async function publishRedditDraft() {
    const preview = state.redditPreview;
    if (!preview || state.redditBusy) return;
    const title = ($("#reddit-draft-title")?.value || "").trim();
    const body = ($("#reddit-draft-body")?.value || "").trim();
    if (!title || !body) {
      toast("Заголовок и текст Reddit-поста не должны быть пустыми.", "err");
      return;
    }
    const proceed = confirm(
      "Последняя проверка перед публикацией в r/" + preview.subreddit + ".\\n\\n"
      + "Я прочитал правила сообщества, проверил заголовок/текст/источники и подтверждаю отправку одного поста. Продолжить?"
    );
    if (!proceed) return;
    const button = $("#reddit-publish-button");
    const resultBox = $("#reddit-publish-result");
    state.redditBusy = true;
    button.disabled = true;
    button.classList.add("is-busy");
    resultBox.textContent = "Проверяю ограничения и отправляю один подтверждённый пост…";
    try {
      const result = await post("/api/reddit/publish", {
        post_public_id: preview.post_public_id,
        subreddit: preview.subreddit,
        title,
        body,
        confirm: true,
      });
      const submission = result.result || {};
      resultBox.replaceChildren();
      const statusLine = redditNode("p", submission.status === "posted" ? "reddit-result-ok" : "reddit-result-warn",
        (submission.status || "unknown") + ": " + (submission.error || "Ответ Reddit обработан."));
      resultBox.appendChild(statusLine);
      const url = safeExternalUrl(submission.url);
      if (url) {
        const link = redditNode("a", "", "Открыть опубликованный пост ↗");
        link.href = url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        resultBox.appendChild(link);
      }
      if (submission.status === "posted") {
        toast("Пост опубликован в r/" + preview.subreddit + ".", "ok");
      } else if (submission.status === "unknown") {
        toast("Reddit вернул неопределённый результат. Проверь сообщество вручную, не повторяй отправку.", "err");
      } else {
        toast("Reddit не подтвердил публикацию: " + (submission.error || submission.status), "err");
      }
    } catch (error) {
      resultBox.textContent = "Публикация не выполнена: " + error.message;
      toast("Reddit не принял запрос: " + error.message, "err");
    } finally {
      state.redditBusy = false;
      button.disabled = false;
      button.classList.remove("is-busy");
    }
  }

  async function updatePlatformSwitch(platformId, enabled) {
    const item = (state.platformData?.platforms || []).find((entry) => entry.id === platformId);
    if (!item || state.platformBusy.has(platformId)) return;
    if (enabled && !item.configured) {
      const proceed = confirm(
        `${item.name} будет включена в настройках, но пока не настроена.\n\n`
        + `${item.connection_detail}\n\nПродолжить? Публикация не начнётся без нужных учётных данных.`
      );
      if (!proceed) return;
    } else if (!enabled) {
      const proceed = confirm(
        `Отключить ${item.name}?\n\nНовые отправки на эту площадку остановятся. Уже опубликованные посты не изменятся, а очередь сохранится.`
      );
      if (!proceed) return;
    }
    state.platformBusy.add(platformId);
    renderPlatformCards();
    try {
      const result = await put(`/api/platforms/${encodeURIComponent(platformId)}`, { enabled });
      toast(`${item.name}: ${enabled ? "включена" : "поставлена на паузу"}`, "ok");
      const payload = await api("/api/platforms");
      renderPlatforms(payload);
    } catch (error) {
      toast(`Не удалось изменить ${item.name}: ${error.message}`, "err");
    } finally {
      state.platformBusy.delete(platformId);
      renderPlatformCards();
    }
  }

  async function updatePlatformPlan(platformId) {
    const item = (state.platformData?.platforms || []).find((entry) => entry.id === platformId);
    if (!item || state.platformBusy.has(platformId)) return;
    state.platformBusy.add(platformId);
    renderPlatformCards();
    try {
      await put(`/api/platforms/${encodeURIComponent(platformId)}/plan`, { planned: !item.planned });
      toast(item.planned ? `${item.name} убрана из плана` : `${item.name} добавлена в план интеграций`, "ok");
      renderPlatforms(await api("/api/platforms"));
    } catch (error) {
      toast(`Не удалось обновить план: ${error.message}`, "err");
    } finally {
      state.platformBusy.delete(platformId);
      renderPlatformCards();
    }
  }

  function renderSources(sources) {
    $("#sources-list").innerHTML = sources.map((s) => {
      const cooling = Boolean(s.cooldown_active);
      const st = cooling ? "tag-warn" : s.failures >= 3 ? "tag-err" : s.last_status === "ok" ? "tag-ok" : "";
      const status = cooling ? "в карантине" : s.last_status === "cooldown" ? "ожидает повторной проверки" : s.last_status;
      return `
        <div class="item">
          <div class="item-main">
            <div class="item-title">${esc(s.name || s.id)}</div>
            <div class="item-meta">${esc(s.kind)} · вес ${s.weight} · ${dt(s.last_fetched_at)}${cooling ? " · пауза до " + dt(s.cooldown_until) : ""}${s.last_error ? " · " + esc((s.last_error || "").slice(0, 70)) : ""}</div>
          </div>
          <div class="item-side">
            <span class="tag ${st}">${esc(status)}</span>
            ${s.failures ? `<span class="tag tag-err">${s.failures} ошибок</span>` : ""}
          </div>
        </div>`;
    }).join("") || `<div class="empty">Источники не заданы</div>`;
  }

  function renderCrosspostHealth(payload) {
    const container = $("#crosspost-health");
    if (!container) return;
    const data = payload || {};
    const totals = data.totals || {};
    const statusLabel = {
      healthy: "есть успешные доставки",
      working: "есть активные повторы",
      attention: "требует внимания",
      setup: "не настроено",
      dry_run: "тестовый режим",
      not_tested: "ещё не проверено",
    };
    const cards = [
      { label: "Адресатов в журнале", value: totals.targets || 0, hint: "материал × площадка" },
      { label: "Опубликовано", value: totals.posted || 0, hint: "последнее состояние" },
      { label: "Требует внимания", value: totals.needs_attention || 0, hint: "неопределённые/зависшие/исчерпанные" },
      { label: "Активные claims", value: totals.claims || 0, hint: `устарело: ${n(totals.stale_claims || 0)}` },
    ];
    container.innerHTML = `
      <div class="delivery-health-head">
        <span class="tag ${data.state === "attention" ? "tag-warn" : data.state === "healthy" ? "tag-ok" : "tag-acc"}">${esc(statusLabel[data.state] || "состояние неизвестно")}</span>
        <span class="card-sub">срез ${esc(dt(data.generated_at))}</span>
      </div>
      <div class="delivery-health-kpis">${cards.map(card => `
        <div class="delivery-health-kpi">
          <span class="delivery-health-label">${esc(card.label)}</span>
          <strong>${n(card.value)}</strong>
          <span class="delivery-health-hint">${esc(card.hint)}</span>
        </div>`).join("")}
      </div>
      <div class="delivery-health-platforms">${(data.platforms || []).map(item => {
        const label = statusLabel[item.state] || item.state;
        const tone = item.state === "attention" ? "tag-warn"
          : item.state === "healthy" ? "tag-ok"
          : item.state === "working" ? "tag-acc" : "";
        return `<div class="delivery-health-platform">
          <strong>${esc(PLATFORM_RU[item.platform] || item.platform)}</strong>
          <span class="tag ${tone}">${esc(label)}</span>
          <span class="delivery-health-hint">${n(item.posted)} опубликовано · ${n(item.unknown)} сверка · ${n(item.permanent)} постоянно</span>
        </div>`;
      }).join("") || `<div class="delivery-health-empty">Пока нет записей о доставке</div>`}</div>`;
  }

  function renderCrossposts(entries) {
    const container = $("#crossposts-list");
    if (!container) return;

    // API rows are newest-first. Keep the newest attempt for each
    // material/platform pair, then show all platforms together as one delivery card.
    const byPost = new Map();
    const seen = new Set();
    for (const row of entries || []) {
      const post = String(row.post || "");
      const platform = String(row.platform || "unknown");
      const key = `${post}\u0000${platform}`;
      if (!post || seen.has(key)) continue;
      seen.add(key);
      if (!byPost.has(post)) byPost.set(post, []);
      byPost.get(post).push(row);
    }

    const labels = {
      posted: "опубликован",
      skipped: "пропущен",
      failed: "временная ошибка",
      permanent: "постоянная ошибка",
      permanent_failure: "постоянная ошибка",
      unknown: "нужна сверка",
      publishing: "отправляется",
      retry_authorized: "повтор разрешён",
      dry_run: "тестовый режим",
    };
    const statusClass = {
      posted: "tag-ok",
      failed: "tag-warn",
      permanent: "tag-err",
      permanent_failure: "tag-err",
      unknown: "tag-err",
      publishing: "tag-warn",
      retry_authorized: "tag-acc",
      dry_run: "tag-acc",
      skipped: "",
    };
    const platformNames = {
      telegram: "Telegram", dzen: "Дзен", vk: "VK", max: "MAX", x: "X",
      reddit: "Reddit", bluesky: "Bluesky", mastodon: "Mastodon",
      threads: "Threads", linkedin: "LinkedIn", youtube: "YouTube",
      tiktok: "TikTok",
    };

    const cards = [...byPost.entries()].sort((a, b) => {
      const aTime = Math.max(...a[1].map((row) => row.at ? new Date(row.at).getTime() : 0));
      const bTime = Math.max(...b[1].map((row) => row.at ? new Date(row.at).getTime() : 0));
      return bTime - aTime;
    });
    const matchesFilter = (rows) => {
      const statuses = rows.map((row) => String(row.status || "unknown"));
      if (state.crosspostFilter === "attention") {
        return statuses.some((status) => ["unknown", "failed", "permanent", "permanent_failure"].includes(status));
      }
      if (state.crosspostFilter === "published") return statuses.includes("posted");
      if (state.crosspostFilter === "errors") {
        return statuses.some((status) => ["failed", "permanent", "permanent_failure"].includes(status));
      }
      if (state.crosspostFilter === "pending") {
        return statuses.some((status) => ["publishing", "retry_authorized", "failed"].includes(status));
      }
      return true;
    };
    const visibleCards = cards.filter(([, rows]) => matchesFilter(rows));

    container.innerHTML = visibleCards.slice(0, 12).map(([post, rows]) => {
      const latestAt = rows.reduce((best, row) => {
        const time = row.at ? new Date(row.at).getTime() : 0;
        return time > best ? time : best;
      }, 0);
      const deliveryStates = rows.map((row) => {
        const platform = String(row.platform || "unknown");
        const status = String(row.status || "unknown");
        const ageMs = row.at ? Date.now() - new Date(row.at).getTime() : 0;
        const canReconcile = ["unknown", "permanent", "permanent_failure"].includes(status)
          || (status === "failed" && Number(row.attempt || 1) >= 5)
          || (status === "publishing" && Number.isFinite(ageMs) && ageMs >= 30 * 60 * 1000);
        const safeUrl = typeof row.url === "string" && row.url.startsWith("https://")
          ? row.url : "";
        const url = safeUrl
          ? ` <a href="${esc(safeUrl)}" target="_blank" rel="noopener noreferrer">открыть</a>`
          : "";
        const remote = row.remote_id
          ? ` <span class="muted">ID: ${esc(String(row.remote_id).slice(0, 54))}</span>`
          : "";
        const error = row.error && status !== "posted"
          ? `<div class="item-meta">${esc(String(row.error).slice(0, 140))}</div>`
          : "";
        const retryAfter = row.retry_after_at
          ? `<div class="item-meta">Автоповтор не раньше ${esc(dt(row.retry_after_at))}</div>`
          : "";
        const actions = canReconcile ? `
          <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:6px">
            <button class="btn btn-primary btn-sm js-crosspost-reconcile"
              data-post="${esc(post)}" data-platform="${esc(platform)}" data-action="confirm_posted">Пост уже есть</button>
            <button class="btn btn-ghost btn-sm js-crosspost-reconcile"
              data-post="${esc(post)}" data-platform="${esc(platform)}" data-action="retry_after_check">Проверил: поста нет</button>
          </div>` : "";
        return `
          <div class="delivery-platform">
            <div class="item-meta"><strong>${esc(platformNames[platform] || PLATFORM_RU[platform] || platform)}</strong>
              <span class="tag ${statusClass[status] || ""}">${esc(labels[status] || status)}</span>${url}${remote}</div>
            ${error}${retryAfter}${actions}
          </div>`;
      }).join("");
      const summary = rows.reduce((acc, row) => {
        acc[row.status] = (acc[row.status] || 0) + 1;
        return acc;
      }, {});
      const successCount = (summary.posted || 0) + (summary.dry_run || 0);
      const needsAttention = (summary.unknown || 0) + (summary.failed || 0)
        + (summary.permanent || 0) + (summary.permanent_failure || 0);
      const activeCount = (summary.publishing || 0) + (summary.retry_authorized || 0);
      const allSkipped = rows.length > 0 && rows.every((row) => row.status === "skipped");
      const overall = needsAttention ? "нужно внимание"
        : activeCount ? "доставка идёт"
        : successCount ? "есть результат"
        : allSkipped ? "не настроено"
        : "ожидает доставки";
      const overallClass = needsAttention ? "tag-warn" : (activeCount ? "tag-acc" : (successCount ? "tag-ok" : ""));
      return `
        <div class="item delivery-card" style="align-items:flex-start">
          <div class="item-main" style="min-width:0">
            <div class="item-title">${esc(post)}</div>
            <div class="item-meta">${esc(dt(latestAt ? new Date(latestAt).toISOString() : null))} · площадок с записью: ${n(rows.length)}</div>
            <div class="delivery-platforms">${deliveryStates}</div>
          </div>
          <div class="item-side"><span class="tag ${overallClass}">${esc(overall)}</span></div>
        </div>`;
    }).join("") || `<div class="empty">${visibleCards.length ? "Нет материалов для выбранного фильтра" : (state.crosspostFilter === "all" ? "Кросспостов пока нет" : "Нет материалов для выбранного фильтра")}</div>`;

    $(".js-crosspost-reconcile", container) && $$(".js-crosspost-reconcile", container).forEach((button) => {
      button.addEventListener("click", () => window.tgfarmReconcileCrosspost(
        button.dataset.post,
        button.dataset.platform,
        button.dataset.action,
      ));
    });
  }

  function renderRuns(runs) {
    const byRun = new Map();
    for (const r of runs) {
      if (!byRun.has(r.run_id)) byRun.set(r.run_id, []);
      byRun.get(r.run_id).push(r);
    }
    $("#runs-list").innerHTML = [...byRun.entries()].map(([runId, stages]) => `
      <div class="item" style="cursor:default">
        <div class="item-main">
          <div class="item-title mono">${esc(runId)} · ${dt(stages[0]?.at)}</div>
          <div class="item-meta">${stages.map((s) => `${esc(s.stage.replace(/^s\d_/, ""))}:${s.out}`).join("  ")}</div>
        </div>
        <div class="item-side">
          <span class="tag ${stages.some((s) => s.status === "error") ? "tag-warn" : "tag-ok"}">
            ${stages.some((s) => s.status === "error") ? "с ошибками" : "ок"}
          </span>
        </div>
      </div>`).join("") || `<div class="empty">Журнал пуст</div>`;
  }

  /* --------------------------------------------------------------- drawer */
  async function openDrawer(postId) {
    if (!postId) return;
    const drawer = $("#drawer");
    const inner = $("#drawer-inner");
    inner.innerHTML = `<div class="empty">Загрузка…</div>`;
    drawer.hidden = false;

    let p;
    try {
      p = await api(`/api/posts/${encodeURIComponent(postId)}`);
    } catch (e) {
      inner.innerHTML = `<div class="empty">Не удалось загрузить пост</div>`;
      return;
    }

    const q = p.quality || {};
    const scores = Object.entries(q.scores || {});
    inner.innerHTML = `
      <div class="drawer-head">
        <h3>${esc(p.hook)}</h3>
        <button class="btn btn-ghost btn-sm drawer-close" onclick="document.getElementById('drawer').hidden=true">✕</button>
      </div>

      <div class="pcard-foot" style="margin-bottom:14px">
        <span class="tag ${STATUS_TAG[p.status] || ""}">${esc(STATUS_RU[p.status] || p.status)}</span>
        <span class="tag">${esc(FORMAT_RU[p.post_format] || p.post_format)}</span>
        <span class="tag">${esc(PLATFORM_RU[p.platform] || p.platform || "Telegram")}</span>
        <span class="tag">${esc(p.channel_title || p.channel_id)}</span>
        <span class="tag">${n(p.chars)} симв.</span>
        ${p.reject_reason ? `<span class="tag tag-err">${esc(p.reject_reason.split(":")[0])}</span>` : ""}
      </div>

      ${p.platform === "dzen" && (p.impressions || p.views) ? `<div class="drawer-block"><h4>Метрики Дзена</h4><div class="kv">
        ${kv("Показы", n(p.impressions))}
        ${kv("Открытия", n(p.opens))}
        ${kv("Дочитывания", n(p.views))}
        ${kv("CTR", pct(p.ctr))}
        ${kv("Дочитываемость", pct(p.opens ? Math.min(1, (p.views || 0) / p.opens) : 0))}
      </div></div>` : ""}

      ${p.cover ? `<div class="drawer-cover"><img src="/api/media/${encodeURIComponent(p.cover.id)}" alt=""></div>` : ""}
      ${p.video ? `<div class="drawer-block"><h4>Видео</h4>
        <video src="/api/media/${encodeURIComponent(p.video.id)}" controls style="width:100%;border-radius:12px"></video>
      </div>` : ""}

      <div class="drawer-block"><h4>Текст</h4><div class="drawer-text">${esc(p.text)}</div></div>

      ${p.format_reason ? `<div class="drawer-block"><h4>Почему такой формат</h4>
        <div class="drawer-text">${esc(p.format_reason)}</div></div>` : ""}

      ${p.external_url ? `<div class="drawer-block"><h4>Публикация</h4>
        <a href="${esc(p.external_url)}" target="_blank" rel="noopener">${esc(p.external_url)}</a></div>` : ""}

      ${scores.length ? `<div class="drawer-block"><h4>Гейты качества · ${esc(q.reason || "")}</h4>
        <div class="kv">${scores.map(([k, v]) => kv(k, (v >= 0.99 ? "✓ " : "") + (k === "tone" ? (v * 10).toFixed(1) + "/10" : pct(v)))).join("")}</div>
      </div>` : ""}
      ${q.issues && q.issues.length ? `<div class="drawer-block"><h4>Замечания критика</h4>
        <ul class="drawer-issues">${q.issues.map((i) => `<li>${esc(i)}</li>`).join("")}</ul></div>` : ""}

      ${(p.claims || []).length ? `<div class="drawer-block"><h4>Проверенные утверждения</h4>
        ${p.claims.map((c) => `<div class="claim ${c.verified ? "ok" : "no"}">
          <span class="claim-dot"></span><span>${esc(c.claim)}${c.verified ? "" : " <em>(нет в источнике)</em>"}</span>
        </div>`).join("")}</div>` : ""}

      ${(p.sources || []).length ? `<div class="drawer-block"><h4>Источники</h4>
        ${p.sources.map((s) => `<div class="drawer-src"><div>${esc(s.title || "")}</div>
          <a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.url)}</a></div>`).join("")}</div>` : ""}

      ${(p.media || []).length ? `<div class="drawer-block"><h4>Медиа</h4>
        ${p.media.map((m) => `<div class="drawer-src"><span class="tag">${esc(m.kind)}</span> ${esc(m.path)}</div>`).join("")}</div>` : ""}

            ${p.status === "publish_unknown" ? `<div class="drawer-block" style="width:100%">
        <div class="drawer-text"><strong>Результат не подтверждён.</strong> Откройте канал или ленту площадки. Не нажимайте повтор, пока не проверите, появился ли пост.</div>
      </div>
      <div class="drawer-block" style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn btn-primary btn-sm" onclick="tgfarmReconcile('${esc(p.id)}','confirm_published')">Да, пост уже есть</button>
        <button class="btn btn-ghost btn-sm" onclick="tgfarmReconcile('${esc(p.id)}','retry_after_check')">Проверил: поста нет</button>
      </div>` : `<div class="drawer-block" style="display:flex;gap:8px;flex-wrap:wrap">
        ${["scheduled", "gated"].includes(p.status) ? `<button class="btn btn-primary btn-sm" onclick="tgfarmPublish('${esc(p.id)}')">Опубликовать сейчас</button>` : ""}
        ${p.status === "failed" && !p.tg_message_id ? `<button class="btn btn-primary btn-sm" onclick="tgfarmRetryFailed('${esc(p.id)}')">Повторить после исправления</button>` : ""}
        ${["draft", "gated", "scheduled", "rejected", "failed"].includes(p.status) ? `<button class="btn btn-ghost btn-sm" onclick="tgfarmReject('${esc(p.id)}')">Отклонить</button>` : ""}
        <button class="btn btn-ghost btn-sm" onclick="tgfarmCopyText('${esc(p.id)}')">Копировать текст</button>
        <button class="btn btn-ghost btn-sm" onclick="tgfarmTrace('${esc(p.id)}')">Трейс</button>
        ${p.external_url ? `<a class="btn btn-ghost btn-sm" href="${esc(p.external_url)}" target="_blank" rel="noopener">Открыть оригинал</a>` : ""}
        ${EDITABLE.has(p.status) && !p.tg_message_id
          ? `<button class="btn btn-ghost btn-sm" id="btn-edit" onclick="tgfarmEditStart('${esc(p.id)}')">Редактировать</button>
             <button class="btn btn-ghost btn-sm" onclick="tgfarmPreview('${esc(p.id)}')">Предпросмотр</button>` : ""}
      </div>`}

      ${state.bestHour != null ? `<div class="drawer-block"><h4>Умная подсказка</h4><div class="drawer-text">Лучшее время публикации по статистике: ${state.bestHour}:00. Пост пройдёт гейты и встанет в очередь автоматически.</div></div>` : ""}

      <div id="editor-slot"></div>
      <div id="preview-slot"></div>`;
  }

  const EDITABLE = new Set(["draft", "gated", "scheduled", "rejected"]);

  window.tgfarmEditStart = async (id) => {
    const slot = $("#editor-slot");
    if (!slot) return;
    slot.innerHTML = `<div class="drawer-block"><div class="empty">Загрузка…</div></div>`;
    let p;
    try {
      p = await api(`/api/posts/${encodeURIComponent(id)}`);
    } catch (e) {
      slot.innerHTML = "";
      return;
    }
    slot.innerHTML = `
      <div class="drawer-block">
        <h4>Редактор</h4>
        <label class="field"><span>Заголовок</span>
          <textarea id="ed-hook" rows="2">${esc(p.hook)}</textarea></label>
        <label class="field"><span>Текст</span>
          <textarea id="ed-body" rows="8">${esc(p.body || "")}</textarea></label>
        <label class="field"><span>Призыв к действию</span>
          <input id="ed-cta" value="${esc(p.cta || "")}" placeholder="необязательно"></label>
        <div id="ed-count" class="drawer-meta"></div>
        <div class="drawer-block" style="display:flex;gap:8px;padding:0;border:0;margin-top:10px">
          <button class="btn btn-primary btn-sm" onclick="tgfarmEditSave('${esc(p.id)}')">Сохранить и проверить</button>
          <button class="btn btn-ghost btn-sm" onclick="tgfarmPreview('${esc(p.id)}')">Предпросмотр</button>
          <button class="btn btn-ghost btn-sm" onclick="tgfarmPolish('${esc(p.id)}','shorter')">Короче</button>
          <button class="btn btn-ghost btn-sm" onclick="tgfarmPolish('${esc(p.id)}','funnier')">Смешнее</button>
          <button class="btn btn-ghost btn-sm" onclick="tgfarmPolish('${esc(p.id)}','stricter')">Строже</button>
          <button class="btn btn-ghost btn-sm" onclick="document.getElementById('editor-slot').innerHTML=''">Свернуть</button>
        </div>
        <div id="ed-result"></div>
      </div>`;
    const count = () => {
      const hook = ($("#ed-hook") || {}).value || "";
      const body = ($("#ed-body") || {}).value || "";
      const el = $("#ed-count");
      if (el) el.textContent = `${hook.length + body.length} симв. редакционного текста`;
    };
    ["ed-hook", "ed-body"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.addEventListener("input", count);
    });
    count();
  };

  window.tgfarmEditSave = async (id) => {
    const body = {
      hook: ($("#ed-hook") || {}).value || "",
      body: ($("#ed-body") || {}).value || "",
      cta: ($("#ed-cta") || {}).value || "",
    };
    try {
      const r = await fetch(`/api/posts/${encodeURIComponent(id)}/draft`, {
        method: "PUT",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || r.status);
      const ok = data.passed;
      $("#ed-result").innerHTML = `<div class="field-result ${ok ? "ok" : "bad"}">
        ${ok ? "Проверки пройдены" : "Проверки не пройдены"}${data.gate ? ` · ${esc(data.gate)}` : ""}
        — статус: ${esc(STATUS_RU[data.status] || data.status)}</div>`;
      toast(ok ? "Сохранено" : "Правка не прошла гейты", ok ? "ok" : "err");
      refresh();
      if (ok) openDrawer(id);
    } catch (e) {
      $("#ed-result").innerHTML = `<div class="field-result bad">${esc(e.message)}</div>`;
    }
  };

  window.tgfarmTrace = async (id) => {
    const slot = $("#preview-slot");
    if (!slot) return;
    slot.innerHTML = `<div class="drawer-block"><h4>Трейс</h4><div class="empty">Загрузка…</div></div>`;
    try {
      const t = await api(`/api/posts/${encodeURIComponent(id)}/trace`);
      const rows = t.events.map((e) =>
        `<div class="trace-row"><span class="trace-at">${e.at || "—"}</span> <b>${esc(e.kind)}</b> <span>${esc(e.detail || "")}</span></div>`).join("");
      slot.innerHTML = `<div class="drawer-block"><h4>Трейс · ${esc(t.status)} · медиа: ${t.media}</h4>${rows}</div>`;
    } catch (e) {
      slot.innerHTML = `<div class="drawer-block"><h4>Трейс</h4><div class="empty">Не удалось: ${esc(e.message)}</div></div>`;
    }
  };

  window.tgfarmPolish = async (id, mode) => {
    if (!confirm("Переписать пост? Гейты запустятся заново.")) return;
    try {
      const res = await fetch(`/api/posts/${encodeURIComponent(id)}/polish`, {
        method: "POST", headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ mode }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || String(res.status));
      if (!data.ok) { toast(data.message || "Не удалось", "err"); return; }
      toast(data.passed ? "Переписано, гейты пройдены" : "Переписано, но гейты не пропустили", data.passed ? "ok" : "err");
      refresh();
      openDrawer(id);
    } catch (e) {
      toast("Ошибка полировки: " + e.message, "err");
    }
  };

  window.tgfarmPreview = async (id) => {
    const slot = $("#preview-slot");
    if (!slot) return;
    slot.innerHTML = `<div class="drawer-block"><h4>Предпросмотр</h4><div class="empty">Готовим…</div></div>`;
    try {
      const res = await fetch(`/api/posts/${encodeURIComponent(id)}/preview`, {
        method: "POST", headers: authHeaders(),
      });
      if (!res.ok) throw new Error(String(res.status));
      const p = await res.json();
      slot.innerHTML = `
        <div class="drawer-block">
          <h4>Предпросмотр · ${esc(PLATFORM_RU[p.platform] || p.platform || "Telegram")} · ${n(p.chars)} / ${p.limit} симв.</h4>
          ${p.cover ? `<img class="preview-cover" src="${esc(p.cover)}" alt="">` : ""}
          <div class="tg-post">
            ${p.channel_title ? `<div class="tg-channel">${esc(p.channel_title)}</div>` : ""}
            <div class="tg-text">${p.preview}</div>
            ${p.disclaimer ? `<div class="tg-note">${esc(p.disclaimer)}</div>` : ""}
          </div>
          ${p.over_limit ? `<div class="field-result bad">Текст длиннее лимита ${esc(PLATFORM_RU[p.platform] || "Telegram")}</div>` : ""}
        </div>`;
    } catch (e) {
      slot.innerHTML = `<div class="drawer-block"><h4>Предпросмотр</h4><div class="empty">Не удалось: ${esc(e.message)}</div></div>`;
    }
  };

  /* ---------------------------------------------------------------- data */
  function renderDzen(dz) {
    const farmPill = $("#farm-pill");
    if (!farmPill) return;
    let pill = $("#dzen-pill");
    if (!pill) {
      pill = document.createElement("span");
      pill.id = "dzen-pill";
      farmPill.parentElement.insertBefore(pill, farmPill.nextSibling);
    }
    if (dz.authorized === true) {
      pill.className = "pill pill-ok";
      pill.textContent = "Дзен: авторизован";
      pill.onclick = null;
      pill.style.cursor = "default";
    } else if (dz.authorized === false) {
      pill.className = "pill pill-err";
      pill.textContent = "Дзен: войти";
      pill.style.cursor = "pointer";
      pill.onclick = window.tgfarmDzenLogin;
    } else {
      pill.className = "pill";
      pill.textContent = "Дзен: …";
      pill.style.cursor = "pointer";
      pill.onclick = window.tgfarmDzenLogin;
    }
  }

  window.tgfarmDzenLogin = async () => {
    if (!confirm("Откроется окно Дзена. Войди в Яндекс там — ферма подождёт до 7 минут.")) return;
    toast("Открываю окно входа в Дзен…");
    try {
      const res = await fetch("/api/dzen/login?timeout=420", { method: "POST", headers: authHeaders() });
      const data = await res.json();
      toast(data.ok ? "Дзен: сессия сохранена" : "Вход не завершён: " + (data.error || ""), data.ok ? "ok" : "err");
      refresh();
    } catch (e) {
      toast("Вход не удался: " + e.message, "err");
    }
  };

  function renderAlerts(alerts) {
    const kpis = $("#kpis");
    if (!kpis) return;
    let box = $("#alert-banner");
    if (!alerts.length) {
      if (box) box.remove();
      return;
    }
    if (!box) {
      box = document.createElement("div");
      box.id = "alert-banner";
      kpis.parentElement.insertBefore(box, kpis);
    }
    box.className = "alert-banner";
    box.innerHTML = alerts.map((a) =>
      `<div class="alert alert-${a.level === "error" ? "err" : "warn"}">
         <b>${esc(a.code)}</b> — ${esc(a.message)}</div>`).join("");
  }

  async function refresh() {
    if (state.inFlight) return;
    state.inFlight = true;
    try {
      const [status, report, runs, trends, posts, channels, sources, dzen, crossposts, deliveryHealth, memory, platforms, redditMetrics, aiUsage] = await Promise.all([
        api("/api/status"), api("/api/report?days=14"), api("/api/runs?limit=40"),
        api("/api/trends?limit=25"),
        api(`/api/posts?limit=40${state.postFilter ? "&status=" + state.postFilter : ""}`),
        api("/api/channels"), api("/api/sources"), api("/api/dzen/status"),
        api("/api/crossposts?limit=100"), api("/api/crossposts/health"),
        api(`/api/memory?limit=20${state.memoryQuery ? "&query=" + encodeURIComponent(state.memoryQuery) : ""}`),
        api("/api/platforms"), api("/api/reddit/metrics"), api("/api/ai/usage?days=7"),
      ]);

      const mode = status.mode || "?";
      const pill = $("#mode-pill");
      pill.textContent = mode === "live" ? "LIVE" : mode === "dry" ? "DRY-RUN" : "OFF";
      pill.className = "pill " + (mode === "live" ? "pill-ok" : mode === "dry" ? "pill-warn" : "pill-err");
      const farmPill = $("#farm-pill");
      if (farmPill) {
        farmPill.textContent = status.scheduler_running ? "Ферма: работает" : "Ферма: остановлена";
        farmPill.className = "pill " + (status.scheduler_running ? "pill-ok" : "pill-err");
      }
      $("#brand-sub").textContent =
        `${status.channels} каналов · ${status.sources} источников · ${status.embedder}`;

      renderAlerts(status.alerts || []);
      state.dzenStatus = dzen || {};
      renderDzen(dzen || {});
      renderKPIs(status.kpis || status);
      renderReport(report);
      renderPipeline(runs.runs || []);
      renderTrends(trends.trends || []);
      renderPosts(posts.posts || []);
      renderChannels(channels.channels || []);
      renderSources(sources.sources || []);
      state.data.crossposts = crossposts;
      renderCrosspostHealth(deliveryHealth || {});
      renderCrossposts(crossposts.crossposts || []);
      renderRuns(runs.runs || []);
      renderMemory(memory || {});
      renderTopicDossier(state.topicDossier);
      renderPlatforms(platforms || {});
      renderRedditMetrics(redditMetrics || {});
      renderAIUsage(aiUsage || {});
    } catch (e) {
      toast("Не удалось обновить данные: " + e.message, "err");
    } finally {
      state.inFlight = false;
    }
  }

  async function runCycle() {
    const btn = $("#run-cycle");
    btn.classList.add("is-busy");
    btn.disabled = true;
    toast("Запущен цикл конвейера…");
    try {
      const res = await api("/api/cycle?collect=true&max_clusters=8");
      const ok = Object.values(res.stages || {}).filter((s) => s.ok).length;
      const bad = (res.errors || []).length;
      toast(bad ? `Цикл завершён с ошибками (${bad}) за ${res.duration_ms} мс`
                 : `Цикл завершён: ${ok} стадий за ${res.duration_ms} мс`, bad ? "err" : "ok");
      await refresh();
    } catch (e) {
      toast("Цикл не запустился: " + e.message, "err");
    } finally {
      btn.classList.remove("is-busy");
      btn.disabled = false;
    }
  }

  async function post(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(body),
    });
    const payload = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = payload.detail;
      throw new Error(typeof detail === "string" ? detail : (detail ? JSON.stringify(detail) : String(res.status)));
    }
    return payload;
  }

  async function put(url, body) {
    const res = await fetch(url, {
      method: "PUT",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(body),
    });
    const payload = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = payload.detail;
      throw new Error(typeof detail === "string" ? detail : (detail ? JSON.stringify(detail) : String(res.status)));
    }
    return payload;
  }

  window.tgfarmReconcileCrosspost = async (postId, platform, action) => {
    if (!postId || !platform) return;
    const body = { action };
    if (action === "confirm_posted") {
      if (!confirm("Подтверждаете, что пост уже появился на целевой площадке?")) return;
    } else if (action === "retry_after_check") {
      if (!confirm("Вы лично проверили целевую площадку и не нашли там этот пост?")) return;
      body.confirm_not_published = true;
    } else {
      return;
    }

    try {
      const result = await post(
        `/api/crossposts/${encodeURIComponent(postId)}/${encodeURIComponent(platform)}/reconcile`,
        body,
      );
      const outcome = (result.outcomes || [])[0];
      if (outcome && outcome.status === "unknown") {
        toast("Результат снова не подтверждён. Проверьте целевую площадку вручную.", "err");
      } else if (action === "confirm_posted") {
        toast("Кросспост отмечен как опубликованный", "ok");
      } else {
        toast("Повтор выполнен: " + ((outcome && outcome.status) || "статус не получен"), "ok");
      }
      refresh();
    } catch (e) {
      toast("Не удалось сверить кросспост: " + (e && e.message ? e.message : e), "err");
    }
  };

  window.tgfarmReconcile = async (id, action) => {
    if (!id) return;
    const body = { action };
    if (action === "confirm_published") {
      if (!confirm("Подтверждаете, что пост уже появился на площадке?")) return;
    } else if (action === "retry_after_check") {
      if (!confirm("Вы лично проверили площадку и не нашли этот пост? Только тогда безопаснее повторять.")) return;
      body.confirm_not_published = true;
    } else {
      return;
    }
    try {
      await post(`/api/posts/${encodeURIComponent(id)}/reconcile`, body);
      toast(action === "confirm_published" ? "Публикация отмечена как подтверждённая" : "Пост возвращён в очередь", "ok");
      $("#drawer").hidden = true;
      refresh();
    } catch (e) {
      toast("Не удалось сверить публикацию: " + (e && e.message ? e.message : e), "err");
    }
  };

  window.tgfarmPublish = async (id) => {
    let proceed = true;
    try { proceed = confirm("Опубликовать пост прямо сейчас?"); } catch (e) { proceed = true; }
    if (!proceed) return;
    try {
      const res = await post("/api/posts/publish", { post_public_ids: [id] });
      if (res && Number(res.failed || 0) > 0) {
        const detail = (res.errors && res.errors.length) ? res.errors[0] : (res.failed + " ошибок");
        toast("Не удалось опубликовать: " + detail, "err");
      } else {
        toast("Опубликовано", "ok");
        $("#drawer").hidden = true;
        refresh();
      }
    } catch (e) {
      toast("Ошибка публикации: " + (e && e.message ? e.message : e), "err");
    }
  };

  window.tgfarmRetryFailed = async (id) => {
    if (!confirm("Причина ошибки исправлена (например, доступ или настройки площадки)? Вернуть пост в очередь с новым лимитом попыток?")) return;
    try {
      await post(`/api/posts/${encodeURIComponent(id)}/retry`, {});
      toast("Пост возвращён в очередь", "ok");
      await refresh();
      openDrawer(id);
    } catch (e) {
      toast("Не удалось вернуть пост в очередь: " + (e && e.message ? e.message : e), "err");
    }
  };

  window.tgfarmReject = async (id) => {
    if (!confirm("Отклонить пост?")) return;
    try {
      await post("/api/posts/reject", { post_public_ids: [id], reason: "manual" });
      toast("Отклонено", "ok");
      $("#drawer").hidden = true;
      refresh();
    } catch (e) { toast("Ошибка", "err"); }
  };

  window.tgfarmCopyText = async (id) => {
    try {
      const p = await api(`/api/posts/${encodeURIComponent(id)}`);
      await navigator.clipboard.writeText(p.text || "");
      toast("Текст скопирован", "ok");
    } catch (e) { toast("Не удалось скопировать", "err"); }
  };

  window.tgfarmSetPostFilter = (status) => {
    state.postFilter = status || "";
    $$("#post-filters button").forEach((x) => x.classList.toggle("is-active", x.dataset.status === state.postFilter));
    refresh();
  };

  window.tgfarmSyncChannels = async () => {
    toast("Синхронизация каналов…");
    try { await refresh(); toast("Каналы обновлены", "ok"); }
    catch (e) { toast("Ошибка синхронизации", "err"); }
  };

  window.tgfarmToggleTheme = () => {
    const html = document.documentElement;
    const dark = html.getAttribute("data-theme") !== "light";
    html.setAttribute("data-theme", dark ? "light" : "dark");
  };

  window.tgfarmToggleFullscreen = () => {
    if (!document.fullscreenElement) document.documentElement.requestFullscreen?.();
    else document.exitFullscreen?.();
  };

  /* ---------------------------------------------------------------- wiring */
  function switchView(view) {
    state.view = view;
    $$(".tab").forEach((t) => t.classList.toggle("is-active", t.dataset.view === view));
    $$(".view").forEach((v) => v.classList.toggle("is-active", v.dataset.view === view));
  }

  // Hooks the native application menu calls. They exist in the browser too, so
  // the page never has to know whether it is running inside a window.
  window.tgfarmGoto = (view) => switchView(view);
  window.tgfarmRefresh = () => refresh();

  // Inside the desktop build the native "Старт цикла" drives the host process
  // directly, which keeps publishing independent of this window.
  window.tgfarmHostCycle = () => {
    if (window.pywebview && window.pywebview.api) return window.pywebview.api.launch_cycle();
    return runCycle();
  };
  $$("#run-cycle")[0].addEventListener("click", window.tgfarmHostCycle);

  $$(".tab").forEach((t) => t.addEventListener("click", () => switchView(t.dataset.view)));
  $$("[data-goto]").forEach((b) => b.addEventListener("click", () => switchView(b.dataset.goto)));

  $("#crosspost-filter")?.addEventListener("change", (event) => {
    state.crosspostFilter = event.target.value || "all";
    renderCrossposts((state.data.crossposts && state.data.crossposts.crossposts) || []);
  });

  $("#crosspost-export")?.addEventListener("click", async () => {
    const button = $("#crosspost-export");
    if (button) button.disabled = true;
    try {
      const report = await api("/api/crossposts/export?limit=300");
      const blob = new Blob([JSON.stringify(report, null, 2)], { type: "application/json;charset=utf-8" });
      const href = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = href;
      link.download = `tgfarm-delivery-diagnostics-${new Date().toISOString().replace(/[:.]/g, "-")}.json`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(href);
      toast("Отчёт доставки экспортирован без текста ошибок и секретов", "ok");
    } catch (error) {
      toast("Не удалось экспортировать отчёт: " + error.message, "err");
    } finally {
      if (button) button.disabled = false;
    }
  });

  $$("#post-filters button").forEach((b) =>
    b.addEventListener("click", () => {
      state.postFilter = b.dataset.status;
      $$("#post-filters button").forEach((x) => x.classList.toggle("is-active", x === b));
      refresh();
    }));

  $("#reddit-scout-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    submitRedditScout();
  });
  $("#reddit-scout-results")?.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-reddit-tailor]");
    if (button) await tailorRedditCandidate(button.dataset.redditTailor);
  });
  $("#reddit-publish-button")?.addEventListener("click", publishRedditDraft);
  $("#reddit-metrics-refresh")?.addEventListener("click", refreshRedditMetrics);
  $("#platform-auth-form")?.addEventListener("submit", savePlatformCredentials);
  $("#platform-auth-close")?.addEventListener("click", closePlatformAuthDialog);
  $("#platform-auth-cancel")?.addEventListener("click", closePlatformAuthDialog);
  $("#platform-auth-check")?.addEventListener("click", async () => {
    if (state.authPlatformId) await checkPlatformAuthorization(state.authPlatformId);
  });
  $("#platform-auth-dialog")?.addEventListener("close", () => {
    state.authSaving = false;
    state.authPlatformId = "";
  });

  $("#platform-search")?.addEventListener("input", (event) => {
    state.platformQuery = event.target.value || "";
    renderPlatformCards();
  });
  $("#platform-filters")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-platform-filter]");
    if (!button) return;
    state.platformFilter = button.dataset.platformFilter || "all";
    $("#platform-filters [data-platform-filter]").forEach((item) =>
      item.classList.toggle("is-active", item === button));
    renderPlatformCards();
  });
  $("#platform-categories")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-platform-category]");
    if (!button) return;
    state.platformCategory = button.dataset.platformCategory || "Все";
    renderPlatformCategories(state.platformData?.platforms || []);
    renderPlatformCards();
  });
  $("#platforms-grid")?.addEventListener("click", async (event) => {
    const authConfig = event.target.closest("[data-platform-auth-config]");
    if (authConfig) {
      await openPlatformAuthorization(authConfig.dataset.platformAuthConfig, true);
      return;
    }
    const authSetup = event.target.closest("[data-platform-auth]");
    if (authSetup) {
      await openPlatformAuthorization(authSetup.dataset.platformAuth);
      return;
    }
    const authCheck = event.target.closest("[data-platform-auth-check]");
    if (authCheck) {
      await checkPlatformAuthorization(authCheck.dataset.platformAuthCheck, authCheck);
      return;
    }
    const toggle = event.target.closest("[data-platform-toggle]");
    if (toggle) {
      const item = (state.platformData?.platforms || []).find((entry) => entry.id === toggle.dataset.platformToggle);
      if (item) await updatePlatformSwitch(item.id, !item.enabled);
      return;
    }
    const plan = event.target.closest("[data-platform-plan]");
    if (plan) {
      await updatePlatformPlan(plan.dataset.platformPlan);
      return;
    }
    if (event.target.closest("[data-platform-reset]")) {
      state.platformQuery = "";
      state.platformFilter = "all";
      state.platformCategory = "Все";
      $("#platform-search").value = "";
      $("#platform-filters [data-platform-filter]").forEach((item) =>
        item.classList.toggle("is-active", item.dataset.platformFilter === "all"));
      renderPlatformCategories(state.platformData?.platforms || []);
      renderPlatformCards();
    }
  });

  $("#memory-search-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    submitMemorySearch();
  });
  $("#topic-dossier-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    submitTopicDossier();
  });
  $("#topic-query")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      submitTopicDossier();
    }
  });
  $("#memory-reindex")?.addEventListener("click", async () => {
    const btn = $("#memory-reindex");
    btn.disabled = true;
    try {
      const result = await post("/api/memory/reindex", {});
      toast(
        result.available
          ? `Архив переиндексирован: ${n(result.indexed_posts)} постов`
          : "FTS5 недоступен, используется резервный поиск",
        result.available ? "ok" : "err",
      );
      const data = await api(`/api/memory?limit=20${state.memoryQuery ? "&query=" + encodeURIComponent(state.memoryQuery) : ""}`);
      renderMemory(data);
    } catch (error) {
      toast("Не удалось обновить индекс: " + error.message, "err");
    } finally {
      btn.disabled = false;
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") $("#drawer").hidden = true;
    if (e.key === "/" && state.view === "platforms" && !e.ctrlKey && !e.metaKey
      && !["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName)) {
      e.preventDefault();
      $("#platform-search")?.focus();
    }
  });

  $("#clock").textContent = new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  setInterval(() => {
    $("#clock").textContent = new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  }, 20000);

  refresh();
  state.refreshTimer = setInterval(() => {
    if (!document.hidden) refresh();
  }, 8000);
})();