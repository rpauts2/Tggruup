/* =========================================================================
   tgfarm dashboard — vanilla, no build step, no framework.
   ========================================================================= */
(() => {
  "use strict";

  const API = "";
  const state = {
    view: "overview",
    postFilter: "",
    data: {},
    refreshTimer: null,
    inFlight: false,
    bestHour: null,
  };

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  const fmt = new Intl.NumberFormat("ru-RU");
  const n = (v) => fmt.format(Math.round(Number(v) || 0));
  const pct = (v) => `${((Number(v) || 0) * 100).toFixed(1)}%`;

  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

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
    publish_unknown: "нужна сверка", rejected: "отклонён", failed: "ошибка", draft: "черновик",
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

    const queue = posts.filter((p) => ["publish_unknown", "scheduled", "gated"].includes(p.status)).slice(0, 6);
    // With nothing waiting, an empty panel tells the operator nothing. Fall back
    // to the most recent posts so the card always reflects the last real output.
    const title = $("#overview-queue-title");
    if (queue.length) {
      title.textContent = "Ближайшие публикации";
      $("#overview-queue").innerHTML = queue.map((p) => `
      <div class="item" data-post="${esc(p.id)}">
        <div class="item-main">
          <div class="item-title">${esc(p.hook)}</div>
          <div class="item-meta">${esc(p.channel_title || p.channel_id)} · ${p.status === "publish_unknown" ? "проверьте площадку перед повтором" : (p.scheduled_at ? clock(p.scheduled_at) : "ждёт слота")}</div>
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

  function renderSources(sources) {
    $("#sources-list").innerHTML = sources.map((s) => {
      const st = s.failures >= 3 ? "tag-err" : s.last_status === "ok" ? "tag-ok" : "";
      return `
        <div class="item">
          <div class="item-main">
            <div class="item-title">${esc(s.name || s.id)}</div>
            <div class="item-meta">${esc(s.kind)} · вес ${s.weight} · ${dt(s.last_fetched_at)}${s.last_error ? " · " + esc((s.last_error || "").slice(0, 70)) : ""}</div>
          </div>
          <div class="item-side">
            <span class="tag ${st}">${esc(s.last_status)}</span>
            ${s.failures ? `<span class="tag tag-err">${s.failures} ошибок</span>` : ""}
          </div>
        </div>`;
    }).join("") || `<div class="empty">Источники не заданы</div>`;
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
      const [status, report, runs, trends, posts, channels, sources, dzen] = await Promise.all([
        api("/api/status"), api("/api/report?days=14"), api("/api/runs?limit=40"),
        api("/api/trends?limit=25"),
        api(`/api/posts?limit=40${state.postFilter ? "&status=" + state.postFilter : ""}`),
        api("/api/channels"), api("/api/sources"), api("/api/dzen/status"),
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
      renderDzen(dzen || {});
      renderKPIs(status.kpis || status);
      renderReport(report);
      renderPipeline(runs.runs || []);
      renderTrends(trends.trends || []);
      renderPosts(posts.posts || []);
      renderChannels(channels.channels || []);
      renderSources(sources.sources || []);
      renderRuns(runs.runs || []);
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
    if (!res.ok) throw new Error(String(res.status));
    return res.json();
  }

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

  $$("#post-filters button").forEach((b) =>
    b.addEventListener("click", () => {
      state.postFilter = b.dataset.status;
      $$("#post-filters button").forEach((x) => x.classList.toggle("is-active", x === b));
      refresh();
    }));

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") $("#drawer").hidden = true;
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