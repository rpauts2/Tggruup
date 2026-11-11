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
    tiktokPostId: "",
    tiktokPrepared: null,
    tiktokBusy: false,
    tiktokResultStatus: "",
    shortVideoBusy: new Set(),
    agentBenchmarkCluster: "",
    agentBenchmarkBusy: false,
  };

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  const fmt = new Intl.NumberFormat("ru-RU");
  const n = (v) => fmt.format(Math.round(Number(v) || 0));
  const pct = (v) => `${((Number(v) || 0) * 100).toFixed(1)}%`;
  // Journal counters arrive as `int | null` (`tgfarm/api.py::_stage_counters`):
  // `null` is "nobody measured this", `0` is "measured: nothing happened".
  // `n()` ends in `|| 0`, so it must never be handed a null — that would put the
  // fabricated zero back, which is the one thing the journal was fixed to remove.
  const items = (v, state) => (typeof v === "number" ? `${n(v)} шт`
    : `— (${state === "legacy" ? "до контракта" : "нет отчёта"})`);
  // Same rule for ratios, which `pct()` cannot serve: it ends in `|| 0` too, so a
  // rate the server could not compute (`null` from `func.avg` over no rows, a
  // share with no denominator) would print "0,0%" and look like a measured quiet.
  const rate = (v) => (typeof v === "number" ? pct(v) : "нет отчёта");

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
      { label: "Отклонено", value: n(s.rejected), hint: rate(s.reject_rate) + " от черновиков", kind: s.reject_rate > 0.5 ? "warn" : "" },
      { label: "ERR (средний)", value: rate(s.avg_er), hint: s.avg_er === null || s.avg_er === undefined ? "замеров не было" : "вовлечённость", kind: s.avg_er >= 0.05 ? "ok" : "" },
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

  /* ------------------------------------------------- freshness: collector half
     The platform rows above answer «есть ли у площадки числа». They cannot
     answer the question an operator actually has to be told about: a job that
     stands in the schedule every N hours and still leaves no dated snapshot.
     `collector_freshness` in `tgfarm/s8_analytics/freshness.py` returns those
     three claims apart — schedule (`job_id`, `interval_hours`, `scheduled`),
     data (`rows`, `last_collected_at`, `age_hours`, `data_status`), attempt
     (`runs_recorded`, `last_run_at`, `last_run_status`) — and this card keeps
     them on three separate lines, because "scheduled" is not evidence of
     "fresh" and must not be readable as it.

     P34 governs every cell here. `rows: 0` is a measurement that happened and
     found an empty table; `rows: null` is no measurement at all. The same pair
     exists for `runs_recorded`, where `null` means the journal names no stage
     (or could not be read) and `0` means it was read and holds nothing. Only a
     value that came from the payload is printed as a number; everything else
     prints the file's own no-report literal through `hrs`/`NO_REPORT`.
  */
  const NO_REPORT = "нет отчёта";
  const cell = (v) => (typeof v === "number" ? n(v) : NO_REPORT);
  const rowsCell = (v) => (typeof v === "number" ? `${n(v)} снимков` : NO_REPORT);
  const hasRows = (v) => typeof v === "number" && v > 0;
  const runsCell = (v) => (typeof v === "number"
    ? (v === 0 ? "ни одной записи о запуске" : `${n(v)} записей о запуске`)
    : NO_REPORT);
  const stampCell = (v) => (v ? dt(v) : NO_REPORT);
  const everyCell = (v) => (typeof v === "number" ? `каждые ${n(v)} ч` : `период ${NO_REPORT}`);
  // A threshold is printed with the rule that produced it: a bare number next to
  // an hourly job is exactly how «24 ч» came to be read as that job's bound.
  const thresholdCell = (v, derivation) => (typeof v === "number"
    ? `порог ${n(v)} ч${derivation ? `: ${derivation}` : ""}`
    : `порог ${NO_REPORT}`);

  // `data_status` values come from `COLLECTOR_*` in s8_analytics/freshness.py.
  // An unrecognised status keeps the server's own word on a colourless chip:
  // inventing a colour for a state we never agreed on would be a diagnosis.
  const COLLECTOR_CHIP = {
    ok: { cls: "tag-ok", label: "снимки свежие" },
    stale: { cls: "tag-warn", label: "снимки устарели" },
    no_report: { cls: "tag-none", label: NO_REPORT },
    missing_table: { cls: "tag-err", label: "таблицы нет в базе" },
    unreadable: { cls: "tag-err", label: "таблицу не прочитать" },
  };

  // The state this whole card exists to make visible, and the reason it needs a
  // chip of its own colour: it is neither `ok` (the numbers are not there) nor an
  // error (nothing failed — the tick simply produced nothing), and a reader who
  // sees only one of those two words will send the operator after the wrong thing.
  const COLLECTOR_SILENT_CHIP = { cls: "tag-silent", label: "по расписанию, данных нет" };

  function freshnessCollectorItem(row, threshold) {
    const data = row || {};
    const known = COLLECTOR_CHIP[data.data_status];
    const chip = known || { cls: "tag-none", label: String(data.data_status || NO_REPORT) };
    const silent = data.scheduled_but_silent ? COLLECTOR_SILENT_CHIP : null;
    const title = esc(data.job_id || data.table || data.id || `сборщик ${NO_REPORT}`);
    const platform = data.platform ? " · " + esc(data.platform) : "";
    // Each row carries the bound it was judged by. `threshold` is only a legacy
    // fallback for an older payload that had no per-row number; the current
    // server has no report-wide collector threshold at all, and borrowing
    // `platform_stale_after_hours` here would put the 24-hour constant back
    // beside an hourly job — the defect this block exists to show.
    const bound = data.stale_after_hours !== undefined ? data.stale_after_hours : threshold;
    const boundNote = typeof data.stale_bound_derivation === "string"
      ? data.stale_bound_derivation : "";
    return '<div class="item freshness-collector' + (silent ? " is-silent" : "") + '">'
      + '<div class="item-main"><div class="item-title">' + title + platform + "</div>"
      + '<div class="item-meta">читает ' + esc(data.snapshot_table || NO_REPORT)
      + " · " + rowsCell(data.rows)
      + " · последний снимок " + hrs(data.age_hours)
      + " (" + esc(thresholdCell(bound, boundNote)) + ")</div>"
      + '<div class="item-meta">' + esc(data.scheduled ? "задача в расписании" : "задачи в расписании нет")
      + " · " + everyCell(data.interval_hours)
      + " · журнал запусков: " + runsCell(data.runs_recorded)
      + " · последний запуск " + stampCell(data.last_run_at)
      + (data.last_run_status ? " · " + esc(data.last_run_status) : "") + "</div>"
      + (data.reason ? '<div class="item-meta freshness-claim">' + esc(data.reason) + "</div>" : "")
      + '</div><div class="item-side"><span class="tag ' + chip.cls + '">' + esc(chip.label)
      + "</span>"
      + (silent ? '<span class="tag ' + silent.cls + '">' + esc(silent.label) + "</span>" : "")
      + "</div></div>";
  }

  // Absent key, failed collector read, and an honestly empty list are three
  // different states and print three different sentences: an older server (or a
  // read that died) must never look like "the registry has no collectors".
  function freshnessListBlock(root, items, threshold, heading, emptyNote) {
    if (!root) return;
    if (!Array.isArray(items)) {
      root.innerHTML = '<div class="list-label">' + esc(heading) + "</div>"
        + '<div class="empty">Сервер не вернул этот список: ' + esc(NO_REPORT) +
        " — это не пустой список, это отсутствие отчёта.</div>";
      return;
    }
    if (!items.length) {
      root.innerHTML = '<div class="list-label">' + esc(heading) + "</div>"
        + '<div class="empty">' + esc(emptyNote) + "</div>";
      return;
    }
    root.innerHTML = '<div class="list-label">' + esc(heading) + "</div>"
      + items.map((row) => freshnessCollectorItem(row, threshold)).join("");
  }

  function renderMetricsFreshness(payload) {
    const root = $("#metrics-freshness");
    if (!root) return;
    const platforms = (payload && payload.platforms) || {};
    const summary = (payload && payload.summary) || {};
    const meta = $("#metrics-freshness-meta");
    if (meta) {
      // `cell` rather than `summary.ok || 0`: an older server that answers without
      // these counters has not measured four states, it has not answered.
      meta.textContent = `${cell(summary.ok)} свежих · ${cell(summary.stale)} устаревших · `
        + `${cell(summary.never)} без данных · ${cell(summary.blocked)} заблокировано`;
    }
    const order = { ok: 0, stale: 1, never: 2, blocked: 3 };
    const labels = { ok: "свежие", stale: "устарели", never: "нет данных", blocked: "сбор заблокирован" };
    const rows = Object.values(platforms)
      .filter((row) => row && (hasRows(row.snapshots) || row.status === "blocked"))
      .sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9) || String(a.id).localeCompare(String(b.id)));
    if (!rows.length) {
      root.innerHTML = '<div class="empty">Метрик пока нет: ни один сборщик не записал снимок.</div>';
    } else {
      root.innerHTML = rows
        .map((row) => '<div class="item"><div class="item-main"><strong>' + esc(row.name || row.id) + "</strong>"
          + '<span class="tag">' + esc(labels[row.status] || row.status) + "</span></div>"
          + '<div class="item-meta">снимков ' + cell(row.snapshots) + " · последний " + hrs(row.age_hours)
          + (row.reason ? " · " + esc(row.reason) : "") + "</div></div>")
        .join("");
    }
    // The collector and unregistered-table blocks are rendered even when the
    // platform rows are empty: an empty platform list with a silent collector is
    // exactly the case the platform rows alone cannot tell apart.
    const threshold = payload && payload.stale_after_hours !== undefined
      ? payload.stale_after_hours
      : summary.stale_after_hours;
    freshnessListBlock(
      $("#metrics-freshness-collectors"),
      payload ? payload.collectors : undefined,
      threshold,
      "Сборщики: читает ли задача, стоящая в расписании, новые строки",
      "Зарегистрированных сборщиков нет — расписание не наполняет ни одну таблицу.",
    );
    freshnessListBlock(
      $("#metrics-freshness-tables"),
      payload ? payload.tables_without_collector : undefined,
      threshold,
      "Таблицы со снимками, за которыми не стоит ни одна задача",
      "Отдельных таблиц без сборщика нет.",
    );
    // The middle case the old report could not name: a dated table that IS filled
    // by a registered tick, just not by its own row. `channel_stats` is that table,
    // and an older payload that never mentions it must read as «нет отчёта», not as
    // «вторичных таблиц нет».
    freshnessListBlock(
      $("#metrics-freshness-destinations"),
      payload ? payload.secondary_destinations : undefined,
      threshold,
      "Вторые таблицы тех же задач: снимки пишет один тик, а порог у каждой строки свой",
      "Ни одна зарегистрированная задача не пишет вторую датированную таблицу.",
    );
    const collectorsError = (payload && payload.collectors_error) || summary.collectors_error;
    const tablesRoot = $("#metrics-freshness-tables");
    if (collectorsError && tablesRoot) {
      tablesRoot.insertAdjacentHTML("afterbegin",
        '<div class="alert alert-err"><b>Чтение сборщиков не удалось:</b> '
        + esc(collectorsError) + "</div>");
    }
  }

  // `blocked` and `unmeasured` each cover two opposite silences, and the panel has
  // no right to merge them: a collector nobody wrote is OUR gap, closable in code,
  // while a collector that exists and still comes back empty is waiting for
  // something only the operator can hand over (a VK user token, an authorised
  // Telegram session — see `BLOCKED_REASONS`, `SCHEDULED_COLLECTORS` and
  // `SESSION_GATED_COLLECTORS` in coverage.py). Since P52 the server states the
  // kind itself as `row.collector_state` — derived from the job specs registered
  // in `pipeline.collector_job_specs`, so "scheduled, therefore covered" cannot
  // be claimed anywhere — and a scheduled-but-gated platform gets its own chip
  // naming that the operator's configuration stops the tick. The reason string
  // stays the server's own prose and carries the specific flag or credential.
  // Kept above `COVERAGE_STATUS` because the test that pins the four statuses
  // reads the table between those two declarations and expects four entries.
  const COVERAGE_GAP = {
    ours: { cls: "tag-none", label: "сборщика нет — это наш пробел" },
    operator: { cls: "tag-warn", label: "нужно только от оператора" },
    // A registered job whose snapshots do not land in post_stats (telegraph):
    // the schedule runs and the learning table stays empty — a gap only we close.
    ours_table: { cls: "tag-none", label: "снимки не в post_stats — наш пробел" },
  };

  // `collector_state` values come from `STATE_*` in s8_analytics/coverage.py.
  // `scheduled_running` is not a kind of silence at all, so it gets no chip.
  const COVERAGE_GAP_BY_STATE = {
    absent: COVERAGE_GAP.ours,
    scheduled_other_table: COVERAGE_GAP.ours_table,
    scheduled_gated: COVERAGE_GAP.operator,
    scheduled_running: null,
  };


  // The four statuses come from tgfarm/s8_analytics/coverage.py (STATUS_*). Each
  // gets its own colour and its own wording: "nobody measures this platform" and
  // "we measure it and throw the numbers away" are different defects, and an
  // operator who sees one grey label for both cannot act on either.
  const COVERAGE_STATUS = {
    blocked: { cls: "tag-err", label: "измерение заблокировано", order: 0 },
    unmeasured: { cls: "tag-none", label: "не измерено", order: 1 },
    partial: { cls: "tag-warn", label: "измеряет частично", order: 2 },
    measured: { cls: "tag-ok", label: "измеряется", order: 3 },
  };

  function renderMetricsCoverage(payload) {
    const root = $("#metrics-coverage");
    if (!root) return;
    const data = payload || {};
    const meta = $("#metrics-coverage-meta");
    // A failed read has no numbers at all. Rendering the totals of the empty
    // report it comes back as would print "0 of 0 measured", which looks exactly
    // like a quiet platform and would destroy the only reason this audit exists.
    if (data.error) {
      if (meta) meta.textContent = "Аудит не выполнен — ниже не нули, а отсутствие данных";
      root.innerHTML = '<div class="alert alert-err"><b>Чтение покрытия не удалось:</b> '
        + esc(data.error) + "</div>"
        + '<div class="empty">Ни одной цифры не показано: вместо них был бы ноль.</div>';
      return;
    }
    const rows = Object.values(data.platforms || {});
    if (!rows.length) {
      if (meta) meta.textContent = "Измерять пока нечего: публикаций нет";
      root.innerHTML = '<div class="empty">Ни на одной площадке нет публикаций — аудит пуст, '
        + "это не ноль покрытия, это отсутствие предмета.</div>";
      return;
    }
    const totals = data.totals || {};
    if (meta) {
      meta.textContent = `из ${Number(totals.published || 0)} опубликованных снимок имеют `
        + `${Number(totals.measured_posts || 0)}, в них содержательное число — `
        + `${Number(totals.measured_with_signal || 0)}`;
    }
    root.innerHTML = rows
      .slice()
      .sort((a, b) => {
        const rank = (r) => (COVERAGE_STATUS[r.status] || {}).order ?? 4;
        return rank(a) - rank(b) || Number(b.published || 0) - Number(a.published || 0);
      })
      .map((row) => {
        const status = COVERAGE_STATUS[row.status]
          || { cls: "tag-none", label: row.status || "статус не распознан" };
        // The collector line is the whole point: an absent collector is our gap,
        // a present one returning nothing is a broken read. Say which.
        const collectorLine = row.collector
          ? "сборщик " + esc(row.collector)
          : "<b>сборщика метрик нет — эту площадку никто не измеряет</b>";
        // Three different "has" statements that must never collapse into one:
        // a publication the journal cannot name (no remote id, no url) can never
        // be matched to its metrics even by a collector that works, and on
        // Telegram even *asking* needs a message id — so «measured: 0» there is
        // our bookkeeping, not the platform staying silent. `crosspost_posted`
        // and `crosspost_with_remote` are plain ints in coverage.py
        // (`int(... or 0)`), so `n()` is safe; `askable_posts` is deliberately
        // `int | None` because no column describes it on the other platforms, so
        // it rides on `items()` and prints «нет отчёта», never a fabricated zero.
        const attributionLine = "публикаций с id/url: " + n(row.crosspost_with_remote)
          + " из " + n(row.crosspost_posted)
          + " · снимок имеют " + n(row.measured_posts) + " из " + n(row.published)
          + " · id для запроса: " + items(row.askable_posts);
        // Whose gap it is: from the structured `collector_state` the server derives
        // from the registered job specs, falling back to `collector` alone —
        // see `COVERAGE_GAP` and `COVERAGE_GAP_BY_STATE`.
        const hasMatter = Number(row.published || 0) > 0 || Number(row.crosspost_posted || 0) > 0;
        const byState = hasMatter && row.collector_state in COVERAGE_GAP_BY_STATE
          ? COVERAGE_GAP_BY_STATE[row.collector_state]
          : undefined;
        const gap = byState !== undefined
          ? byState
          : (row.collector
            ? (row.status === "blocked" ? COVERAGE_GAP.operator : null)
            : (hasMatter ? COVERAGE_GAP.ours : null));

        return '<div class="item"><div class="item-main"><div class="item-title">'
          + esc(row.platform) + "</div>"
          + '<div class="item-meta">опубликовано ' + Number(row.published || 0)
          + " · со снимком " + Number(row.measured_posts || 0)
          + " · со значимым числом " + Number(row.measured_with_signal || 0)
          + " · строк метрик " + Number(row.stat_rows || 0) + "</div>"
          + '<div class="item-meta">' + attributionLine + "</div>"
          + '<div class="item-meta">' + collectorLine
          + " · последний снимок " + esc(dt(row.last_snapshot_at)) + "</div>"
          + (row.reason ? '<div class="item-meta">' + esc(row.reason) + "</div>" : "")
          + '</div><div class="item-side"><span class="tag ' + status.cls + '">'
          + esc(status.label) + "</span>"
          + (gap ? '<span class="tag ' + gap.cls + '">' + esc(gap.label) + "</span>" : "")
          + "</div></div>";
      })
      .join("");
  }

  // The machine answer to «почему ферма ничего не публикует»: the reason the
  // newest publish_tick journalled in `meta.decision` (`tgfarm/pipeline.py`
  //::_publish_decision). The six reason codes and the seven gate codes below are
  // the server's own vocabulary (reasons above; gates are the GATE_* constants
  // of tgfarm/s7_publish/__init__.py). An unknown code coming from the server is
  // printed verbatim on the colourless `tag-none` chip — exactly like the
  // coverage statuses above — because a invented colour would present a code we
  // never agreed on as a diagnosis we understand.
  const PUBLICATION_REASON = {
    published: { cls: "tag-ok", label: "публикует" },
    publish_failed: { cls: "tag-err", label: "публикация не удалась" },
    gated: { cls: "tag-warn", label: "удержано гейтами" },
    attempted_not_sent: { cls: "tag-warn", label: "пытались отправить, не вышло" },
    nothing_due: { cls: "tag-none", label: "очередей на отправку нет" },
    stage_error: { cls: "tag-none", label: "измерение не удалось" },
  };

  const PUBLICATION_GATE = {
    platform_disabled: "платформа выключена",
    platform_not_deliverable: "платформа не доставляет",
    approval_required: "пост ждёт ручного одобрения",
    persona_disabled: "персона-канал выключен",
    daily_cap: "дневной лимит площадки исчерпан",
    dzen_queue_gap: "пауза между постами Dzen",
    dzen_needs_online_model: "Dzen требует онлайн-модель",
    // A dry run is not a delivery: the chip must say that nothing was sent,
    // not leave the operator to read "published 0" as an empty queue.
    farm_not_live: "ферма не в live-режиме: ничего не отправлено",
    telegram_bot_token_missing: "нет токена Telegram-бота: ничего не отправлено",
    unknown_tg_chat: "не знаем chat_id канала: ждём sync_channels",
    // A booking the plan made for an hour this channel may not publish in: the
    // scheduler refuses rather than moving it, so the chip must say the slot is
    // the problem, not the post.
    slot_outside_local_hours: "слот плана вне локальных часов канала: публикация отложена",
    // The same booking, corrected rather than parked (W22-C): the refused minute
    // stays in the plan column and the post is scheduled at a legal one, so this
    // chip counts an outcome that did NOT stay in the queue. It is labelled
    // separately because "отложена" and "исправлена" are different answers, and
    // reading one as the other is how a repaired booking looks like a stall.
    slot_booking_rebounded: "слот плана исправлен на ближайший легальный: пост запланирован",
  };

  function renderPublicationDecision(statusPayload, failure) {
    const root = $("#publication-decision");
    if (!root) return;
    const meta = $("#publication-decision-meta");
    // Three honest states, three different wordings: «данных ещё нет», «сервер
    // ответил ошибкой» и «сеть не отвечает». They must not collapse into each
    // other, and none of them may paint a zero — an operator reading "0
    // опубликовано" out of a failed read is reading a phantom.
    if (failure) {
      const message = String(failure.message || failure || "");
      // `api()` rejects HTTP failures as "<path> -> <code>"; anything else is
      // the fetch never reaching the server at all.
      const httpError = /\s->\s\d/.test(message);
      if (meta) meta.textContent = httpError ? "статус ответил ошибкой" : "сервер не ответил";
      root.innerHTML = '<div class="alert alert-err">'
        + (httpError
          ? "<b>Решение о публикации не прочитано:</b> " + esc(message)
          : "<b>Сеть не отвечает:</b> последнее решение о публикации не получено — " + esc(message))
        + "</div><div class=\"empty\">Ни одной цифры не показано: вместо них был бы ноль.</div>";
      return;
    }
    const payload = statusPayload || {};
    // `error` is checked before any totals for the same reason the coverage
    // card checks it: an empty payload's counters are all zeros, and a zero
    // here is indistinguishable from a quiet farm.
    if (payload.error) {
      if (meta) meta.textContent = "статус не свёлся к решению";
      root.innerHTML = '<div class="alert alert-err"><b>Статус фермы нечитаем:</b> '
        + esc(payload.error) + "</div>";
      return;
    }
    const decision = payload.publication;
    if (!decision || typeof decision !== "object") {
      if (meta) meta.textContent = "решений в журнале пока нет";
      root.innerHTML = '<div class="empty">Нет данных: ни один publish-тик ещё не записал '
        + "решение в журнал прогонов. Это не «публикует ноль» — это отсутствие ответа.</div>";
      return;
    }
    if (decision.measured === false) {
      if (meta) meta.textContent = "тик отработал без измерения";
      root.innerHTML = '<div class="item"><div class="item-main"><div class="item-title">'
        + "Последний publish-тик не вернул никакого ответа</div>"
        + '<div class="item-meta">счётчики не измерены: из-за очереди, опубликовано и '
        + "не отправлено остались неизвестными, а не нулями</div></div>"
        + '<div class="item-side"><span class="tag tag-none">нет данных</span></div></div>';
      return;
    }
    const reasonKey = typeof decision.reason === "string" ? decision.reason : "";
    const known = PUBLICATION_REASON[reasonKey];
    // Unknown code: verbatim text on the colourless chip. Known code: agreed
    // label and colour. Nothing in between.
    const chip = known
      ? `<span class="tag ${known.cls}">${esc(known.label)}</span>`
      : `<span class="tag tag-none">${esc(reasonKey || "причина не записана")}</span>`;
    // Counters pass through `items()` (the d4d8ef1 guard), never through
      // `n()` directly: a missing number must stay "нет отчёта", because `n()`
      // ends in `|| 0` and would re-fabricate the zero the journal fixed.
    const counters = "из очереди " + items(decision.due)
      + " · опубликовано " + items(decision.published)
      + " · с ошибкой " + items(decision.failed)
      + " · намечено " + items(decision.scheduled);
    const gates = decision.gates && typeof decision.gates === "object"
      ? Object.entries(decision.gates) : [];
    const gateRows = gates.map(([code, count]) => {
      const label = PUBLICATION_GATE[code];
      return '<div class="item"><div class="item-main"><div class="item-title">'
        + esc(label || code) + "</div>"
        + (label ? '<div class="item-meta">код ' + esc(code) + "</div>" : "")
        + '</div><div class="item-side"><span class="tag '
        + (label ? "tag-warn" : "tag-none") + '">'
        + (typeof count === "number" ? esc(n(count)) + " шт" : "нет данных")
        + "</span></div></div>";
    }).join("");
    if (meta) {
      meta.textContent = (known ? "причина: " + known.label : "причина не распознана")
        + (gates.length ? ` · гейтов с удержанными постами: ${gates.length}` : "");
    }
    root.innerHTML = '<div class="item"><div class="item-main"><div class="item-title">'
      + "Последний publish-тик</div>"
      + '<div class="item-meta">' + esc(counters) + "</div></div>"
      + '<div class="item-side">' + chip + "</div></div>"
      + gateRows;
  }

  // Evidence states come from `tgfarm/s8_analytics/formats.py`: `EVIDENCE_OK`,
  // `EVIDENCE_MIXED`, `EVIDENCE_STALE`, `EVIDENCE_NEVER` and the `blocked` spelling
  // its `EVIDENCE_UNTRUSTED` set already names. The block exists because
  // `measured: 9` and `measured: 9 with every snapshot six days old` are different
  // facts — and only one of them moves the farm: `planner_history` hands the
  // planner the LIVE counts, so a format measured solely by dead snapshots arrives
  // there as `measured: 0` and the plan falls back to the rules. A panel that
  // showed the same format as "enough data" would be claiming a readiness the
  // planner has already refused, which is what the chips below prevent.
  // An unrecognised code travels verbatim on the colourless chip like every other
  // table here: a colour we never agreed on would dress a new server state up as a
  // diagnosis we understand.
  const EVIDENCE_STATUS = {
    ok: { cls: "tag-ok", label: "замеры живые" },
    mixed: { cls: "tag-warn", label: "живые и мёртвые вперемешку" },
    stale: { cls: "tag-err", label: "все замеры устарели" },
    never: { cls: "tag-none", label: "формат не измеряли" },
    blocked: { cls: "tag-err", label: "сбор метрик заблокирован" },
  };

  // Mirror of `formats.EVIDENCE_UNTRUSTED`: states carrying no live evidence at
  // all. Neither `stale` nor `never` nor `blocked` may ever borrow the green chip,
  // however many snapshots it counts.
  const EVIDENCE_UNTRUSTED = ["stale", "never", "blocked"];

  // Newest of the ISO stamps the report carries, or null when none parses.
  // `format_performance` emits per-format stamps only, so the card's "newest
  // measurement" is the max over those — a derived number, and it says so by
  // being labelled «самый новый замер» rather than a server field.
  const latestMeasuredAt = (values) => values.reduce((newest, value) => (
    typeof value === "string" && !Number.isNaN(Date.parse(value))
      && (newest === null || Date.parse(value) > Date.parse(newest)) ? value : newest
  ), null);

  function renderMetricsFormats(payload) {
    const root = $("#metrics-formats");
    if (!root) return;
    const data = payload || {};
    const formats = data.formats || {};
    // A threshold the server did not send is not a threshold of zero: the row has
    // to say it does not know how many measurements it wanted.
    const minSampleText = Number.isInteger(data.min_sample) && data.min_sample > 0
      ? String(data.min_sample) : "нет отчёта";
    const meta = $("#metrics-formats-meta");
    // The shared card contract: `error` is checked before a single total is
    // printed. `format_performance` has no error field of its own today (a dead
    // read throws, the endpoint answers 500 and refresh() counts the section as
    // failed), so this branch is the contract rather than a live path — but the
    // day the read learns to answer 200-with-error, the totals it would carry are
    // zeros, and a zero here is indistinguishable from an unmeasured farm.
    if (data.error) {
      if (meta) meta.textContent = "Отчёт не прочитан — ниже не нули, а отсутствие данных";
      const failedPublication = $("#metrics-formats-publication");
      if (failedPublication) {
        failedPublication.innerHTML = '<div class="empty">Форматы не прочитаны, поэтому и '
          + "подтверждённость публикаций не измерялась: вместо этих цифр был бы ноль.</div>";
      }
      root.innerHTML = '<div class="alert alert-err"><b>Форматы не прочитаны:</b> '
        + esc(data.error) + "</div>"
        + '<div class="empty">Ни одной цифры не показано: вместо них был бы ноль.</div>';
      return;
    }
    // The freshness half of the same report. Absent means the panel is talking to
    // a backend older than the block, and then the honest statement is "we do not
    // know which snapshots are still alive" — never the old reading, which implied
    // every measurement counted.
    const measurement = (data.measurement && typeof data.measurement === "object")
      ? data.measurement : null;
    const newest = latestMeasuredAt(measurement
      ? Object.values(measurement).map((entry) => (entry || {}).last_measured_at)
      : []);
    const threshold = Number.isInteger(data.stale_after_days) ? data.stale_after_days : null;
    // The report's own verdict over every format (`evidence_status` at the top
    // level). An unrecognised code is printed as it arrived rather than folded
    // into a known one; `textContent` means no markup can reach the DOM here.
    const overallKey = typeof data.evidence_status === "string" ? data.evidence_status : "";
    const overallChip = measurement
      ? (EVIDENCE_STATUS[overallKey] || { label: overallKey || "нет отчёта" }).label
      : "нет отчёта";
    if (meta) {
      // The threshold and the newest stamp belong in the header because they are
      // what turns "measured" into a fact an operator can weigh. `n()` ends in
      // `|| 0` and must never receive a possibly-null counter, so the two counts
      // this block adds ride on `items()`.
      meta.textContent = `${Number(data.published_total || 0)} опубликовано · `
        + `${Number(data.measured_total || 0)} с замерами · живых ${items(data.fresh_measured_total)}`
        + ` · мёртвых ${items(data.stale_measured_total)}`
        + ` · окно ${Number(data.window_days || 0)} дн`
        + ` · порог живости ${threshold === null ? "нет отчёта" : `${threshold} дн`}`
        + ` · самый новый замер ${newest === null ? "нет отчёта" : dt(newest)}`
        + ` · вывод: ${overallChip}`;
    }
    const rows = Object.values(formats);
    // The report-level publication line is drawn before the row check: a window
    // with no published posts still has an evidence answer to give (and a
    // `publish_unknown` count that outlives the window), and an absent block must
    // say so once here instead of silently dropping the whole dimension.
    const publication = (data.publication && typeof data.publication === "object")
      ? data.publication : null;
    const publicationRoot = $("#metrics-formats-publication");
    if (publicationRoot) publicationRoot.innerHTML = formatsPublicationPanel(publication, data);
    if (!rows.length) {
      root.innerHTML = '<div class="empty">В этом окне не было опубликовано ни одного поста — про форматы ничего не известно.</div>';
      return;
    }
    root.innerHTML = rows
      .map((row) => {
        const measured = Number(row.measured || 0);
        const reliable = row.reliable === true;
        const evidence = measurement ? measurement[row.format] : undefined;
        const hasEvidence = !!evidence && typeof evidence === "object";
        const statusKey = hasEvidence && typeof evidence.evidence_status === "string"
          ? evidence.evidence_status : "";
        const known = EVIDENCE_STATUS[statusKey];
        const untrusted = EVIDENCE_UNTRUSTED.indexOf(statusKey) !== -1;
        const fresh = hasEvidence && typeof evidence.fresh === "number" ? evidence.fresh : null;
        const stale = hasEvidence && typeof evidence.stale === "number" ? evidence.stale : null;
        const evidenceChip = !hasEvidence
          ? '<span class="tag tag-none">свежесть не измерена</span>'
          : `<span class="tag ${known ? known.cls : "tag-none"}">`
            + esc(known ? known.label : (statusKey || "статус не записан")) + "</span>";
        const evidenceMeta = !hasEvidence
          ? "отчёт о свежести не пришёл: живые и мёртвые снимки неразличимы"
          : "живых " + items(fresh) + " · мёртвых " + items(stale)
            + " · самый новый снимок " + dt(evidence.last_measured_at)
            + (fresh ? " · самый новый живой " + dt(evidence.fresh_last_measured_at) : "");
        // The average an operator may act on. `avg_engagement` blends dead
        // snapshots into a mixed format, so the live-only figure is the one worth
        // a chip; when there is no live snapshot at all, the old average is
        // history and gets the red of a dead read instead of the accent blue of a
        // score. An empty average is never printed as `0.0`, because "nothing was
        // measured" is not "measured: nothing" — and since P81 the report itself
        // sends `null` here, so the absence is read as an absence rather than
        // coerced: `Number(null || 0).toFixed(1)` would print that `0.0`.
        const attention = !hasEvidence
          ? (typeof row.avg_engagement === "number"
            ? `<span class="tag tag-acc">внимание ${esc(row.avg_engagement.toFixed(1))}</span>`
            : '<span class="tag tag-none">внимание: нет данных</span>')
          : (fresh !== null && fresh > 0)
            ? `<span class="tag tag-acc">внимание по живым `
              + esc(typeof evidence.fresh_avg_engagement === "number"
                ? evidence.fresh_avg_engagement.toFixed(1) : "нет данных") + "</span>"
            : (measured
              ? '<span class="tag tag-err">внимание: только мёртвые замеры</span>'
              : '<span class="tag tag-none">внимание: нет данных</span>');
        // Would the planner act on this? `planner_history` gates the sample on the
        // LIVE count, so `fresh_reliable` is the only readiness claim the panel may
        // make while the block is present — and a format that is merely old never
        // gets "мало данных" either, because that would call a dead measurement a
        // small live one. The verdict keys on the COUNTS (`fresh === 0`) and on a
        // declared untrusted state, never on the code alone: a state the server
        // invents tomorrow must not inherit the green chip by being unrecognised.
        // `min_sample` is only trusted as a real threshold when it arrives as a
        // positive int, else "довольно" would be measured against a zero.
        const enoughDead = Number.isInteger(data.min_sample) && data.min_sample > 0
          ? measured >= data.min_sample : false;
        const noLiveEvidence = hasEvidence && (fresh === 0 || untrusted);
        const readiness = !hasEvidence
          ? `<span class="tag ${reliable ? "tag-ok" : "tag-none"}">`
            + (reliable ? "данных достаточно" : "мало данных") + "</span>"
          : noLiveEvidence
            ? (enoughDead
              ? '<span class="tag tag-err">замеров довольно, но они мертвы: план их отвергает</span>'
              : '<span class="tag tag-none">мало живых данных</span>')
            : evidence.fresh_reliable === true
              ? '<span class="tag tag-ok">живых данных достаточно</span>'
              : '<span class="tag tag-none">мало живых данных</span>';
        // The third subset of this same row, kept apart from both blocks above:
        // `formats[fmt].measured` counts every measurement, this one only those
        // the platform can prove were published. Absent means the panel talks to a
        // backend older than the block, and then it says «не вернул».
        const pub = hasEvidence && evidence.publication
          && typeof evidence.publication === "object" ? evidence.publication : null;
        return '<div class="item' + (noLiveEvidence ? " is-dead-evidence" : "")
          + (pub ? "" : " is-without-publication")
          + '"><div class="item-main"><div class="item-title">'
          + esc(FORMAT_RU[row.format] || row.format || "—") + "</div>"
          + '<div class="item-meta">опубликовано ' + Number(row.published || 0)
          + " · с замерами " + measured
          + (reliable ? "" : " · нужно не меньше " + minSampleText) + "</div>"
          + '<div class="item-meta">' + esc(evidenceMeta) + "</div>"
          + formatPublicationRow(pub, row) + "</div>"
          + '<div class="item-side">' + evidenceChip + readiness + attention
          + publicationChip(pub) + "</div></div>";
      })
      .join("");
  }

  /* ------------------------------------------------- publication: the third
     denominator
     `format_performance` answers three questions about the same rows and keeps
     them in three blocks: `formats[fmt]` (every published post of the window),
     `measurement[fmt]` (how many of its snapshots are still live) and
     `measurement[fmt].publication` (how many of those publications the platform
     can actually prove, per `EVIDENCE_RULES` of `tgfarm/s8_analytics/formats.py`).
     The card above rendered the first two and ignored the third, which is how a
     format whose publications nobody can prove kept reading as «данных
     достаточно»: on the live database, read 2026-11-05 over a 30-day window,
     `news` carries legacy `reliable: true, n: 8, avg 59.375` while its own
     publication block reports `sample_size: 1, reliable: false` — 414 published,
     386 proven, 28 disproven — while `stat` sits at `measured: 0, sample_size: 0`
     with 40 publications behind it.

     None of the freshness words is reused here, on purpose. `measured`,
     `avg_engagement`, `reliable`, `evidence_status` and `fresh` all exist in BOTH
     blocks with different denominators, so one shared label would fuse two
     numbers that disagree — and the disagreement is the finding. Nothing is
     removed from `published`: it is joined by the proven subset and by
     `sample_size`, the count the proven average divides by. `publish_unknown` is
     the report's COUNT of rows the repair sweep could not place; it belongs to no
     denominator above and keeps its own wording, because a count is not the
     `publish_unknown` STATUS the backlog card labels «ждёт оператора».

     P34 holds on every cell: counts through `items()`, averages through
     `pubAvg()`, tri-state verdicts through `pubWord()`. An absent key prints
     «нет отчёта», a measured empty set prints `0 шт`, and no `Number(x || 0)`
     stands anywhere in this path.
  */
  const PUBLICATION_EVIDENCE_CHIP = {
    ok: { cls: "tag-ok", label: "публикации подтверждены" },
    mixed: { cls: "tag-warn", label: "подтверждённые и без подтверждения вперемешку" },
    never: { cls: "tag-err", label: "ни одной подтверждённой публикации" },
    unknown: { cls: "tag-none", label: "для этих площадок нет правила" },
  };

  // A block an older server never sent is not a block of zeros, and it must not
  // print as one: «не вернул» stays the file's single idiom for an absent key.
  const PUBLICATION_ABSENT = "сервер не вернул блок publication: подтверждённость "
    + "публикаций не измерялась — это не «ноль подтверждённых», это нет отчёта";

  const pubAvg = (v) => (typeof v === "number" ? v.toFixed(1) : NO_REPORT);
  const pubWord = (v, yes, no) => (typeof v === "boolean" ? (v ? yes : no) : NO_REPORT);

  // Every number says which set it divides, because the same key one line above
  // divides another one.
  function publicationCounters(pub) {
    return "публикаций всего " + items(pub.published)
      + " · подтверждено правилом " + items(pub.published_evidenced)
      + " · правило есть, не подтверждено " + items(pub.published_unevidenced)
      + " · для площадки правила нет " + items(pub.published_unrulled)
      + " · замеров всего " + items(pub.measured)
      + " · подтверждённых замеров " + items(pub.measured_evidenced)
      + " · знаменатель подтверждённой средней " + items(pub.sample_size)
      + " · средняя по подтверждённым " + pubAvg(pub.avg_engagement)
      + " · живой знаменатель " + items(pub.fresh_sample_size)
      + " · живая средняя по подтверждённым " + pubAvg(pub.fresh_avg_engagement);
  }

  function publicationChip(pub) {
    if (!pub) return '<span class="tag tag-none">подтверждённость: нет отчёта</span>';
    const key = typeof pub.evidence_status === "string" ? pub.evidence_status : "";
    const known = PUBLICATION_EVIDENCE_CHIP[key];
    return '<span class="tag ' + (known ? known.cls : "tag-none") + '">'
      + "публикации: " + esc(known ? known.label : (key || "статус не записан"))
      + "</span>";
  }

  // The rules arrive as platform -> the server's own rule string. They are data,
  // so each half travels escaped and verbatim: a paraphrase would be a second,
  // possibly wrong definition of what counts as proof.
  function publicationRules(pub) {
    const rules = pub.rules && typeof pub.rules === "object" ? pub.rules : null;
    if (!rules) {
      return "правила доказательства сервер не вернул: чем подтверждается публикация — нет отчёта";
    }
    return Object.entries(rules).map(([platform, rule]) => esc(platform) + " — " + esc(rule)).join(" · ");
  }

  // Report-level only, and a count: `formats.py` says these rows sit in no
  // denominator above, so the sentence has to carry that with the number.
  function publicationUnknownCount(pub) {
    return "в статусе publish_unknown: " + items(pub.publish_unknown)
      + " — эти строки не входят ни в один знаменатель выше";
  }

  // Where the legacy row and the publication block disagree, both verdicts print
  // and neither is corrected: the UI has no basis for choosing, and a reader handed
  // a resolved version cannot see that the two numbers never measured the same set.
  function publicationConflict(legacy, pub) {
    if (typeof pub.reliable !== "boolean") return "";
    const legacyReliable = typeof legacy.reliable === "boolean" ? legacy.reliable : null;
    if (legacyReliable === null || legacyReliable === pub.reliable) return "";
    return "знаменатели разошлись: "
      + (legacyReliable
        ? "по всем замерам — данных достаточно (" + items(legacy.measured)
          + "), по подтверждённым публикациям — мало (" + items(pub.sample_size) + ")"
        : "по всем замерам — мало данных (" + items(legacy.measured)
          + "), по подтверждённым публикациям — достаточно (" + items(pub.sample_size) + ")")
      + " · расхождение не разрешено: это два разных вопроса, а не два значения одного";
  }

  function formatPublicationRow(pub, legacy) {
    if (!pub) {
      return '<div class="item-meta formats-pub-absent">' + esc(PUBLICATION_ABSENT) + "</div>";
    }
    const verdict = pubWord(pub.reliable,
      "подтверждённых публикаций довольно", "подтверждённых публикаций мало");
    const conflict = publicationConflict(legacy || {}, pub);
    return '<div class="item-meta formats-pub-row">'
      + esc(publicationCounters(pub)) + " · вывод: " + esc(verdict) + "</div>"
      + (conflict
        ? '<div class="item-meta formats-pub-conflict">' + esc(conflict) + "</div>"
        : "");
  }

  // The report-level line stands next to the report-level legacy totals for the
  // same reason the per-format row does: `published_total` counts every published
  // post, `publication.published_evidenced` counts the provable subset of them.
  function formatsPublicationPanel(pub, totals) {
    const data = totals || {};
    const head = '<div class="list-label">Подтверждённость публикаций</div>';
    const legacyLine = "по отчёту целиком: опубликовано " + items(data.published_total)
      + " · с замерами " + items(data.measured_total);
    if (!pub) {
      return head + '<div class="item"><div class="item-main">'
        + '<div class="item-meta formats-pub-absent">' + esc(PUBLICATION_ABSENT) + "</div>"
        + '<div class="item-meta">' + esc(legacyLine) + "</div>"
        + '</div><div class="item-side">' + publicationChip(null) + "</div></div>";
    }
    const verdict = pubWord(pub.reliable,
      "подтверждённых публикаций довольно", "подтверждённых публикаций мало");
    return head + '<div class="item"><div class="item-main">'
      + '<div class="item-meta">' + esc(legacyLine) + "</div>"
      + '<div class="item-meta formats-pub-row">' + esc(publicationCounters(pub))
      + " · вывод: " + esc(verdict) + "</div>"
      + '<div class="item-meta">' + esc(publicationUnknownCount(pub)) + "</div>"
      + '<div class="item-meta">правило подтверждения: ' + publicationRules(pub) + "</div>"
      + '</div><div class="item-side">' + publicationChip(pub) + "</div></div>";
  }

  // «Что происходит с очередью» used to be answerable only by hand-written SQL,
  // and the SQL was usually written against the wrong column: `scheduled` holds
  // posts booked for the future, so a post booked and published the same day
  // never appears in it and a full day of work looks like nothing moving. The
  // card below therefore prints flow off `/api/backlog`'s three clocks
  // (`created_at` intake + what those posts became, `scheduled_at` bookings,
  // `published_at` output) and stock next to it with ages, so a forward buffer
  // and an overdue-and-retried pile cannot share a line.
  // These are the `posts.status` values that still wait for something — the same
  // set the server puts in `BACKLOG_WAITING`, which a test keeps in step. An
  // unrecognised one travels verbatim on the colourless chip like every other
  // table here.
  const BACKLOG_STATE = {
    draft: { cls: "tag-none", label: "черновик, ждёт гейта" },
    gated: { cls: "tag-warn", label: "гейты прошёл, ждёт окна" },
    scheduled: { cls: "tag", label: "забронировано" },
    publishing: { cls: "tag", label: "отправка идёт" },
    publish_unknown: { cls: "tag-warn", label: "неизвестно, ушло ли — ждёт оператора" },
    failed: { cls: "tag-err", label: "сорвалось, ждёт оператора" },
  };

  // Age is stated in hours because that is the unit the gates and the alerts
  // speak; a missing stamp prints «нет отчёта» instead of a zero, exactly like
  // `items()` does for counters.
  const hrs = (v) => (typeof v === "number" ? `${Math.round(v)} ч` : "нет отчёта");
  // The ratio that makes the problem visible: intake against output. A period
  // with no published posts has no ratio at all — it is neither infinity nor 0×,
  // so the server sends null and the panel says the relation is not computable.
  const leadVs = (v) => (typeof v === "number"
    ? `приток больше выпуска в ${v.toFixed(1).replace(".", ",")} раза`
    : "выпуска за период не было — отношение притока к выпуску не вычисляется");

  function renderBacklog(payload, failure) {
    const root = $("#backlog-list");
    if (!root) return;
    const meta = $("#backlog-meta");
    // The three silences stay three different wordings, as in the publication
    // card: the network died, the server answered with an error, and there is
    // simply nothing to show yet. None of them may print a zero.
    if (failure) {
      const message = String(failure.message || failure || "");
      const httpError = /\s->\s\d/.test(message);
      if (meta) meta.textContent = httpError ? "очередь не прочитана" : "сервер не ответил";
      root.innerHTML = '<div class="alert alert-err">'
        + (httpError
          ? "<b>Состояние очереди не прочитано:</b> " + esc(message)
          : "<b>Сеть не отвечает:</b> очередь публикации не получена — " + esc(message))
        + "</div><div class=\"empty\">Ни одной цифры не показано: вместо них был бы ноль.</div>";
      return;
    }
    const data = payload || {};
    if (data.error) {
      if (meta) meta.textContent = "очередь не свелась к цифрам";
      root.innerHTML = '<div class="alert alert-err"><b>Чтение очереди не удалось:</b> '
        + esc(data.error) + "</div>"
        + '<div class="empty">Ни одной цифры не показано: вместо них был бы ноль.</div>';
      return;
    }
    const totals = data.totals || {};
    const platforms = data.platforms || [];
    if (meta) {
      meta.textContent = `за ${n(data.flow_hours)} ч: создано ${n(totals.created)}, `
        + `опубликовано ${n(totals.published)} · ждут ${n(totals.waiting)} · `
        + `снимок ${esc(dt(data.generated_at))}`;
    }
    // The blocking reason is the journal's own decision (`meta.decision` of the
    // newest publish tick), never a second guess built from the counters above.
    // The detailed counters and gates live on the card above; here the reason only
    // labels what these numbers are the consequence of.
    const decision = data.publication;
    const known = decision && PUBLICATION_REASON[decision.reason];
    const blockedLine = '<div class="item"><div class="item-main"><div class="item-title">'
      + "Почему ничего не ушло последним тиком</div>"
      + '<div class="item-meta">' + (decision
        ? "код " + esc(decision.reason || "не записан")
          + (decision.gates
            ? ` · гейтов с удержанными: ${n(Object.keys(decision.gates).length)}` : "")
        : "ни один publish-тик ещё не записал решение в журнал прогонов — это не "
          + "«публикует ноль», это отсутствие ответа")
      + '</div></div><div class="item-side">'
      + (decision
        ? `<span class="tag ${known ? known.cls : "tag-none"}">`
          + esc(known ? known.label : (decision.reason || "причина не записана")) + "</span>"
        : '<span class="tag tag-none">нет данных</span>')
      + "</div></div>";
    if (!platforms.length) {
      root.innerHTML = blockedLine
        + '<div class="empty">Ни на одной площадке нет постов: очередь пуста не потому, '
        + "что ничего не выходит, а потому, что ей неоткуда наполняться.</div>";
      return;
    }
    const lateTotal = typeof totals.overdue === "number"
      ? ` <span class="tag ${totals.overdue ? "tag-err" : "tag-ok"}">просрочено ${n(totals.overdue)}</span>`
      : "";
    const summary = '<div class="item"><div class="item-main"><div class="item-title">Всего</div>'
      + `<div class="item-meta">за ${n(data.flow_hours)} ч: создано ${n(totals.created)}`
      + ` · отклонено ${n(totals.rejected)} · забронировано ${n(totals.booked)}`
      + ` · опубликовано ${n(totals.published)}</div>`
      + `<div class="item-meta">за ${n(data.compare_days)} дней: ${leadVs(totals.lead)}</div></div>`
      + `<div class="item-side"><span class="tag">ждут ${n(totals.waiting)}</span>${lateTotal}</div></div>`;
    root.innerHTML = blockedLine + summary
      + platforms.map((item) => {
        const flow = item.flow || {};
        const week = item.week || {};
        const rates = item.rates || {};
        const stock = item.stock || [];
        // A platform whose channels are switched off produces zeros that mean
        // "the operator closed the tap", not "the farm is broken" — so how many
        // channels are enabled rides on the line and cannot be missed.
        const head = '<div class="item"><div class="item-main"><div class="item-title">'
          + esc(item.platform || "unknown") + "</div>"
          + '<div class="item-meta">за ' + n(data.flow_hours) + " ч: создано " + n(flow.created)
          + " · из них отклонено " + n(flow.rejected)
          + ", в гейте " + n(flow.gated)
          + ", черновиком " + n(flow.draft)
          + " · забронировано " + n(flow.booked)
          + " · опубликовано " + n(flow.published) + "</div>"
          + '<div class="item-meta">за ' + n(data.compare_days) + " дней: создано " + n(week.created)
          + " · отклонено " + n(week.rejected)
          + " · забронировано " + n(week.booked)
          + " · опубликовано " + n(week.published) + "</div>"
          + '<div class="item-meta">в день: ' + n(rates.created_per_day) + " создаём · "
          + n(rates.published_per_day) + " публикуем · " + leadVs(rates.lead) + "</div>"
          + '<div class="item-meta">ждут ' + n(item.waiting)
          + " · старше всех " + hrs(item.oldest_age_hours) + "</div></div>"
          + '<div class="item-side"><span class="tag '
          + (item.channels_enabled ? "tag" : "tag-warn") + '">каналов '
          + n(item.channels_enabled) + "/" + n(item.channels) + "</span></div></div>";
        if (!stock.length) {
          return head + '<div class="item"><div class="item-main">'
            + '<div class="item-meta">в ожидании никого: очередь площадки пуста</div>'
            + "</div></div>";
        }
        return head + stock.map((row) => {
          const ages = row.ages || {};
          const state = BACKLOG_STATE[row.status]
            || { cls: "tag-none", label: "состояние не распознано" };
          // `overdue` is a number only for a bucket of bookings that all carry a
          // time. A booking bucket with unbooked posts says how many are missing
          // instead of publishing a too-small count, and the states that precede
          // booking get no chip at all: a draft cannot be late, and printing 0
          // would claim a measurement nobody took.
          const late = typeof row.overdue === "number"
            ? `<span class="tag ${row.overdue ? "tag-err" : "tag-ok"}">просрочено ${n(row.overdue)}</span>`
            : (row.status === "scheduled"
              ? `<span class="tag tag-none">просрочено: ${row.unbooked
                ? `${n(row.unbooked)} без времени брони` : "нет отчёта"}</span>`
              : "");
          return '<div class="item is-backlog-state"><div class="item-main">'
            + '<div class="item-title">' + esc(row.status || "unknown") + "</div>"
            + '<div class="item-meta">ждут ' + n(row.count) + " · возраст: до суток "
            + n(ages.lt_24h) + ", до троих суток " + n(ages.lt_72h)
            + ", до недели " + n(ages.lt_168h) + ", старше недели " + n(ages.ge_168h)
            + " · самый старый " + hrs(row.oldest_age_hours) + "</div>"
            + '<div class="item-meta">попыток ' + n(row.retries)
            + (row.retrying ? " у " + n(row.retrying) + " постов" : "") + "</div>"
            + '</div><div class="item-side"><span class="tag ' + state.cls + '">'
            + esc(state.label) + "</span> " + late + "</div></div>";
        }).join("");
      }).join("");
  }

  // The Dzen attribution split — the number `f0c317a` produced and the operator
  // could not see. `metrics_status()` (`tgfarm/s8_analytics/dzen_web.py`, served as
  // the `dzen` block of `/api/report`) counts posted Dzen rows two ways:
  // `mapped_publications` is every row the journal records as posted, and
  // `mapped_publications_with_id` is the subset that names the article it posted.
  // Only the second one lets the Studio collector attach a scraped number to a
  // post without guessing by headline — and a headline guess stops working the
  // moment somebody edits the title, so a publication outside that subset is not
  // "unmeasured by Dzen", it is unattributable by our own bookkeeping. This is the
  // distinction the coverage rows above make with `crosspost_posted` and
  // `crosspost_with_remote`; Dzen's stricter count exists only here, so it gets a
  // line of its own under that card rather than being invented from data that does
  // not describe it. A missing payload means the report read failed, and that is
  // stated: the alternative is printing the zeros of an empty payload as a verdict.
  function renderDzenAttribution(dzMetrics, failed) {
    const root = $("#metrics-coverage-dzen");
    if (!root) return;
    if (failed || !dzMetrics) {
      const why = failed ? String(failed.message || failed) : "";
      root.innerHTML = '<div class="item-meta">'
        + "<b>Привязка метрик Дзена не прочитана</b>: отчёт фермы не ответил — "
        + "и это не «ни одна публикация не привязана».</div>"
        + (why ? '<div class="item-meta">' + esc(why.slice(0, 120)) + "</div>" : "");
      return;
    }
    const dz = dzMetrics;
    // `metrics_status()` coerces every one of these with `int(... or 0)`, so they
    // are ints, not unknowns; `items()` is used anyway because a payload from an
    // older build simply lacks the keys, and a missing key is not a zero.
    const withId = dz.mapped_publications_with_id;
    const posted = dz.mapped_publications;
    const note = dz.last_reason ? " · последняя причина сбора: " + esc(String(dz.last_reason)) : "";
    root.innerHTML = '<div class="item-meta">Дзен: постов в статусе «published» '
      + items(dz.published_posts)
      + " · снимок имеют " + items(dz.measured_posts)
      + " · записей «posted» " + items(posted)
      + " · из них с id статьи " + items(withId) + note + "</div>"
      + (posted === 0
        ? '<div class="item-meta">Ни одной записи публикации Дзена: привязывать метрики пока не к чему — это не «сборщик молчит», это пустой журнал.</div>'
        : "");
  }


  function renderReport(report) {
    const hours = report.hours || {};
    const entries = Object.entries(hours)
      .filter(([, v]) => typeof v === "number" && Number.isFinite(v))
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
        const measured = byHour.has(h);
        const isBest = best && h === best[0];
        const height = measured ? Math.max(4, (v / max) * 100) : 0;
        const tip = measured
          ? `${h}:00 — качество ${pct(v)}`
          : `${h}:00 — нет данных`;
        cols.push(`<div class="bar-col ${isBest ? "is-best" : ""} ${measured ? "" : "is-empty"}"
             title="${tip}">
          <div class="bar" style="height:${height}%"></div>
          ${h % 3 === 0 || isBest ? `<div class="bar-label">${h}</div>` : `<div class="bar-label"></div>`}
        </div>`);
      }
      box.innerHTML = `<div class="bars">${cols.join("")}</div>`;
    }

    const angles = report.angles || {};
    const angleScore = (v) => v.quality ?? v.er;
    const topAngle = Object.entries(angles)
      .filter(([, v]) => typeof angleScore(v) === "number" && Number.isFinite(angleScore(v)))
      .sort((a, b) => angleScore(b[1]) - angleScore(a[1]))[0];
    const fleet = report.fleet || {};
    $("#report-breakdown").innerHTML = `
      ${kv("Постов за окно", n(fleet.posts))}
      ${kv("Просмотров", n(fleet.views_total))}
      ${kv("Вовлечений", n(fleet.engagement_total))}
      ${kv("Средний ER", rate(fleet.avg_er))}
      ${kv("Лучший угол", topAngle ? `${topAngle[0]} (${pct(angleScore(topAngle[1]))})` : "—")}
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
    const styleLine = Object.entries(styles).map(([k, v]) => `${k} ${rate(v.quality)}`).join(" · ");
    const parts = [];
    if (ho.n) parts.push(`holdout: ${n(ho.n)} постов, качество ${rate(ho.quality)} против ${rate(a.quality)} у основных`);
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


  function renderAgentBenchmark(data, trends = []) {
    const select = $("#agent-benchmark-cluster");
    const runButton = $("#agent-benchmark-run");
    if (!select) return;
    const ready = (trends || []).filter((item) => item.status === "ready" && item.id);
    const selected = state.agentBenchmarkCluster || select.value;
    select.innerHTML = ready.length
      ? ready.map((item) => '<option value="' + esc(item.id) + '">'
          + esc(item.label || item.id) + " · " + esc(item.niche || "общая тема")
          + " · " + n(item.items) + " источн.</option>").join("")
      : '<option value="">Нет готовых кластеров</option>';
    if (ready.some((item) => item.id === selected)) select.value = selected;
    else if (ready.length) select.value = ready[0].id;
    state.agentBenchmarkCluster = select.value || "";
    if (runButton) runButton.disabled = state.agentBenchmarkBusy || !ready.length;

    const valid = Number(data.valid_pairs) || 0;
    const total = Number(data.pairs_total) || 0;
    const labels = {
      insufficient_sample: "Недостаточно пар для вывода",
      positive_signal: "Положительный практический сигнал",
      negative_signal: "Отрицательный практический сигнал",
      inconclusive: "Результат неоднозначен",
    };
    const tone = data.conclusion === "positive_signal" ? "tag-ok"
      : data.conclusion === "negative_signal" ? "tag-warn" : "";
    const meta = $("#agent-benchmark-meta");
    if (meta) meta.textContent = total
      ? n(valid) + " валидных пар · " + n(total) + " запусков"
      : "Эксперименты ещё не запускались";

    const delta = typeof data.mean_quality_delta === "number" ? data.mean_quality_delta : NaN;
    const factDelta = typeof data.mean_verified_claim_ratio_delta === "number"
      ? data.mean_verified_claim_ratio_delta : NaN;
    const signed = (value) => (value > 0 ? "+" : "") + (value * 100).toFixed(1) + " п.п.";
    const summary = $("#agent-benchmark-summary");
    if (summary) {
      summary.innerHTML =
        '<div class="agent-benchmark-status"><span class="tag ' + tone + '">'
        + esc(labels[data.conclusion] || "Нет итогового сигнала")
        + '</span><span class="card-sub">' + n(valid) + " / "
        + n(data.minimum_pairs_for_signal || 10) + ' валидных пар для оценки</span></div>'
        + '<div class="agent-benchmark-stats">'
        + '<div><span>Победы стратега</span><strong>' + n(data.strategy_wins || 0) + '</strong></div>'
        + '<div><span>Победы контроля</span><strong>' + n(data.control_wins || 0) + '</strong></div>'
        + '<div><span>Ничьи</span><strong>' + n(data.ties || 0) + '</strong></div>'
        + '<div><span>Разница оценок</span><strong>' + (Number.isFinite(delta) ? esc(signed(delta)) : "—") + '</strong></div>'
        + '<div><span>Изменение фактологии</span><strong>' + (Number.isFinite(factDelta) ? esc(signed(factDelta)) : "—") + '</strong></div>'
        + '</div><p class="agent-benchmark-disclaimer">Практический сигнал не является статистическим доказательством. Денежная стоимость и токены провайдера в текущем журнале недоступны.</p>';
    }

    const history = $("#agent-benchmark-history");
    const recent = Array.isArray(data.recent_runs) ? data.recent_runs : [];
    if (history) {
      if (!recent.length) {
        history.innerHTML = '<div class="empty">Здесь появится история парных сравнений.</div>';
      } else {
        const statusLabel = (item) => item.status === "completed"
          ? (item.winner === "strategy" ? "стратег лучше" : item.winner === "control" ? "контроль лучше" : "ничья")
          : item.status === "inconclusive" ? "недостаточно данных" : "ошибка запуска";
        history.innerHTML = '<div class="agent-benchmark-history-title">Последние сравнения</div>'
          + recent.slice(0, 5).map((item) => {
            const runDelta = typeof item.quality_delta === "number" ? item.quality_delta : NaN;
            const detail = [
              item.channel_id,
              item.angle,
              item.format,
              "вызовов: " + n((item.calls || {}).control || 0) + " + " + n((item.calls || {}).strategy || 0),
            ].filter(Boolean).join(" · ");
            return '<div class="agent-benchmark-run"><div class="agent-benchmark-run-main">'
              + '<strong>' + esc(item.cluster_public_id || item.run_id || "эксперимент") + '</strong>'
              + '<span>' + esc(statusLabel(item)) + '</span>'
              + '<small>' + esc(detail) + '</small></div><div class="agent-benchmark-run-side">'
              + '<b>' + (Number.isFinite(runDelta) ? esc(signed(runDelta)) : "—") + '</b>'
              + '<small>' + esc(dt(item.started_at)) + '</small></div></div>';
          }).join("");
      }
    }
  }

  async function refreshAgentBenchmark() {
    try {
      const [report, trends] = await Promise.all([
        api("/api/agents/benchmark"),
        api("/api/trends?limit=40"),
      ]);
      renderAgentBenchmark(report || {}, trends.trends || []);
    } catch (error) {
      const note = $("#agent-benchmark-note");
      if (note) note.textContent = "Не удалось загрузить отчёт: " + error.message;
    }
  }

  async function runAgentBenchmark() {
    const cluster = $("#agent-benchmark-cluster")?.value || "";
    if (!cluster || state.agentBenchmarkBusy) return;
    const button = $("#agent-benchmark-run");
    const note = $("#agent-benchmark-note");
    state.agentBenchmarkBusy = true;
    if (button) {
      button.disabled = true;
      button.classList.add("is-busy");
    }
    if (note) note.textContent = "Сравнение запущено. Две версии одного материала проходят одинаковые проверки; публикации не выполняются.";
    try {
      const result = await post("/api/agents/benchmark", { cluster_public_id: cluster });
      if (result.status === "completed") toast("Парное сравнение завершено и сохранено.", "ok");
      else if (result.status === "inconclusive") toast("Сравнение завершено, но данных недостаточно для честного вывода.", "warn");
      else toast("Эксперимент не завершён: " + (result.failure_type || "ошибка"), "err");
      await refreshAgentBenchmark();
    } catch (error) {
      if (note) note.textContent = error.message;
      toast("Benchmark не запустился: " + error.message, "err");
    } finally {
      state.agentBenchmarkBusy = false;
      if (button) {
        button.classList.remove("is-busy");
        button.disabled = !($("#agent-benchmark-cluster")?.value);
      }
    }
  }

  // `tgfarm/pipeline.py` writes four values into the journal's `status` column,
  // which is a bare String(16) with no CHECK: `ok`, `error`, `timeout` and — since
  // `f038373` — `cancelled`. The old line here painted every non-`ok` red, so a
  // cancellation looked exactly like a crash, and a cancellation is the one
  // outcome the farm chose itself: the watchdog or the cycle limit stopped the
  // stage on purpose. It gets its own colour and its own word, because an operator
  // who reads "crash" goes looking for a defect that does not exist. An unknown
  // status keeps the red on purpose: a code the panel has not been told about must
  // not be quietly promoted to "nothing happened".
  const STAGE_STATUS = {
    ok: { cls: "stage-ok", word: null },
    cancelled: { cls: "stage-cancelled", word: "прерван" },
  };

  function renderPipeline(runs) {
    const targets = ["#pipeline-visual", "#team-pipeline"].map((selector) => $(selector)).filter(Boolean);
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
      targets.forEach((target) => { target.innerHTML = '<div class="empty">Прогонов ещё не было</div>'; });
      return;
    }
    const map = Object.fromEntries(stages.map((s) => [s.stage, s]));
    const markup = order.map((key) => {
      const stage = map[key];
      const status = stage ? (STAGE_STATUS[stage.status] || { cls: "stage-err", word: null }) : null;
      const cancelled = !!status && stage.status === "cancelled";
      const cls = !stage ? "" : status.cls;
      // A cancelled stage's error is the timer's cancellation reason, not a publish failure.
      const detail = stage ? (cancelled ? `прерван · ${esc((stage.error || "").slice(0, 40))}`
        : stage.status === "ok" ? `${items(stage.out, stage.counters_state)} · ${stage.ms} мс` : esc((stage.error || "").slice(0, 40))) : "не запускался";
      return `<div class="stage ${cls}"><span class="stage-dot"></span>
                <span><b>${names[key]}</b><br>${detail}</span></div>`;
    }).join("");
    targets.forEach((target) => { target.innerHTML = markup; });
  }
  // S2 stores the *shape* of interest per cluster: `trend_phase` is this window
  // against the window before it, `acceleration` is the relative change. Both
  // can be legitimately absent — `unknown` means the farm never observed a
  // preceding window, `null` means no ratio could be computed (an empty or a
  // missing baseline). Neither is a zero, so neither may render as one.
  const TREND_PHASE_RU = {
    rising: ["растёт", "tag-ok"],
    steady: ["ровно", "tag-acc"],
    spike: ["всплеск", "tag-warn"],
    decaying: ["затухает", ""],
    unknown: ["фаза не измерена", "tag-none"],
  };
  const PHASE_HINTS = {
    rising: "новых элементов заметно больше, чем в предыдущем окне",
    steady: "объём примерно тот же, что и в предыдущем окне",
    spike: "элементы появились только в этом окне — предыдущее было пустым",
    decaying: "элементов меньше, чем в предыдущем окне",
    unknown: "предыдущего окна наблюдений ещё не было: сравнивать не с чем, и это не ноль",
  };

  const trendPhaseChip = (phase) => {
    const key = String(phase ?? "").trim().toLowerCase();
    const known = TREND_PHASE_RU[key];
    // An unrecognised code is shown verbatim, never silently renamed: the panel
    // must not dress up a value it does not understand as a known one.
    const label = known ? known[0] : (key ? key : "фаза не измерена");
    const cls = known ? known[1] : "tag-none";
    const hint = known ? PHASE_HINTS[key] : "неизвестная фаза: код пришёл с сервера без описания";
    return `<span class="tag ${cls} trend-phase" title="${esc(hint)}">фаза: ${esc(label)}</span>`;
  };

  const trendAccelerationChip = (value) => {
    const measured = typeof value === "number" && Number.isFinite(value);
    if (!measured) {
      return '<span class="tag tag-none trend-accel" title="измерения нет: это не ноль">ускорение: нет данных</span>';
    }
    // 0 is a real measurement ("объём не изменился"), so it must print as 0 %.
    const percent = value * 100;
    const rounded = Math.abs(percent) < 0.5 && percent !== 0
      ? percent.toFixed(1)
      : String(Math.round(percent));
    const sign = percent > 0 ? "+" : "";
    return `<span class="tag ${percent > 0 ? "tag-ok" : percent < 0 ? "tag-err" : "tag-none"} trend-accel" title="окно к окну, по независимым документам">ускорение ${esc(sign + rounded)} %</span>`;
  };

  const trendShapeRow = (t) => `<div class="trend-shape">${trendPhaseChip(t.trend_phase)}${trendAccelerationChip(t.acceleration)}</div>`;

  function renderTrends(trends) {
    const html = trends.length ? trends.map((t) => `
      <div class="item">
        <div class="item-main">
          <div class="item-title">${esc(t.label || t.id)}</div>
          <div class="item-meta">${esc(t.niche || "—")} · ${t.items} материалов · ${t.outlets.length} изданий: ${esc((t.outlets || []).slice(0, 3).join(", "))}</div>
          ${trendShapeRow(t)}
          <div class="trend-explain trend-skips" data-trend-skips-body="${esc(t.id || t.public_id || "")}"></div>
        </div>
        <div class="item-side">
          <span class="tag ${t.status === "ready" ? "tag-ok" : ""}">${esc(t.status)}</span>
          <div class="heat"><i style="width:${Math.round((t.heat || 0) * 100)}%"></i></div>
          <div class="heat-val">${(t.heat || 0).toFixed(2)}</div>
          <button class="btn btn-ghost btn-sm" type="button" data-trend-explain="${esc(t.id || t.public_id || "")}">Почему?</button>
          <button class="btn btn-ghost btn-sm" type="button" data-trend-skips="${esc(t.id || t.public_id || "")}" title="Куда история не ушла и почему">Пропуски?</button>
        </div>
        <div class="trend-explain" data-trend-explain-body="${esc(t.id || t.public_id || "")}"></div>
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
  const PLATFORM_RU = { telegram: "Telegram", dzen: "Дзен", vk: "VK", max: "MAX", x: "X", youtube: "YouTube", tiktok: "TikTok", vk_clips: "VK Клипы" };

  // ── Провенанс поста: из какого решения плана этот пост ────────────────────
  // The question is one sentence — «этот пост из какого решения плана, и на какой
  // слот он был забронирован?» — and since W19-B the post row itself answers it:
  // `posts.plan_entry_id` names the `editorial_plan_entries` row that ordered the
  // post, and `posts.plan_slot_at` is the booking that entry made, which survives
  // every later overwrite of `scheduled_at` by a requeue, a retry or an unpublish.
  // `_post_view` serves both, plus `plan_public_id`: the snapshot that owns the
  // entry, resolved by the server or not at all.
  //
  // Four answers, four wordings, because three of them are different silences:
  // a planned post whose plan is still readable; a post produced while no plan was
  // in play (`plan_entry_id: null`, the state every row on the live base is in
  // today); a post that names an entry that is gone (the reference is a measured
  // fact, the answer is unread); and this block's own state for a read that did
  // not send the fields at all. None of them is a zero, and a null slot is not a
  // date — so the slot goes through `nextRunCell`, the helper the plan-execution
  // block wrote to keep a booked moment away from `dt()`, which answers «только
  // что» for every future instant because its elapsed time is negative.
  const PROVENANCE_UNKNOWN = "неизвестно";
  const PROVENANCE_FIELDS = ["plan_entry_id", "plan_slot_at", "plan_public_id"];
  const hasProvenanceFields = (p) => !!p && typeof p === "object"
    && PROVENANCE_FIELDS.every((key) => key in p);
  const planSlotText = (v) => (typeof v === "string" && v
    ? `слот ${nextRunCell(v)}` : "слот не записан");

  function postProvenance(p) {
    if (!hasProvenanceFields(p)) return `провенанс: ${esc(PROVENANCE_UNKNOWN)}`;
    if (p.plan_entry_id === null) return "создан без плана";
    const entry = `запись плана ${esc(String(p.plan_entry_id))}`;
    const slot = esc(planSlotText(p.plan_slot_at));
    if (typeof p.plan_public_id !== "string" || !p.plan_public_id) {
      return `${entry} не читается · ${slot}`;
    }
    return `по плану ${esc(p.plan_public_id)} · ${entry} · ${slot}`;
  }

  const postProvenanceChip = (p) => `<span class="plan-chip">${postProvenance(p)}</span>`;

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
            ${postProvenanceChip(p)}
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
          ${postProvenanceChip(p)}
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
          ${postProvenanceChip(p)}
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

  // «Ближайшие публикации» renders the newest 40 posts, newest first. On the live
  // base (2026-11-05, read-only connection) all 51 rows in `publish_unknown` were
  // created 2026-10-29 and ranked 7794…9368 newest-first out of 21124 posts, so
  // not one of them could ever appear in the list above — while the queue card
  // printed their count. The count was the proof and the names were missing.
  // `/api/backlog` now sends the ten oldest with the aggregate that says how many
  // exist in total, and this block shows both: a truncated list presented as the
  // whole set is the failure this wording exists to prevent.
  // Nothing here decides a publication outcome. Both buttons call
  // `window.tgfarmReconcile` — the very function the post drawer uses, with its
  // `confirm()` gate — which POSTs to `/api/posts/{public_id}/reconcile` (§80:
  // an existing route behind an existing consent, never a new write path).
  function renderUnresolvedWorklist(payload, failure) {
    const root = $("#overview-unresolved");
    if (!root) return;
    // Three silences, three wordings, and none of them may print a zero: the
    // network died, the queue read failed server-side, the worklist itself never
    // arrived. `total: 0` below is the fourth case — a measured nothing.
    if (failure) {
      const message = String(failure.message || failure || "");
      const httpError = /\s->\s\d/.test(message);
      root.innerHTML = '<div class="alert alert-err">'
        + (httpError
          ? "<b>Список неподтверждённых публикаций не прочитан:</b> "
          : "<b>Сеть не отвечает:</b> список неподтверждённых публикаций не получен — ")
        + esc(message) + "</div>";
      return;
    }
    const data = payload || {};
    if (data.error) {
      root.innerHTML = '<div class="alert alert-err"><b>Чтение очереди не удалось:</b> '
        + esc(data.error) + "</div>"
        + '<div class="empty">Список не прочитан — это не «неподтверждённых нет».</div>';
      return;
    }
    const block = data.unresolved;
    if (!block) {
      root.innerHTML = '<div class="empty">Список неподтверждённых публикаций не пришёл '
        + "в ответе — он не прочитан, а не пуст.</div>";
      return;
    }
    const items = Array.isArray(block.items) ? block.items : [];
    const total = typeof block.total === "number" ? block.total : null;
    const shown = typeof block.shown === "number" ? block.shown : items.length;
    const limit = typeof block.limit === "number" ? block.limit : null;
    let denominator;
    if (total === null) {
      denominator = "сколько их всего — не прочитано";
    } else if (total === 0) {
      denominator = "неподтверждённых публикаций нет";
    } else if (shown >= total) {
      denominator = `показаны все ${n(total)} — ждать сверки больше некому`;
    } else {
      denominator = `показаны ${n(shown)} из ${n(total)} самых старых`
        + (limit === null ? "" : ` · карточка называет не больше ${n(limit)} строк за раз`);
    }
    const head = '<div class="unresolved-head"><div class="item-title">'
      + "Не подтверждено, ушло ли</div>"
      + '<div class="item-meta">' + esc(denominator)
      + " · полный список и причина каждой строки — на вкладке «Сверка»</div></div>";
    if (!items.length) {
      // An empty list is three different sentences depending on what the
      // denominator says, and only one of them is «всё чисто».
      const note = total === null
        ? "ни списка, ни знаменателя: чтение очереди не завершилось"
        : (total === 0
          ? "ни одной публикации в этом состоянии нет — это измерено, а не отсутствие ответа"
          : `строк в списке нет, а ждут сверки ${n(total)} — знаменатель выше честнее списка`);
      root.innerHTML = head + '<div class="empty">' + esc(note) + "</div>";
      return;
    }
    root.innerHTML = head + items.map((row) => {
      const platform = row.platform || "unknown";
      const channel = row.channel_title || row.channel_id || "канал не найден";
      const age = typeof row.age_hours === "number"
        ? `ждёт сверки ${hrs(row.age_hours)}`
        : "ждёт сверки: возраст не прочитан";
      const attempts = typeof row.attempts === "number"
        ? ` · попыток ${n(row.attempts)}`
        : " · попыток: нет отчёта";
      return '<div class="item is-unresolved"><div class="item-main">'
        + '<div class="item-title">' + esc(row.hook || "без заголовка") + "</div>"
        + '<div class="item-meta">' + esc(channel) + " · "
        + esc(PLATFORM_RU[platform] || platform) + " · " + esc(age) + esc(attempts)
        + " · " + esc(dt(row.created_at)) + "</div>"
        + '<div class="item-meta is-unresolved-id">' + esc(row.public_id) + "</div>"
        + (row.error
          ? '<div class="item-meta is-unresolved-error">' + esc(row.error) + "</div>"
          : "")
        + '</div><div class="item-side unresolved-actions">'
        + '<button class="btn btn-primary btn-sm" type="button"'
        + ' data-unresolved-action="confirm_published"'
        + ' data-unresolved-id="' + esc(row.public_id) + '">Да, пост уже есть</button>'
        + '<button class="btn btn-ghost btn-sm" type="button"'
        + ' data-unresolved-action="retry_after_check"'
        + ' data-unresolved-id="' + esc(row.public_id) + '">Проверил: поста нет</button>'
        + "</div></div>";
    }).join("");
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

  // ── План тем (глобальный редакционный план) ─────────────────────────────
  async function loadEditorialPlan(force = false) {
    if (force) state.planLoaded = false;
    // Read independently of the plan itself: whether the consumer runs does not
    // depend on this process being able to serialize the plan.
    loadPlanExecution();
    // The same argument in the other direction: booking fidelity is a fact about
    // stored rows, not about the plan snapshot this call is building, and it keeps
    // its own request so a failed one cannot be reported as the other.
    loadBookingFidelity();
    // And the third independent read: which topics the plan has already ordered a
    // draft for but has not booked a slot to yet. `/api/editorial` answers it out of
    // `editorial.queue`, so this is neither the consumer's state nor the snapshot's
    // — it is the queue's own answer, and it keeps its own request so that a dead
    // one cannot be reported as the other.
    loadOpenOrderQueue();
    try {
      const [plan, history] = await Promise.all([
        api("/api/editorial/plan"),
        api("/api/editorial/plan/history?limit=10"),
      ]);
      renderEditorialPlan(plan || {}, (history && history.plans) || []);
      state.planLoaded = true;
    } catch (err) {
      const meta = $("#plan-meta");
      if (meta) meta.textContent = "План недоступен: " + (err && err.message ? err.message : String(err));
    }
  }

  function renderEditorialPlan(plan, history) {
    const meta = $("#plan-meta");
    const chips = $("#plan-summary");
    const rows = $("#plan-entries");
    const hist = $("#plan-history");
    if (!meta || !rows) return;

    const summary = plan.summary || {};
    const stateLabel = plan.status === "transient"
      ? "предпросмотр — «Зафиксировать план» сделает его активным"
      : "активный";
    const created = plan.created_at ? new Date(plan.created_at).toLocaleString() : "—";
    meta.textContent = `Статус: ${stateLabel}; горизонт ${plan.horizon_days ?? "—"} дн.; сформирован ${created}`;
    // The server writes the denominator sentence (and the reason there is no
    // denominator) — the panel repeats it instead of inventing its own.
    if (summary.coverage_note) meta.textContent += `; ${summary.coverage_note}`;

    const diff = summary.diff || {};
    // Coverage is a denominator, not a score. `measured: 0` means the plan could
    // not be checked at all — a preview, no stored rows, or a failed read — and
    // the server already says which in `coverage_note`. Rendering zero chips there
    // would turn "nothing was measured" into "nothing was done", the inversion
    // this panel has been corrected for twice already.
    const cov = summary.coverage || null;
    const coverageChips = !cov || !(Number(cov.measured || 0) > 0)
      ? []
      : [
        ["Исполнено", cov.materialized ?? 0],
        ["Не дошло", cov.abandoned ?? 0],
        ["В горизонте", cov.not_yet ?? 0],
        ["Без ответа", cov.unknown ?? 0],
      ];
    const chipItems = [
      ["Всего тем", summary.total ?? (plan.entries || []).length],
      ["В работу", summary.produce ?? 0],
      ["В расписание", summary.schedule ?? 0],
      ["Отложено", summary.hold ?? 0],
      ...(diff.added && diff.added.length ? [["Добавлено тем", diff.added.length]] : []),
      ...(diff.removed && diff.removed.length ? [["Снято тем", diff.removed.length]] : []),
      ...Object.entries(summary.by_platform || {}).map(([p, n]) => [p, n]),
      ...coverageChips,
    ];
    chips.innerHTML = chipItems
      .map(([label, value]) => `<span class="plan-chip">${esc(String(label))}: ${esc(String(value))}</span>`)
      .join("");

    const entries = plan.entries || [];
    // A plan decision has four honest endings. "без ответа" is not a failure: it
    // is the server saying it could not tell (no post row, no stored plan, a read
    // that errored), and it must never be folded into "не дошло".
    const OUTCOME_LABELS = {
      materialized: "исполнено",
      abandoned: "не дошло",
      not_yet: "в горизонте",
      unknown: "без ответа",
    };
    // A `schedule` decision with no slot is not "по готовности": the builder
    // chose the action and then found no minute to book, and the consumer holds
    // the entry under that cause. The builder already names which of the two it
    // was (`payload.no_slot_reason`), and they ask the operator for opposite
    // things — «поставить позже» versus «настроить часы канала» — so printing
    // one phrase for both, or the friendlier "по готовности", sends him to the
    // wrong lever. An unrecognised cause keeps its verbatim code: a code nobody
    // agreed on must not borrow a label we do understand.
    const NO_SLOT_LABELS = {
      horizon_occupied: "слот не забронирован: горизонт занят — освободится позже",
      no_legal_hours: "слот не забронирован: у канала нет легальных часов публикации",
    };
    const slotCell = (e) => {
      if (e.slot_at) return new Date(e.slot_at).toLocaleString();
      if (e.action === "schedule") {
        const cause = e.payload && typeof e.payload === "object" ? e.payload.no_slot_reason : null;
        if (typeof cause === "string" && cause) {
          return NO_SLOT_LABELS[cause] || `слот не забронирован: ${cause}`;
        }
        // Written before the cause existed, or written by a path that had none.
        return "слот не забронирован — причина не записана";
      }
      return "по готовности";
    };
    rows.innerHTML = entries.length
      ? `<ul class="plan-entries">${entries.map((e) => {
          const slot = slotCell(e);
          const reasons = (e.reasons || []).filter(Boolean);
          const channel = e.channel_title || (e.channel && e.channel.title) || e.channel_id || "—";
          const state = e.outcome && e.outcome.state ? String(e.outcome.state) : "";
          const outcome = state
            ? `<span class="plan-chip">исход: ${esc(OUTCOME_LABELS[state] || state)}</span>`
            : "";
          return `<li class="plan-entry${e.action === "hold" ? " plan-hold" : ""}">
            <div class="plan-entry-head">
              <span class="plan-chip">P${esc(String(e.priority ?? "-"))}</span>
              <span class="plan-chip">${esc(String(e.action || "—"))}</span>
              ${outcome}
              <span>${esc(String(e.title || e.cluster_public_id || "—"))}</span>
            </div>
            <div class="plan-entry-reasons">${esc(String(e.platform || "—"))} · ${esc(String(channel))} · ${esc(String(e.format || "—"))} · слот ${esc(slot)}</div>
            ${reasons.length ? `<ul class="plan-entry-reasons">${reasons.map((r) => `<li>${esc(String(r))}</li>`).join("")}</ul>` : ""}
          </li>`;
        }).join("")}</ul>`
      : `<p class="plan-entry-reasons">Материалов нет — запустите сбор тем или обновите план.</p>`;

    hist.innerHTML = history.length
      ? `<ul class="plan-history">${history.map((h) => {
          const s = h.summary || {};
          const when = h.created_at ? new Date(h.created_at).toLocaleString() : "—";
          return `<li>${esc(String(h.public_id || ""))} — ${esc(when)}: ${esc(String(s.total ?? 0))} тем (в работу ${esc(String(s.produce ?? 0))}, отложено ${esc(String(s.hold ?? 0))})</li>`;
        }).join("")}</ul>`
      : `<p class="plan-entry-reasons">Зафиксированных планов пока нет.</p>`;
  }

  // ── Исполнитель плана: работает ли потребитель и что он уже сделал ───────
  const EXECUTION_UNKNOWN = "неизвестно";
  // Three different silences must keep three different wordings. `registered:
  // null` means the API process is blind to the scheduler, `next_run_at: null`
  // means no moment is planned, a missing `last_apply` means the consumer never
  // ran. None of them is a zero, and none of them may borrow another's words.
  const registeredCell = (v) => (v === true ? "зарегистрировано"
    : v === false ? "не зарегистрировано" : EXECUTION_UNKNOWN);
  // `dt()` answers "how long ago", and every future instant has a negative elapsed
  // time — so it answers "только что" for a run planned for tonight as readily as
  // for one that just happened. A job that has not run yet is a moment on the
  // clock, not an elapsed time, so it gets a scheduled label; a null is still "—".
  const nextRunCell = (v) => {
    if (typeof v !== "string" || !v) return "—";
    const at = Date.parse(v);
    if (Number.isNaN(at)) return "—";
    if (at <= Date.now()) return dt(v);
    const when = new Date(at);
    const sameDay = when.toDateString() === new Date().toDateString();
    const date = when.toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
    return sameDay ? `в ${clock(v)}` : `${date} ${clock(v)}`;
  };
  const execCount = (v) => (typeof v === "number" && Number.isFinite(v) ? n(v) : EXECUTION_UNKNOWN);
  const execChip = (label, value) => `<span class="plan-chip">${esc(label)}: ${esc(value)}</span>`;

  // The executor's refusal vocabulary (`tgfarm/plan_execution.py::REASON_*`),
  // written out once so the operator reads a cause and not a token. Only the two
  // codes that used to share one name get a sentence that tells them apart: the
  // builder holds a `schedule` entry under `horizon_occupied` when a later minute
  // exists and under `no_legal_hours` when none ever will, and «поставить позже»
  // and «настроить часы канала» are different jobs for the reader. Every other
  // code keeps its own short label; an absent code prints verbatim.
  const EXEC_REASON = {
    plan_execution_disabled: "исполнитель выключен флагом",
    no_active_plan: "активного плана нет",
    plan_action_hold: "решение плана — удержать",
    produce_entry_not_generated_here: "материал готовит другая стадия",
    unknown_action: "неизвестное действие в записи плана",
    gated_post_missing: "черновика под записью нет",
    already_scheduled: "уже запланировано",
    post_not_gated: "пост не в состоянии ожидания",
    channel_missing: "канал записи не найден",
    channel_disabled: "канал выключен",
    capability_not_publishable: "площадка не умеет публиковать сама",
    channel_daily_cap: "дневной лимит канала исчерпан",
    platform_daily_cap: "дневной лимит площадки исчерпан",
    ai_daily_budget_exhausted: "дневной бюджет AI исчерпан",
    horizon_occupied: "горизонт занят: слот освободится позже",
    no_legal_hours: "у канала нет легальных часов публикации",
    no_slot_planned: "слот не забронирован, причина не записана",
    slot_not_due: "срок брони ещё не наступил",
    slot_outside_local_hours: "слот вне локальных часов канала",
    slot_booking_rebounded: "слот плана исправлен на ближайший легальный",
    handover_refused: "передача в очередь отказана",
  };

  function execReasonChips(reasons) {
    return Object.entries(reasons).map(([code, count]) => {
      const label = EXEC_REASON[code];
      // The code stays beside the sentence: it is what the operator greps for in
      // the journal, and an unknown code must still be readable as itself.
      return execChip(label ? label + " · " + code : code, execCount(count));
    });
  }

  function execJobChips(jobs) {
    return jobs.map((raw) => {
      const job = raw || {};
      const id = String(job.id || "—");
      return `<span class="plan-chip">${esc(id)}: ${esc(registeredCell(job.registered))}</span>`
        + `<span class="plan-chip">${esc(id)} · далее ${esc(nextRunCell(job.next_run_at))}</span>`;
    });
  }

  function execLastApplyBlock(la) {
    if (!la || typeof la !== "object") {
      return `<p class="plan-entry-reasons">ни одного пропуска: исполнитель ещё ни разу не запускался.</p>`;
    }
    const chips = [
      execChip("пропуск", la.at ? dt(la.at) : EXECUTION_UNKNOWN),
      execChip("по плану", la.plan_id ? String(la.plan_id) : EXECUTION_UNKNOWN),
      execChip("исполнено", execCount(la.applied)),
      execChip("удержано", execCount(la.held)),
    ];
    const hasReasons = la.reasons && typeof la.reasons === "object";
    const reasons = hasReasons ? execReasonChips(la.reasons) : [];
    const reasonLine = !hasReasons
      ? `<p class="plan-entry-reasons">причины не записаны</p>`
      : reasons.length
        ? `<p class="plan-entry-reasons">итоги по причинам:</p><div class="plan-chips">${reasons.join("")}</div>`
        : `<p class="plan-entry-reasons">ни одной причины в отчёте</p>`;
    return `<div class="plan-chips">${chips.join("")}</div>${reasonLine}`;
  }

  function execPlanBlock(pl) {
    if (!pl || typeof pl !== "object") return "";
    const facts = [
      execChip("действующий план", pl.id ? String(pl.id) : EXECUTION_UNKNOWN),
      execChip("составлен", pl.created_at ? dt(pl.created_at) : EXECUTION_UNKNOWN),
      execChip("всего тем", execCount(pl.total)),
      execChip("в работу", execCount(pl.produce)),
      execChip("в расписание", execCount(pl.schedule)),
      execChip("отложено", execCount(pl.hold)),
    ];
    const coverage = pl.coverage && typeof pl.coverage === "object"
      ? Object.entries(pl.coverage).map(([key, value]) => execChip(String(key), execCount(value)))
      : [];
    return `<div class="plan-chips">${facts.concat(coverage).join("")}</div>`;
  }

  function renderPlanExecution(payload, failure) {
    const root = $("#plan-execution");
    if (!root) return;
    const data = payload && typeof payload === "object" ? payload : null;

    if (!data) {
      const reason = !failure ? "ответ не пришёл в запросе"
        : typeof failure === "string" ? failure
          : String((failure && failure.message) || failure);
      root.innerHTML = `<div class="plan-chips">${execChip("потребитель плана", EXECUTION_UNKNOWN)}</div>`
        + `<p class="plan-entry-reasons">Отчёт исполнителя не прочитан: ${esc(reason)}. `
        + "Это не «выключен» и не «ничего не исполнено» — состояние потребителя неизвестно.</p>";
      return;
    }

    const enabledChip = data.enabled === true ? `<span class="tag tag-ok">включён</span>`
      : data.enabled === false ? `<span class="tag tag-warn">выключен</span>`
        : `<span class="tag tag-none">${esc(EXECUTION_UNKNOWN)}</span>`;
    const head = `<div class="plan-chips"><span class="tag">потребитель плана</span>${enabledChip}</div>`;
    // The sentence is about what the flag does, not about how to flip it: this
    // panel reads a configured setting and never changes one.
    const offLine = data.enabled === false
      ? `<p class="plan-entry-reasons">Флаг выключен — задачи исполнителя не регистрируются, `
        + "ни одна запись плана не двигается, и числа ниже останутся пустыми, пока "
        + "PLAN_EXECUTION_ENABLED не включат и процесс не перезапустят.</p>"
      : "";
    const jobs = Array.isArray(data.jobs) ? data.jobs : [];
    const jobsBlock = jobs.length
      ? `<div class="plan-chips">${execJobChips(jobs).join("")}</div>`
      : `<p class="plan-entry-reasons">список задач не прочитан</p>`;

    // An absent field is not a null field: `last_apply: null` is the server
    // saying the executor never ran, a payload without the key says nothing.
    const applyBlock = "last_apply" in data
      ? execLastApplyBlock(data.last_apply)
      : `<p class="plan-entry-reasons">отчёт о пропуске не прочитан</p>`;
    const noteLine = typeof data.note === "string" && data.note
      ? `<p class="plan-entry-reasons">${esc(data.note)}</p>` : "";

    root.innerHTML = head + offLine + jobsBlock + applyBlock
      + execPlanBlock(data.plan) + noteLine;
  }

  async function loadPlanExecution() {
    const root = $("#plan-execution");
    if (!root) return;
    try {
      renderPlanExecution(await api("/api/editorial/plan/execution"), null);
    } catch (error) {
      renderPlanExecution(null, error);
    }
  }

  // ── Бронь плана против факта: попал ли пост в зарезервированный слот ──────
  // The question this card exists to answer is «план забронировал слот — ферма
  // опубликовала в него?». `posts.plan_slot_at` is the reservation, written once
  // at insert; `posts.scheduled_at` is the slot the queue ended up using and is
  // rewritten by every requeue, retry and unpublish; `posts.published_at` is what
  // the platform actually took. `_post_view` has shown the reservation since
  // W19-C and the queue has always shown the slot — the subtraction was left to
  // the operator, so nobody could say whether the plan's schedule means anything.
  // `/api/editorial/plan/booking` does it server-side and this card only words it.
  //
  // Six wordings, six facts, and no borrowing between them: «брони нет» is not a
  // zero, «уважено» is not the absence of a shift, «отказано» is not «заблокировано»
  // in general but the one named gate the scheduler registers, «ждёт» says nothing
  // about the future, and «не прочитано» says the read did not happen — which is
  // what the live store answers today, because its `posts` table has no booking
  // column yet. A failed request is a seventh case and its own sentence: the
  // server answering "nothing measured" and the server not answering are not one
  // state, and this panel must never print them with the same words (P34).
  const BOOKING_UNREAD = "не прочитано";
  const BOOKING_STATE_WORDS = {
    no_booking: "брони нет",
    honoured: "забронировано и уважено",
    shifted: "забронировано и сдвинуто",
    rebound: "забронировано и исправлено по правилу локальных часов",
    refused: "забронировано и отказано",
    awaiting_slot: "забронировано, слот ещё не выдан",
    unread_row: "бронь не читается",
  };
  const BOOKING_STATE_ORDER = [
    "no_booking", "honoured", "shifted", "rebound", "refused", "awaiting_slot", "unread_row",
  ];
  const BOOKING_DIRECTION_WORDS = { later: "позже брони", earlier: "раньше брони" };
  const bookingCount = (v) => (typeof v === "number" && Number.isFinite(v) ? n(v) : BOOKING_UNREAD);
  // `index.html` is outside this file's reach, so the card adds its own mount next
  // to the consumer block instead of writing into `#plan-execution`, whose
  // innerHTML the consumer render owns and replaces on every load.
  function bookingMount() {
    const existing = $("#plan-booking");
    if (existing) return existing;
    const anchor = $("#plan-execution");
    if (!anchor || !anchor.parentNode) return null;
    const node = document.createElement("div");
    node.id = "plan-booking";
    anchor.parentNode.insertBefore(node, anchor.nextSibling);
    return node;
  }

  // A signed displacement, spelled with its sign and its direction, in the units
  // that were measured. Seconds under a minute are printed instead of rounded
  // away: a 30-second shift the server measured may not read as "nothing moved",
  // and a bare `0 мин` is exactly the fabricated zero this project refuses.
  function bookingDeltaText(seconds) {
    if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds === 0) return "";
    const abs = Math.abs(Math.trunc(seconds));
    const hours = Math.floor(abs / 3600);
    const minutes = Math.floor((abs % 3600) / 60);
    const rest = abs % 60;
    const parts = [];
    if (hours) parts.push(`${hours} ч`);
    if (minutes) parts.push(`${minutes} мин`);
    if (rest || !parts.length) parts.push(`${rest} с`);
    const sign = seconds > 0 ? "+" : "−";
    const words = seconds > 0 ? BOOKING_DIRECTION_WORDS.later : BOOKING_DIRECTION_WORDS.earlier;
    return `${sign}${parts.join(" ")} ${words}`;
  }

  // A measured zero and an unmeasured distance are two different sentences.
  // `shift_seconds: 0` is the reading of a booking against a slot that matched it;
  // printing "не измерен" there would deny a measurement the server made, and
  // printing the empty string `bookingDeltaText(0)` returns would look like a
  // missing field. Both errors are the same one P34 names, only inverted.
  function bookingShiftWords(seconds, zeroWords) {
    if (typeof seconds !== "number" || !Number.isFinite(seconds)) return null;
    return seconds === 0 ? zeroWords : bookingDeltaText(seconds);
  }

  function bookingStateChip(code, state) {
    const view = state && typeof state === "object" ? state : {};
    const word = BOOKING_STATE_WORDS[code] || `состояние «${String(code)}» не описано`;
    const parts = [`${word}: ${bookingCount(view.count)}`];
    // One row is an observation, not a finding, and the card says so while it
    // still prints the number.
    if (view.single_row === true) parts.push("одна строка — не вывод");
    if (view.basis === "sample") parts.push("по прочитанному окну");
    return `<span class="plan-chip">${esc(parts.join(" · "))}</span>`;
  }

  // A stamp has three readings and they are not interchangeable: the key is absent
  // (this server sent no such field — nothing was read), the key is null (read, and
  // there is nothing there), the key names a moment. Calling a null «не прочитано»
  // reports a broken column where the column is fine and simply empty, which is the
  // same invention in the other direction as printing a fabricated date for it.
  function bookingStampWords(value, absentWords, emptyWords) {
    if (value === undefined) return absentWords;
    if (typeof value !== "string" || !value) return emptyWords;
    return nextRunCell(value);
  }

  function bookingRowLine(row) {
    const r = row && typeof row === "object" ? row : {};
    const code = typeof r.state === "string" ? r.state : "";
    const word = BOOKING_STATE_WORDS[code] || `${BOOKING_UNREAD} (${code || "состояние не прислано"})`;
    const head = `<div class="plan-entry-head">`
      + `<span class="plan-chip">${esc(String(r.post_id || "—"))}</span>`
      + `<span class="plan-chip">${esc(word)}</span>`
      + `</div>`;
    const booked = `бронь: ${esc(bookingStampWords(r.plan_slot_at, "поле не прислано", BOOKING_UNREAD))}`;
    const used = `слот: ${esc(bookingStampWords(r.scheduled_at, "поле не прислано", "ещё не выдан"))}`;
    const shift = bookingShiftWords(r.shift_seconds, "0 с — слот совпал с бронью");
    const sentShift = bookingShiftWords(r.published_shift_seconds, "ровно по брони");
    const sentStamp = typeof r.published_at === "string" && r.published_at
      ? nextRunCell(r.published_at) : null;
    const sent = sentStamp
      ? `отправлен ${esc(sentStamp)}${sentShift ? " · " + esc(sentShift) : " · дельта к брони не измерена"}`
      : "published_at" in r
        ? "не отправлен"
        : "отправка: поле не прислано";
    const details = [booked, used, shift ? `сдвиг ${esc(shift)}` : "сдвиг не измерен", sent];
    // The refusal keeps the gate's own code in the sentence: «заблокировано» would
    // be a second, vaguer word for a fact the ledger already names exactly.
    if (code === "refused") {
      const gateCode = typeof r.refusal_gate_code === "string" && r.refusal_gate_code
        ? r.refusal_gate_code : null;
      details.push(gateCode
        ? `отказ по гейту ${esc(String(gateCode))} — счётчик журнала принадлежит тику, не посту`
        : "код гейта не прислан — называть отказ нечем");
    }
    // A shifted booking has a second, stronger reading: was the move repaired by
    // the rebind journal, and which tick did it. The four statuses are four facts
    // — «совпало с журналом» is not «в журнале нет», and «журнал не прочитан до
    // конца» is not «не найдено» — so each gets its own sentence, and a row with
    // no `rebind` object at all (the flag is off) prints nothing rather than a
    // verdict about a journal nobody consulted.
    const rebind = r.rebind && typeof r.rebind === "object" ? r.rebind : null;
    if (rebind) {
      const status = typeof rebind.status === "string" ? rebind.status : "";
      const journal = typeof rebind.journal_id === "number" || typeof rebind.journal_id === "string"
        ? String(rebind.journal_id) : "";
      const source = typeof rebind.source === "string" && rebind.source ? rebind.source : "";
      if (status === "matched") {
        const provenance = [
          journal ? `журнал #${esc(journal)}` : "",
          source ? `источник: ${esc(source)}` : "",
        ].filter(Boolean).join(", ");
        details.push("сдвиг исправлен правилом локальных часов"
          + (provenance ? ` (${provenance})` : " (журнал не назвал ни записи, ни источника)"));
      } else if (status === "incomplete") {
        details.push("сдвиг не объяснён: журнал прочитан не до конца, «не найдено» утверждать нельзя");
      } else if (status === "unread") {
        details.push("журнал перепривязок не прочитан — исправление не измерено");
      } else if (status === "not_found") {
        details.push("в журнале нет записи об исправлении этого сдвига");
      }
    }
    return `<li class="plan-entry">${head}<div class="plan-entry-reasons">${details.join(" · ")}</div></li>`;
  }

  function bookingTransportText(failure) {
    const reason = !failure ? "ответ не пришёл в запросе"
      : typeof failure === "string" ? failure
        : String((failure && failure.message) || failure);
    // «не прочитано» is reserved for a server that answered and could not measure.
    // A request that never landed says «ответ не пришёл», so the two silences can
    // never be read as one.
    return `<div class="plan-chips"><span class="plan-chip">${esc("бронь: ответ не пришёл")}</span></div>`
      + `<p class="plan-entry-reasons">Запрос состояния брони не удался: ${esc(reason)}. `
      + "Это не «брони нет», не «совпало» и не «сервер ответил пусто» — ответа не было.</p>";
  }

  function renderBookingFidelity(data, failure) {
    const root = bookingMount();
    if (!root) return;
    if (!data || typeof data !== "object") {
      root.innerHTML = bookingTransportText(failure);
      return;
    }
    const head = `<h3>Бронь плана против факта</h3>`;
    // The server could not read the table at all. Every count under it stays
    // «не прочитано»: printing zeros here would print outcomes nobody measured.
    if (data.readable !== true) {
      const reason = typeof data.reason === "string" && data.reason ? data.reason : BOOKING_UNREAD;
      root.innerHTML = head
        + `<div class="plan-chips"><span class="plan-chip">${esc(`бронь: ${BOOKING_UNREAD}`)}</span>`
        + `<span class="plan-chip">причина: ${esc(reason)}</span></div>`
        + `<p class="plan-entry-reasons">${esc(typeof data.note === "string" && data.note
          ? data.note
          : "Состояние брони не измерено.")}</p>`;
      return;
    }
    const population = data.population && typeof data.population === "object"
      ? data.population : {};
    const gate = data.gate && typeof data.gate === "object" ? data.gate : {};
    const stats = data.states && typeof data.states === "object" ? data.states : {};
    const booked = typeof population.booked === "number" ? population.booked : null;
    const chips = [
      `<span class="plan-chip">${esc(`постов с бронью: ${bookingCount(population.booked)}`)}</span>`,
      `<span class="plan-chip">${esc(`без брони: ${bookingCount(population.unbooked)}`)}</span>`,
      `<span class="plan-chip">${esc(`прочитано строк: ${bookingCount(population.scanned)}`)}</span>`,
    ];
    if (population.truncated === true) {
      chips.push(`<span class="plan-chip">${esc(`окно ограничено: ${bookingCount(population.limit)} строк`)}</span>`);
    }
    // The server decides which states this card may word: a state it did not
    // send (the rebind state while the flag is off) is not printed as unread.
    const stateChips = BOOKING_STATE_ORDER
      .filter((code) => stats[code] !== undefined)
      .map((code) => bookingStateChip(code, stats[code]));
    // With no booking in the table the four booked states are vacuous, and
    // printing their measured zeros would look exactly like a verdict about them.
    const stateLine = booked === 0
      ? `<p class="plan-entry-reasons">Бронированных постов нет — состояния «уважено / сдвинуто / `
        + "исправлено / отказано / ждёт» не о чём спрашивать, и нули вместо них не печатаются.</p>"
      : `<div class="plan-chips">${stateChips.join("")}</div>`;
    const rows = Array.isArray(data.samples) ? data.samples : [];
    const rowsBlock = rows.length
      ? `<ul class="plan-entries">${rows.map(bookingRowLine).join("")}</ul>`
      : `<p class="plan-entry-reasons">Ни одной строки с бронью читатель не назвал.</p>`;
    const gateCount = bookingCount(gate.count);
    const gateLine = `<p class="plan-entry-reasons">Отказы: гейт ${esc(String(gate.code || BOOKING_UNREAD))} — `
      + `${esc(gateCount)}`
      + (gate.count === null || gate.count === undefined
        ? " (ни один publish-тик этот счётчик ещё не записывал)"
        : " (счётчик последнего publish-тика, не отдельного поста)")
      + ".</p>";
    root.innerHTML = head + `<div class="plan-chips">${chips.join("")}</div>` + stateLine
      + gateLine + rowsBlock
      + `<p class="plan-entry-reasons">${esc(typeof data.note === "string" ? data.note : "")}</p>`;
  }

  async function loadBookingFidelity() {
    const root = bookingMount();
    if (!root) return;
    try {
      renderBookingFidelity(await api("/api/editorial/plan/booking"), null);
    } catch (error) {
      renderBookingFidelity(null, error);
    }
  }

  function renderDistributionPlan(payload) {
    const plan = payload || {};
    const mode = plan.mode || "dry";
    const modeNames = { off: "OFF · остановлен", dry: "DRY · без отправки", live: "LIVE · автопостинг" };
    const stateLabel = $("#distribution-mode-state");
    const notice = $("#distribution-notice");
    if (stateLabel) stateLabel.textContent = modeNames[mode] || mode;
    if (notice) {
      notice.textContent = mode === "off"
        ? "Автопубликация остановлена глобальным переключателем. Площадки и очереди сохранены."
        : mode === "dry"
          ? `Пробный режим: выбранные материалы и лимиты видны ниже, реальные посты не отправляются. Сейчас в плане ${n(plan.selected_total || 0)} доставок.`
          : `LIVE включён: ферма отправляет только выбранные материалы в подключённые и включённые автоматические площадки. В плане сейчас ${n(plan.selected_total || 0)} доставок.`;
      if (plan.approval_required) {
        notice.textContent += " Глобальное ручное одобрение дополнительно блокирует основную очередь публикации.";
      }
    }
    $$("#distribution-mode-buttons [data-runtime-mode]").forEach((button) => {
      const active = button.dataset.runtimeMode === mode;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
      button.disabled = active;
    });

    const primaryRoot = $("#distribution-primary-grid");
    const primary = plan.primary_platforms || [];
    if (primaryRoot) {
      primaryRoot.innerHTML = primary.map((item) => {
        const remaining = Math.max(0, Number(item.remaining || 0));
        const cap = Math.max(0, Number(item.daily_cap || 0));
        const sent = Math.max(0, Number(item.daily_sent || 0));
        const channelList = (item.channels || []).map(esc).join(" · ");
        const topics = (item.topics || []).map(esc).join(", ");
        const queued = item.queue_preview || [];
        // A row of the preview answers two questions, and the second one used to
        // be missing entirely: «что это за пост» the card always said, «заказан ли
        // он планом» it never did. `queueProvenanceText` answers the second one in
        // four words, and `queueProvenanceLegend` says out loud when the platform's
        // whole preview carries no provenance read at all.
        const queuedMarkup = queued.length
          ? `<div class="distribution-picks"><strong>В очереди: ${n(queued.length)}+</strong>${queueProvenanceLegend(item)}${queued.slice(0, 3).map((post) => `
              <div class="distribution-pick">
                <strong>${esc(post.title || post.post_public_id)}</strong>
                <span>${esc(post.channel || "")} · ${esc(post.status || "")}${post.scheduled_at ? " · " + esc(nextRunCell(post.scheduled_at)) : ""}</span>
                <small>${queueProvenanceText(post, item)}</small>
              </div>`).join("")}</div>`
          : `<div class="distribution-no-pick">В очереди пока нет готового материала.</div>`;
        const progress = cap ? Math.min(100, sent / cap * 100) : 0;
        return `
          <article class="distribution-card ${item.enabled && item.configured ? "is-ready" : "is-paused"}">
            <div class="distribution-card-top">
              <strong>${esc(item.name || item.platform)}</strong>
              <span class="distribution-state">${item.enabled ? (item.configured ? "Основная очередь" : "Нужна настройка") : "ВЫКЛ"}</span>
            </div>
            <div class="distribution-budget">
              <b>${n(sent)} <small>/ ${n(cap)}</small></b>
              <span>постов за 24 ч · осталось ${n(remaining)}</span>
            </div>
            <div class="distribution-track"><i style="width:${progress}%"></i></div>
            <p class="distribution-topics">${topics || "Темы не заданы"}</p>
            <p class="distribution-detail">${channelList || esc(item.reason || "")}</p>
            ${queuedMarkup}
            <small>${esc(item.reason || "")}</small>
          </article>`;
      }).join("") || `<div class="distribution-empty">Нет включённых основных каналов.</div>`;
    }

    const crosspostRoot = $("#distribution-crosspost-grid");
    const destinations = plan.platforms || [];
    if (crosspostRoot) {
      crosspostRoot.innerHTML = destinations.map((item) => {
        const selected = item.selected || [];
        const manualSuggestions = item.manual_suggestions || [];
        const recommendations = selected.length ? selected : manualSuggestions;
        const manualOnly = item.automation === "manual";
        const cap = Math.max(0, Number(item.daily_cap || 0));
        const sent = Math.max(0, Number(item.daily_sent || 0));
        const remaining = Math.max(0, Number(item.remaining || 0));
        const progress = cap ? Math.min(100, sent / cap * 100) : 0;
        const stateText = !item.enabled ? "ВЫКЛ"
          : item.automation === "manual" ? "Только вручную"
          : item.automation !== "automatic" ? "Нет адаптера"
          : !item.configured ? "Нужен вход"
          : mode === "off" ? "Автопилот остановлен"
          : "Автоподбор включён";
        const picks = recommendations.length ? recommendations.map((post) => `
          <div class="distribution-pick">
            <strong>${esc(post.title || post.post_public_id)}</strong>
            <span>${esc(post.topic || "подходящая тема")} · совпадение ${Math.round(Number(post.score || 0) * 100)}%</span>
            <small>Формат: ${esc(post.native_format_label || post.native_format || "уточняется")}${post.render_on_demand ? " · видео будет создано в S9" : ""}</small>
            ${manualOnly ? "<small>Только рекомендация для ручной подготовки. Фоновая отправка отключена.</small>" : ""}
            <small>${esc(post.reason || "")}</small>
          </div>`).join("") : `<div class="distribution-no-pick">${esc(item.reason || "На этот тик подходящих материалов нет.")}</div>`;
        const topics = (item.topics || []).slice(0, 4).map(esc).join(" · ");
        const editorial = item.editorial || {};
        const editorialHint = editorial.audience
          ? `<small>Аудитория: ${esc(editorial.audience)} · Адресат: ${esc(editorial.target_selection || editorial.target_type || "профиль")}</small>
             <small>Проверка: ${esc(editorial.rule_gate || "")}</small>`
          : "";
        return `
          <article class="distribution-card ${item.enabled && item.configured && item.automation === "automatic" ? "is-ready" : "is-paused"}">
            <div class="distribution-card-top">
              <strong>${esc(item.name || item.platform)}</strong>
              <span class="distribution-state">${esc(stateText)}</span>
            </div>
            <div class="distribution-budget">
              <b>${n(sent)} <small>/ ${n(cap)}</small></b>
              <span>за 24 ч · осталось ${n(remaining)} · пауза ${n(item.min_gap_hours || 0)} ч</span>
            </div>
            <div class="distribution-track"><i style="width:${progress}%"></i></div>
            <p class="distribution-topics">${topics || "Профиль темы уточняется"}</p>
            ${editorialHint}
            <div class="distribution-picks">${picks}</div>
            <small>${esc(item.reason || item.detail || "")}</small>
          </article>`;
      }).join("") || `<div class="distribution-empty">Нет включённых площадок с автоматической доставкой.</div>`;
    }
    state.distributionPlan = plan;
  }

  // ── Провенанс строки очереди: четыре состояния — четыре формулировки ──────
  // Two payload families answer one operator question — «этот материал заказан
  // планом, ждёт своего слота, или план тут ни при чём?» — and they answer it with
  // different keys, so the panel needs one reading of both:
  //
  // * `queue_preview` rows of `/api/distribution/plan` carry `plan_entry_id`,
  //   `plan_slot_at`, `plan_public_id`, and beside them the platform's own verdict
  //   `queue_provenance`: `measured` when the booking columns were really read,
  //   `plan_columns_missing` when the live store has none of them and nothing under
  //   it was read at all;
  // * `/api/editorial` answers the narrower question with `awaiting_booking` —
  //   `tgfarm/editorial.py::awaiting_booking`, the open order the plan still owes
  //   a slot to. The server has been producing that key since W20-B and the panel
  //   read it nowhere: an operator could not tell a draft waiting for a booked
  //   slot from a topic nobody has queued at all.
  //
  // Four states, four sentences, none of them a zero and none an absence:
  //   «по плану»             — the row names the plan entry that ordered it;
  //   «не по плану»          — the read happened and the entry reference is null;
  //   «ждёт слота плана»     — the server's own open-order predicate answered yes;
  //   «провенанс не прочитан» — the columns are missing, or the keys never came.
  // The last line is the standing rule of this project: silence is not a verdict,
  // so a row without the keys prints neither «не по плану» nor a count of nothing.
  const QUEUE_PROVENANCE_UNREAD = "провенанс не прочитан";
  const QUEUE_PROVENANCE_LABELS = {
    planned: "по плану",
    unplanned: "не по плану",
    awaiting: "ждёт слота плана",
    unread: QUEUE_PROVENANCE_UNREAD,
  };
  // The order this block prints its tally in, and the order the contract test
  // walks to prove no two states share a sentence.
  const QUEUE_PROVENANCE_ORDER = ["planned", "unplanned", "awaiting", "unread"];
  const QUEUE_PROVENANCE_KEYS = ["plan_entry_id", "plan_slot_at", "plan_public_id"];
  const QUEUE_PROVENANCE_MEASURED = "measured";
  const QUEUE_PROVENANCE_NO_COLUMNS = "plan_columns_missing";
  // A measured ratio keeps its two decimals; an unread one says it has no report
  // instead of printing the `0` that `n()` would manufacture out of a null.
  const queueNumberText = (value, caption) => (typeof value === "number" && Number.isFinite(value)
    ? `${caption} — ${value.toFixed(2)}`
    : `${caption} — нет отчёта`);

  // One resolution, four answers. The carrier's verdict outranks the row: the
  // platform saying «колонок нет» invalidates every field under it, and a row that
  // did carry values read from a store without those columns would otherwise be
  // printed as a measurement.
  function queueProvenanceState(row, carrier) {
    const src = row && typeof row === "object" ? row : {};
    const owner = carrier && typeof carrier === "object" ? carrier : {};
    const verdict = typeof owner.queue_provenance === "string" ? owner.queue_provenance : null;
    if (verdict === QUEUE_PROVENANCE_NO_COLUMNS) {
      return { state: "unread", reason: "хранилище ответило plan_columns_missing" };
    }
    // `true` is a measured verdict from the queue; `false` and an absent key are
    // handled below, because neither of them names the plan entry of this row.
    if (src.awaiting_booking === true) return { state: "awaiting", reason: "" };
    if (!QUEUE_PROVENANCE_KEYS.every((key) => key in src)) {
      return {
        state: "unread",
        reason: src.awaiting_booking === false
          ? "ключей плана в строке нет · слота плана эта тема не ждёт"
          : "ключей плана в строке нет",
      };
    }
    if (verdict !== null && verdict !== QUEUE_PROVENANCE_MEASURED) {
      // The code itself is data the store sent: it is returned, never formatted
      // here, so the one place that prints it is the one place that escapes it.
      return { state: "unread", reason: "код провенанса не описан", code: verdict };
    }
    if (src.plan_entry_id === null) return { state: "unplanned", reason: "" };
    // A reference the panel can name is a reference the server read: an integer id
    // or a non-empty public id. Anything else — NaN, an empty string, an object —
    // is a field that arrived without an answer behind it.
    const readable = (typeof src.plan_entry_id === "number" && Number.isFinite(src.plan_entry_id))
      || (typeof src.plan_entry_id === "string" && src.plan_entry_id.trim() !== "");
    return readable
      ? { state: "planned", reason: "" }
      : { state: "unread", reason: "ссылка на запись плана не читается" };
  }

  // What the server named beside the state. Every fragment leaves here already
  // escaped, so the caller cannot interpolate a stored string raw; `nextRunCell`
  // is the helper that keeps a slot booked for tonight from being read by `dt()`
  // as a moment that has already passed.
  function queueProvenanceDetail(src, state) {
    if (state === "planned") {
      const plan = typeof src.plan_public_id === "string" && src.plan_public_id
        ? `план ${esc(src.plan_public_id)}`
        : "план не назван";
      const slot = typeof src.plan_slot_at === "string" && src.plan_slot_at
        ? `слот ${esc(nextRunCell(src.plan_slot_at))}`
        : "слот не записан";
      return ` · ${plan} · запись ${esc(String(src.plan_entry_id))} · ${slot}`;
    }
    if (state === "unplanned") return " · запись плана прочитана: её нет";
    if (state === "awaiting") return " · чернов уже написан, забронированного слота нет";
    return "";
  }

  function queueProvenanceText(row, carrier) {
    const src = row && typeof row === "object" ? row : {};
    const resolved = queueProvenanceState(src, carrier);
    const label = QUEUE_PROVENANCE_LABELS[resolved.state] || QUEUE_PROVENANCE_UNREAD;
    // `reason` is a literal of this file and `code` is a string the store sent: the
    // first is printed as it is, the second goes through `esc()` here and nowhere
    // else, so no field of this block is ever escaped twice.
    const reasonText = resolved.reason ? ` · ${esc(String(resolved.reason))}` : "";
    const codeText = resolved.code ? ` · ${esc(String(resolved.code))}` : "";
    // The unread label already names itself; prefixing it with «провенанс:» would
    // print the word twice and hide the reason under the stutter.
    const head = resolved.state === "unread"
      ? esc(QUEUE_PROVENANCE_UNREAD)
      : `провенанс: ${esc(label)}`;
    return `${head}${queueProvenanceDetail(src, resolved.state)}${reasonText}${codeText}`;
  }

  function queueProvenanceChip(row, carrier) {
    return `<span class="plan-chip">${queueProvenanceText(row, carrier)}</span>`;
  }

  // The platform-level sentence, printed once above the rows: a whole preview read
  // without the booking columns is one fact about the store, not three identical
  // row warnings the operator has to compare.
  function queueProvenanceLegend(item) {
    const owner = item && typeof item === "object" ? item : {};
    if (!("queue_provenance" in owner)) {
      return `<small>${esc(`провенанс очереди не прочитан — ключа queue_provenance в ответе нет`)}</small>`;
    }
    if (owner.queue_provenance === QUEUE_PROVENANCE_NO_COLUMNS) {
      return `<small>${esc(`провенанс очереди не прочитан — `
        + "хранилище ответило plan_columns_missing, поля брони не читались")}</small>`;
    }
    if (owner.queue_provenance !== QUEUE_PROVENANCE_MEASURED) {
      return `<small>${esc("провенанс очереди не прочитан — незнакомый код: ")
        + esc(String(owner.queue_provenance))}</small>`;
    }
    return "";
  }

  // ── Кто из черновиков ждёт слота плана ────────────────────────────────────
  // `index.html` is outside this file's reach, so the card creates its own mount
  // next to `#plan-entries` instead of writing into a container another render
  // replaces (the booking card above uses the same move for the same reason).
  const OPEN_ORDERS_ENDPOINT = "/api/editorial";
  const OPEN_ORDERS_SHOWN = 8;
  const OPEN_ORDERS_TRANSPORT = "очередь: ответ не пришёл";

  function openOrdersMount() {
    const existing = $("#plan-open-orders");
    if (existing) return existing;
    const anchor = $("#plan-entries");
    if (!anchor || !anchor.parentNode) return null;
    const node = document.createElement("div");
    node.id = "plan-open-orders";
    anchor.parentNode.insertBefore(node, anchor);
    return node;
  }

  function openOrderRow(row, carrier) {
    const src = row && typeof row === "object" ? row : {};
    const title = typeof src.label === "string" && src.label
      ? src.label
      : (typeof src.cluster_public_id === "string" && src.cluster_public_id
        ? src.cluster_public_id : "тема без подписи");
    const measured = queueNumberText(src.heat, "тепло");
    const score = queueNumberText(src.score, "приоритет");
    const niche = typeof src.niche === "string" && src.niche ? src.niche : "ниша не задана";
    return `<li class="plan-entry"><div class="plan-entry-head">`
      + `<span>${esc(title)}</span>`
      + queueProvenanceChip(src, carrier)
      + `</div><div class="plan-entry-reasons">${esc(niche)} · ${esc(measured)} · ${esc(score)}`
      + ` · ${esc(String(src.cluster_public_id || "публичный id темы не прислан"))}</div></li>`;
  }

  function renderOpenOrderQueue(payload, failure) {
    const root = openOrdersMount();
    if (!root) return;
    if (!payload || typeof payload !== "object") {
      // A request that never landed is not a store that answered "nothing waits".
      // The whole sentence is escaped once, below, where it becomes markup.
      const failureText = failure
        ? (typeof failure === "string" ? failure : String(failure.message || failure))
        : OPEN_ORDERS_TRANSPORT;
      root.innerHTML = `<h3>Кто ждёт слота плана</h3>`
        + `<div class="plan-chips"><span class="plan-chip">${esc(OPEN_ORDERS_TRANSPORT)}</span></div>`
        + `<p class="plan-entry-reasons">${esc(`Запрос редакционной очереди не удался: ${failureText}. `
          + "Это не «ждущих нет» и не «сервер ответил пусто» — ответа не было.")}</p>`;
      return;
    }
    const head = `<h3>Кто ждёт слота плана</h3>`;
    if (!Array.isArray(payload.queue)) {
      root.innerHTML = head
        + `<div class="plan-chips"><span class="plan-chip">${esc(`очередь: ${QUEUE_PROVENANCE_UNREAD}`)}</span></div>`
        + `<p class="plan-entry-reasons">${esc("Ключ queue не пришёл в ответе — очередь не прочитана, "
          + "а не пуста: ни одного состояния ниже печатать нечем.")}</p>`;
      return;
    }
    const rows = payload.queue;
    const waiting = rows.filter((row) => row && typeof row === "object" && row.awaiting_booking === true);
    const answered = rows.filter((row) => row && typeof row === "object" && row.awaiting_booking === false);
    const silent = rows.length - waiting.length - answered.length;
    const tally = [
      `прочитано тем: ${n(rows.length)}`,
      // A measured zero is a measurement and prints as one. The third line is not a
      // zero: it counts the rows whose `awaiting_booking` never arrived, and it is
      // printed only when such rows really exist.
      `ждут слота плана: ${n(waiting.length)}`,
      `слота плана не ждут: ${n(answered.length)}`,
    ];
    if (silent) tally.push(`ключа о брони не прислано: ${n(silent)}`);
    const intro = waiting.length
      ? `План уже заказал эти темы, а слота им ещё не забронировали: S4 не тратит на них бюджет, `
        + "и ждать им остаётся ровно до ближайшего пропуска исполнителя."
      : `Ни одна прочитанная тема не ждёт слота плана — это ответ очереди, а не её отсутствие.`;
    const listed = waiting.length ? waiting : rows;
    const shown = listed.slice(0, OPEN_ORDERS_SHOWN);
    const whichList = waiting.length
      ? (listed.length > shown.length
        ? `показаны ${n(shown.length)} из ${n(listed.length)} ждущих — карточка называет не больше ${n(OPEN_ORDERS_SHOWN)} строк за раз`
        : `показаны все ${n(shown.length)} ждущих`)
      : (rows.length > shown.length
        ? `ждущих нет, показаны первые ${n(shown.length)} прочитанных тем из ${n(rows.length)}`
        : `ждущих нет, показаны все ${n(rows.length)} прочитанных тем`);
    root.innerHTML = head
      + `<div class="plan-chips">${tally.map((line) => `<span class="plan-chip">${esc(line)}</span>`).join("")}</div>`
      + `<p class="plan-entry-reasons">${esc(intro)}</p>`
      + (shown.length
        ? `<ul class="plan-entries">${shown.map((row) => openOrderRow(row, payload)).join("")}</ul>`
          + `<p class="plan-entry-reasons">${esc(whichList)}</p>`
        : `<p class="plan-entry-reasons">${esc("Очередь ответила списком без строк — ждущих слота тем нет.")}</p>`);
  }

  async function loadOpenOrderQueue() {
    const root = openOrdersMount();
    if (!root) return;
    try {
      renderOpenOrderQueue(await api(OPEN_ORDERS_ENDPOINT), null);
    } catch (error) {
      renderOpenOrderQueue(null, error);
    }
  }

  window.tgfarmSetRuntimeMode = async (mode) => {
    if (!["off", "dry", "live"].includes(mode)) return;
    if (mode === "live") {
      const proceed = confirm(
        "Включить LIVE-автопостинг?\n\n"
        + "TgFarm начнёт публиковать реальные материалы в выбранные и подключённые автоматические площадки. "
        + "Неподключённые, выключенные и площадки с ручным подтверждением будут пропущены. "
        + "Можно в любой момент выключить LIVE или отдельную площадку."
      );
      if (!proceed) return;
    } else if (mode === "off") {
      if (!confirm("Остановить все новые автоматические публикации? Очереди и настройки сохранятся.")) return;
    }
    try {
      const result = await put("/api/runtime/mode", {
        mode,
        confirm_live: mode === "live",
      });
      toast(
        mode === "live" ? "LIVE включён. Автопостинг работает по редакторскому плану."
          : mode === "dry" ? "DRY включён. Ферма покажет план без реальных отправок."
            : "Автопостинг остановлен.",
        "ok",
      );
      renderDistributionPlan(result.plan || {});
      await refresh();
    } catch (error) {
      toast("Не удалось изменить режим автопилота: " + (error?.message || error), "err");
    }
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
      const automationLabel = item.automation_mode === "automatic" ? "Автопостинг"
        : item.automation_mode === "primary" ? "Основная очередь"
        : item.automation_mode === "manual" ? "Только вручную"
        : "Нет автопубликации";
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
      const authSetupLabel = auth.method === "browser"
        ? (item.id === "dzen" ? "Войти в Дзен" : item.id === "vc_ru" ? "Войти в vc.ru" : item.id === "pikabu" ? "Войти в Пикабу" : "Войти в браузере")
        : auth.method === "vkhost" ? "Получить токен VK"
        : auth.method === "oauth" ? "Подключить через OAuth"
        : auth.method === "credentials" ? "Настроить доступ" : "Войти / подключить ↗";
      const authSetup = `<button type="button" class="btn btn-ghost btn-sm platform-auth-button"
          data-platform-auth="${esc(item.id)}" ${busy ? "disabled" : ""}>${authSetupLabel}</button>`;
      const authConfig = supported && (
        (["x", "threads", "linkedin", "youtube", "tiktok", "instagram", "reddit", "pinterest", "facebook"].includes(item.id) && auth.method === "oauth")
        || item.id === "vk"
      )
        ? `<button type="button" class="btn btn-ghost btn-sm platform-auth-config" data-platform-auth-config="${esc(item.id)}" ${busy ? "disabled" : ""}>${item.id === "vk" ? "ID сообщества" : item.id === "reddit" ? "App / subreddit" : "Настроить приложение"}</button>`
        : "";
      const authCheck = supported && (auth.method === "credentials" || (auth.method === "oauth" && item.configured) || (auth.method === "vkhost" && item.configured))
        ? `<button type="button" class="btn btn-primary btn-sm platform-auth-check"
             data-platform-auth-check="${esc(item.id)}" ${busy ? "disabled" : ""}>Проверить</button>`
        : "";
      const browserSession = auth.browser_session || {};
      const browserSessionMarkup = browserSession.profile_saved
        ? `<small class="platform-auth-checked">${browserSession.verified ? "Сессия сайта сохранена и распознана" : "Профиль сайта сохранён, вход не подтверждён"}</small>`
        : "";
      const browserLoginButton = browserSession.available && !["dzen", "vk", "vc_ru", "pikabu"].includes(item.id)
        ? `<button type="button" class="btn btn-ghost btn-sm platform-browser-login" data-platform-browser-auth="${esc(item.id)}" ${busy ? "disabled" : ""}>Войти в браузере</button>`
        : "";
      const maxHasToken = (auth.fields || []).some((field) => field.key === "MAX_ACCESS_TOKEN" && field.has_value);
      const destinationTools = ["pinterest", "facebook", "vk", "max"].includes(item.id)
        ? `<div class="platform-destination-tools">
             ${item.id === "max" ? `<button type="button" class="btn btn-ghost btn-sm" data-platform-max-webhook ${!maxHasToken || busy ? "disabled" : ""}>Подключить поиск чатов</button>` : ""}
             <button type="button" class="btn btn-ghost btn-sm" data-platform-destinations="${esc(item.id)}" ${item.id === "max" && !maxHasToken ? "disabled" : ""}>${item.id === "facebook" ? "Найти мои Pages" : item.id === "vk" ? "Найти мои сообщества" : item.id === "max" ? "Найти мои чаты" : "Найти мои доски"}</button>
             <div id="platform-destination-${esc(item.id)}"></div>
             ${item.id === "max" ? "<small class='platform-destination-note'>Укажи TGFARM_PUBLIC_BASE_URL=https://твой-домен на сервере. Нужен публичный HTTPS на 443. После подписки добавь бота в канал/чат и выдай право write.</small>" : ""}
           </div>`
        : "";
      const readyHint = supported
        ? item.connection_detail
        : (item.planned ? "Выбрана для следующей волны интеграций." : "Можно добавить в список следующих интеграций.");
      const automationNote = item.automation_note
        ? `<small class="platform-auth-checked">${esc(item.automation_note)}</small>`
        : "";
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
          ${item.editorial ? `<div class="platform-editorial-meta">
            <span>РЕДАКЦИОННЫЙ ПРОФИЛЬ</span>
            <p><strong>${esc(item.editorial.label || item.editorial.format)}</strong> · ${esc(item.editorial.audience || "")}</p>
            <small>Задача: ${esc(item.editorial.goal || "")}</small>
            <small>Адресат: ${esc(item.editorial.target_selection || "")}</small>
            <small>Проверка: ${esc(item.editorial.rule_gate || "")}</small>
          </div>` : ""}
          <div class="platform-badge-row">
            <span class="platform-status status-${statusClass}"><i></i>${esc(item.status_label || "Статус неизвестен")}</span>
            <span class="platform-priority ${item.priority === "core" ? "priority-core" : ""}">${esc(priority)}</span>
            <span class="platform-priority">${esc(automationLabel)}</span>
            ${item.capability ? `<span class="platform-capability capability-${esc(item.capability)}" title="${esc(item.capability_note || "")}">${esc(item.capability_label || item.capability)}</span>` : ""}
          </div>
          <div class="platform-connection">
            <span class="platform-connection-icon">${supported ? (item.configured ? "✓" : "!") : "↗"}</span>
            <div><strong>${supported ? (item.configured ? "Настройки найдены" : "Нужна настройка") : "Пока не подключена"}</strong>
              <p>${esc(readyHint)}</p>${automationNote}
            </div>
          </div>
          <div class="platform-auth-block ${authStyle}">
            <div class="platform-auth-copy">
              <span class="platform-auth-dot" aria-hidden="true"></span>
              <div><strong>${esc(authLabel)}</strong><p>${esc(auth.detail || auth.note || (supported ? "Статус доступа ещё не проверен." : "Открытие сайта не подключает публикацию к ферме."))}</p>${authCheckedMarkup}${browserSessionMarkup}</div>
            </div>
            <div class="platform-auth-actions">${authConfig}${authSetup}${authCheck}${browserLoginButton}</div>
           ${destinationTools}
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

  async function registerMaxWebhook(button = null) {
    if (button) {
      button.disabled = true;
      button.textContent = "Подключаю webhook…";
    }
    try {
      const result = await post("/api/platforms/max/webhook/register", {});
      toast(
        "MAX webhook подключён для " + (result.bot_name || "бота")
          + ". Добавь бота в свой канал или групповой чат, выдай право write, затем нажми «Найти мои чаты».",
        "ok",
      );
      await refresh();
    } catch (error) {
      toast("Не удалось подключить MAX webhook: " + (error?.message || error), "err");
    } finally {
      if (button) {
        button.disabled = false;
        button.textContent = "Подключить поиск чатов";
      }
    }
  }

  async function discoverPlatformDestinations(platformId, button) {
    const root = document.getElementById("platform-destination-" + platformId);
    if (!root) return;
    const isFacebook = platformId === "facebook";
    const isVk = platformId === "vk";
    const isMax = platformId === "max";
    const noun = isFacebook ? "страниц" : isVk ? "сообществ" : isMax ? "чатов/каналов" : "досок";
    const previousText = button?.textContent || "";
    if (button) {
      button.disabled = true;
      button.textContent = "Ищу доступные места…";
    }
    root.innerHTML = "";
    try {
      const result = await api("/api/platforms/" + encodeURIComponent(platformId) + "/destinations");
      const destinations = result.destinations || [];
      if (!destinations.length) {
        const note = isMax && !result.webhook_registered
          ? (result.webhook_detail || "Сначала нажми «Подключить поиск чатов» и настрой публичный HTTPS webhook на порту 443.")
          : isMax
            ? "Подписка активна. Теперь добавь бота в свой MAX-канал или групповой чат и выдай ему право write. Через событие bot_added появится chat_id."
            : "API не вернул доступных " + noun + ". Проверь разрешения приложения и права аккаунта.";
        root.innerHTML = '<p class="platform-destination-note">' + esc(note) + '</p>';
        return;
      }
      const selectId = "platform-destination-select-" + platformId;
      const options = destinations.map((item) => `
        <option value="${esc(item.id)}">${esc(item.name)}${item.privacy ? " · " + esc(item.privacy) : item.role ? " · " + esc(item.role) : isMax ? " · " + (item.kind === "channel" ? "канал" : "чат") : ""}</option>
      `).join("");
      root.innerHTML = `
        <label class="field platform-destination-field">
          <span>Куда публиковать</span>
          <select id="${esc(selectId)}">${options}</select>
        </label>
        <button type="button" class="btn btn-primary btn-sm" data-platform-destination-save="${esc(platformId)}">Сохранить выбранное место</button>
        <small class="platform-destination-note">Найдено: ${n(destinations.length)}. Доступ будет проверен ещё раз перед сохранением.</small>
      `;
      const save = root.querySelector("[data-platform-destination-save]");
      save?.addEventListener("click", async () => {
        const selectedId = document.getElementById(selectId)?.value || "";
        save.disabled = true;
        try {
          const payload = isFacebook
            ? { page_id: selectedId }
            : isVk
              ? { group_id: selectedId }
              : isMax
                ? { chat_id: selectedId }
                : { board_id: selectedId };
          const saved = await post(
            "/api/platforms/" + encodeURIComponent(platformId) + "/destination",
            payload,
          );
          const name = saved.destination?.name || selectedId;
          toast((isFacebook ? "Страница подключена: " : isVk ? "Сообщество VK подключено: " : isMax ? "MAX назначение проверено и подключено: " : "Доска подключена: ") + name, "ok");
          await refresh();
        } catch (error) {
          toast("Не удалось выбрать место: " + (error?.message || error), "err");
        } finally {
          save.disabled = false;
        }
      });
    } catch (error) {
      root.innerHTML = '<p class="platform-destination-note is-error">' + esc(error?.message || error) + '</p>';
      toast("Не удалось получить список назначений: " + (error?.message || error), "err");
    } finally {
      if (button) {
        button.disabled = false;
        button.textContent = previousText;
      }
    }
  }

  async function openPlatformAuthorization(platformId, forceCredentials = false) {
    const item = (state.platformData?.platforms || []).find((entry) => entry.id === platformId);
    if (!item) return;
    const auth = item.auth || {};
    if (auth.method === "browser" && platformId === "dzen") {
      window.tgfarmDzenLogin();
      return;
    }
    if (auth.method === "browser" && ["vc_ru", "pikabu"].includes(platformId)) {
      const button = $(`[data-platform-auth="${platformId}"]`);
      await startPlatformBrowserLogin(platformId, button);
      return;
    }
    if (auth.method === "vkhost" && !forceCredentials) {
      await startVkHostAuthorization();
      return;
    }
    if (auth.method === "oauth" && platformId === "instagram" && !forceCredentials) {
      const appId = (auth.fields || []).find((field) => field.key === "INSTAGRAM_APP_ID");
      const appSecret = (auth.fields || []).find((field) => field.key === "INSTAGRAM_APP_SECRET");
      if (!appId?.has_value || !appSecret?.has_value) {
        toast("Сначала сохрани Instagram App ID и App Secret из Meta Developer Portal.", "err");
        return;
      }
      const callbackUri = window.location.origin.replace(/\/$/, "") + "/api/platforms/instagram/auth/callback";
      const proceed = window.confirm(
        "В Meta Developer Portal добавь Instagram API with Instagram Login, укажи этот Redirect URI и включи разрешения instagram_business_basic и instagram_business_content_publish. Нужен профессиональный Business или Creator аккаунт.\n\n"
        + callbackUri + "\n\nПродолжить официальный Instagram Login?",
      );
      if (!proceed) return;
      try {
        const result = await post("/api/platforms/instagram/auth/start", {});
        if (result.redirect_uri !== callbackUri) {
          throw new Error("Callback URL отличается от адреса панели: " + result.redirect_uri);
        }
        const authorizeUrl = safeExternalUrl(result.authorize_url);
        if (!authorizeUrl) throw new Error("Instagram вернул некорректную ссылку авторизации.");
        window.location.assign(authorizeUrl);
      } catch (error) {
        toast("Не удалось начать Instagram OAuth: " + (error?.message || error), "err");
      }
      return;
    }

    if (auth.method === "oauth" && platformId === "pinterest" && !forceCredentials) {
      const appId = (auth.fields || []).find((field) => field.key === "PINTEREST_APP_ID");
      const appSecret = (auth.fields || []).find((field) => field.key === "PINTEREST_APP_SECRET");
      if (!appId?.has_value || !appSecret?.has_value) {
        toast("Сначала сохрани Pinterest App ID и App Secret. Доску ферма найдёт сама после OAuth.", "err");
        return;
      }
      const callbackUri = window.location.origin.replace(/\/$/, "") + "/api/platforms/pinterest/auth/callback";
      const proceed = window.confirm(
        "В Pinterest Developer Portal добавь этот Redirect URI и включи разрешения boards:read, pins:read и pins:write. После входа TgFarm покажет все доступные доски, чтобы ты выбрал нужную.\n\n"
        + callbackUri + "\n\nПродолжить официальный Pinterest OAuth?",
      );
      if (!proceed) return;
      try {
        const result = await post("/api/platforms/pinterest/auth/start", {});
        if (result.redirect_uri !== callbackUri) {
          throw new Error("Callback URL отличается от адреса панели: " + result.redirect_uri);
        }
        const authorizeUrl = safeExternalUrl(result.authorize_url);
        if (!authorizeUrl) throw new Error("Pinterest вернул некорректную ссылку авторизации.");
        window.location.assign(authorizeUrl);
      } catch (error) {
        toast("Не удалось начать Pinterest OAuth: " + (error?.message || error), "err");
      }
      return;
    }

    if (auth.method === "oauth" && platformId === "reddit" && !forceCredentials) {
      const clientId = (auth.fields || []).find((field) => field.key === "REDDIT_CLIENT_ID");
      const clientSecret = (auth.fields || []).find((field) => field.key === "REDDIT_CLIENT_SECRET");
      if (!clientId?.has_value || !clientSecret?.has_value) {
        toast("Сначала сохрани Reddit Client ID и Client Secret. Создай приложение типа Web App.", "err");
        return;
      }
      const callbackUri = window.location.origin.replace(/\/$/, "") + "/api/platforms/reddit/auth/callback";
      const proceed = window.confirm(
        "В Reddit Developer Apps создай приложение типа Web App и добавь этот Redirect URI. TgFarm запросит identity, read и submit; постоянный refresh token сохранится автоматически.\n\n"
        + callbackUri + "\n\nПродолжить официальный Reddit OAuth?",
      );
      if (!proceed) return;
      try {
        const result = await post("/api/platforms/reddit/auth/start", {});
        if (result.redirect_uri !== callbackUri) {
          throw new Error("Callback URL отличается от адреса панели: " + result.redirect_uri);
        }
        const authorizeUrl = safeExternalUrl(result.authorize_url);
        if (!authorizeUrl) throw new Error("Reddit вернул некорректную ссылку авторизации.");
        window.location.assign(authorizeUrl);
      } catch (error) {
        toast("Не удалось начать Reddit OAuth: " + (error?.message || error), "err");
      }
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

    if (auth.method === "oauth" && platformId === "facebook" && !forceCredentials) {
      const appId = (auth.fields || []).find((field) => field.key === "FACEBOOK_APP_ID");
      const appSecret = (auth.fields || []).find((field) => field.key === "FACEBOOK_APP_SECRET");
      if (!appId?.has_value || !appSecret?.has_value) {
        toast("Сначала сохрани Facebook App ID и App Secret. Список доступных Pages ферма получит сама после OAuth.", "err");
        return;
      }
      const callbackUri = window.location.origin.replace(/\/$/, "") + "/api/platforms/facebook/auth/callback";
      const proceed = window.confirm(
        "В Meta Developer Portal включи Facebook Login, Pages API и добавь этот Redirect URI. Нужны permissions pages_show_list, pages_read_engagement и pages_manage_posts. После OAuth TgFarm покажет только Pages, где твой профиль имеет CREATE_CONTENT или эквивалентную административную задачу. Личные профили и группы не поддерживаются.\n\n"
        + callbackUri + "\n\nПродолжить Facebook Login?",
      );
      if (!proceed) return;
      try {
        const result = await post("/api/platforms/facebook/auth/start", {});
        if (result.redirect_uri !== callbackUri) {
          throw new Error("Callback URL отличается от адреса панели: " + result.redirect_uri);
        }
        const authorizeUrl = safeExternalUrl(result.authorize_url);
        if (!authorizeUrl) throw new Error("Facebook вернул некорректную ссылку авторизации.");
        window.location.assign(authorizeUrl);
      } catch (error) {
        toast("Не удалось начать Facebook OAuth: " + (error?.message || error), "err");
      }
      return;
    }

    if (auth.method === "oauth" && platformId === "tumblr" && !forceCredentials) {
      const clientId = (auth.fields || []).find((field) => field.key === "TUMBLR_CLIENT_ID");
      const clientSecret = (auth.fields || []).find((field) => field.key === "TUMBLR_CLIENT_SECRET");
      if (!clientId?.has_value || !clientSecret?.has_value) {
        toast("Сначала сохрани Tumblr Client ID и Client Secret.", "err");
        return;
      }
      const callbackUri = window.location.origin.replace(/\/$/, "") + "/api/platforms/tumblr/auth/callback";
      const proceed = window.confirm(
        "В Tumblr OAuth Apps добавь этот Callback URL и разреши scopes basic, write и offline_access. " +
        "При желании заранее укажи Blog identifier в карточке; иначе подключится основной блог аккаунта.\n\n"
        + callbackUri
        + "\n\nПродолжить?",
      );
      if (!proceed) return;
      try {
        const result = await post("/api/platforms/tumblr/auth/start", {});
        if (result.redirect_uri !== callbackUri) {
          throw new Error("Callback URL отличается от адреса панели: " + result.redirect_uri);
        }
        const authorizeUrl = safeExternalUrl(result.authorize_url);
        if (!authorizeUrl) throw new Error("Tumblr вернул некорректную ссылку авторизации.");
        window.location.assign(authorizeUrl);
      } catch (error) {
        toast("Не удалось начать Tumblr OAuth: " + (error?.message || error), "err");
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

    if (auth.method === "oauth" && platformId === "tiktok" && !forceCredentials) {
      const clientKey = (auth.fields || []).find((field) => field.key === "TIKTOK_CLIENT_KEY");
      const clientSecret = (auth.fields || []).find((field) => field.key === "TIKTOK_CLIENT_SECRET");
      if (!clientKey?.has_value || !clientSecret?.has_value) {
        toast("Сначала нажми «Настроить приложение» и сохрани TikTok Client Key/Secret.", "err");
        return;
      }
      const callbackUri = window.location.origin.replace(/\/$/, "") + "/api/platforms/tiktok/auth/callback";
      const proceed = window.confirm(
        "В TikTok Developer Portal включи Content Posting API, добавь redirect URI ниже и запроси scopes user.info.basic + video.publish. До аудита приложения публикации могут быть ограничены приватными.\n\n"
        + callbackUri + "\n\nПродолжить подключение?",
      );
      if (!proceed) return;
      try {
        const result = await post("/api/platforms/tiktok/auth/start", {});
        if (result.redirect_uri !== callbackUri) {
          throw new Error("Callback URL отличается от адреса панели: " + result.redirect_uri);
        }
        const authorizeUrl = safeExternalUrl(result.authorize_url);
        if (!authorizeUrl) throw new Error("TikTok вернул некорректную ссылку авторизации.");
        window.location.assign(authorizeUrl);
      } catch (error) {
        toast("Не удалось начать TikTok OAuth: " + (error?.message || error), "err");
      }
      return;
    }

    if (auth.method === "oauth" && platformId === "douyin" && !forceCredentials) {
      const clientKey = (auth.fields || []).find((field) => field.key === "DOUYIN_CLIENT_KEY");
      const clientSecret = (auth.fields || []).find((field) => field.key === "DOUYIN_CLIENT_SECRET");
      if (!clientKey?.has_value || !clientSecret?.has_value) {
        toast("Сначала сохрани Douyin Client Key и Client Secret из Douyin Open Platform.", "err");
        return;
      }
      const callbackUri = window.location.origin.replace(/\/$/, "") + "/api/platforms/douyin/auth/callback";
      const proceed = window.confirm(
        "В Douyin Open Platform создай Web App, добавь этот Redirect URI и запроси scopes user_info и video.create. "
        + "Приложение должно иметь доступ к созданию видео; каждое видео всё равно требует отдельного preview и подтверждения владельца.\n\n"
        + callbackUri + "\n\nПродолжить официальный Douyin OAuth?"
      );
      if (!proceed) return;
      try {
        const result = await post("/api/platforms/douyin/auth/start", {});
        if (result.redirect_uri !== callbackUri) {
          throw new Error("Callback URL отличается от адреса панели: " + result.redirect_uri);
        }
        const authorizeUrl = safeExternalUrl(result.authorize_url);
        if (!authorizeUrl) throw new Error("Douyin вернул некорректную ссылку авторизации.");
        window.location.assign(authorizeUrl);
      } catch (error) {
        toast("Не удалось начать Douyin OAuth: " + (error?.message || error), "err");
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
      if (auth.browser_session?.available && !["dzen", "vk"].includes(platformId)) {
        await startPlatformBrowserLogin(platformId);
        return;
      }
      const url = safeExternalUrl(auth.connect_url);
      if (url) window.open(url, "_blank", "noopener,noreferrer");
      toast(
        item.supported
          ? "Сайт открыт. Вход на сайт сам по себе не выдаёт API-разрешения для TgFarm."
          : "Сайт открыт. Эта сеть пока не публикует через TgFarm; вход не означает подключение API.",
        item.supported ? "" : "err",
      );
      return;
    }

    state.authPlatformId = platformId;
    const dialog = $("#platform-auth-dialog");
    $("#platform-auth-title").textContent = item.name + " · подключение";
    $("#platform-auth-description").textContent = platformId === "pinterest"
      ? "Введи Pinterest App ID и App Secret. После OAuth TgFarm покажет доступные доски и проверит выбранную доску."
      : platformId === "facebook"
        ? "Введи Facebook App ID и App Secret. После OAuth TgFarm сможет найти управляемые Pages и выбрать страницу с CREATE_CONTENT."
        : platformId === "youtube"
      ? "Введи Google OAuth Client ID и Client Secret из Google Cloud Console. Создай Web application client, включи YouTube Data API v3 и добавь callback URI панели. После сохранения нажми «Подключить через OAuth»."
      : platformId === "x"
      ? "Введи Client ID и Client Secret из X Developer Portal. Эти ключи хранятся локально, пароль X не нужен. После сохранения нажми «Подключить через OAuth»."
      : platformId === "threads"
        ? "Введи Threads App ID и App Secret из Meta Developer Portal. Добавь callback URI панели и разрешения threads_basic, threads_content_publish. После сохранения нажми «Подключить через OAuth»."
      : platformId === "instagram"
        ? "Введи Instagram App ID и App Secret из Meta Developer Portal. Включи Instagram API with Instagram Login и добавь callback URI панели. Для входа нужен профессиональный аккаунт Business или Creator."
      : platformId === "douyin"
         ? "Введи Douyin Client Key и Client Secret из Douyin Open Platform. Добавь callback URI панели и включи scopes user_info и video.create. Каждое видео будет требовать отдельного подтверждения."
       : platformId === "reddit"
        ? "Введи Client ID и Client Secret приложения Reddit типа Web App. После сохранения TgFarm откроет официальный экран Reddit, получит permanent refresh token и сохранит его локально. Subreddit укажешь отдельно."
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
    save.textContent = ["x", "threads", "linkedin", "youtube", "tiktok", "instagram", "reddit", "douyin"].includes(platformId) ? "Сохранить приложение" : "Сохранить и проверить";
    $("#platform-auth-check").hidden = ["x", "threads", "linkedin", "youtube", "instagram", "reddit", "douyin"].includes(platformId);
    setPlatformAuthMessage(
      (auth.fields || []).some((field) => field.has_value)
        ? "Часть полей уже сохранена. Оставь их пустыми, если не хочешь менять."
        : "Заполни поля, которые нужны этой площадке. Пустые значения ничего не стирают.",
    );
    if (!dialog.open) dialog.showModal();
  }

  async function startVkHostAuthorization(button = null) {
    if (!confirm(
      "Откроется VKHost. Выбери приложение и разрешения для стены, фотографий и групп, затем войди в VK и подтверди доступ. TgFarm перехватит только OAuth-токен из redirect, проверит его через VK API и сохранит локально. Пароль VK не читается."
    )) return;
    if (button) button.disabled = true;
    toast("Открываю VKHost. После подтверждения окно вернёт токен…");
    try {
      const result = await post("/api/platforms/vk/auth/vkhost?timeout=420", {});
      toast(
        "VK подключён: " + (result.account || "аккаунт подтверждён")
          + ". Теперь нажми «Найти мои сообщества» в карточке VK и выбери нужное.",
        "ok",
      );
      await refresh();
    } catch (error) {
      toast("Не удалось получить токен VK: " + (error?.message || error), "err");
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function startPlatformBrowserLogin(platformId, button = null) {
    if (!platformId || state.platformBusy.has(platformId)) return;
    state.platformBusy.add(platformId);
    if (button) button.disabled = true;
    renderPlatformCards();
    toast("Открываю отдельный профиль " + platformId + ". Войди на сайте в открывшемся окне…");
    try {
      const result = await post(
        "/api/platforms/" + encodeURIComponent(platformId) + "/browser-login?timeout=420", {},
      );
      const message = result.authorized
        ? "Вход в " + platformId + " распознан. Профиль сохранён локально."
        : "Профиль " + platformId + " сохранён, но подтверждение входа не распознано. API-доступ отдельно не подтверждён.";
      toast(message, result.authorized ? "ok" : "warn");
    } catch (error) {
      toast("Не удалось сохранить вход " + platformId + ": " + (error?.message || error), "err");
    } finally {
      state.platformBusy.delete(platformId);
      await refresh();
      renderPlatformCards();
    }
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
      if (["x", "threads", "linkedin", "youtube", "tiktok"].includes(platformId)) {
        closePlatformAuthDialog();
        const platformLabel = {
          x: "OAuth-приложение X",
          threads: "Threads App",
          linkedin: "LinkedIn App",
          youtube: "Google OAuth-приложение",
          tiktok: "TikTok Developer App",
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
      save.textContent = ["x", "threads", "linkedin", "youtube", "tiktok"].includes(platformId) ? "Сохранить приложение" : "Сохранить и проверить";
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
    if (enabled && platformId === "x") {
      const proceed = confirm(
        "Разрешить TgFarm автоматически публиковать подобранные информационные материалы в X через официальный API?\n\n"
        + "Ферма не будет публиковать тренды, одинаковые сообщения или массовые ответы. "
        + "Отключи площадку в любой момент, чтобы остановить новые отправки."
      );
      if (!proceed) return;
    }
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

  async function loadSourcesHealth() {
    const root = $("#sources-health");
    if (!root) return;
    try {
      const data = await api("/api/sources/health");
      const summary = (data && data.summary) || {};
      const counts = summary.counts || {};
      const problems = summary.top_problems || [];
      // The endpoint returns at most five offenders; saying so beats implying
      // the list is complete when the fleet has more sources in trouble.
      const attention = Number(summary.attention || 0);
      const hidden = Math.max(0, attention - problems.length);
      const staleHours = Number(summary.stale_hours || 0);
      root.innerHTML =
        '<div class="item"><div class="item-main"><div class="item-title">Флот источников</div>'
        + '<div class="item-meta">' + Number(summary.total || 0) + " всего · "
        + Number(summary.enabled || 0) + " включено · "
        + Number(counts.ok || 0) + " в порядке · " + Number(counts.due || 0) + " ждут проверки · "
        + Number(counts.cooldown || 0) + " в карантине · " + Number(counts.failing || 0) + " сбоят · "
        + Number(counts.disabled || 0) + " отключено</div></div>"
        + '<div class="item-side"><span class="tag ' + (attention ? "tag-warn" : "tag-ok") + '">'
        + attention + " требуют внимания</span></div></div>"
        + '<div class="item"><div class="item-main"><div class="item-title">Молчание</div>'
        + '<div class="item-meta">не проверялись ' + Number(summary.never_checked || 0)
        + " · тише " + staleHours + " ч: " + Number(summary.stale || 0)
        + " · снимок в " + esc(dt(summary.generated_at) || "—") + "</div></div></div>"
        + (problems.length
          ? problems.map((row) => {
            const reason = String(row.reason || "").slice(0, 140);
            // `dt()` describes the past; a cooldown ends in the future, so the
            // clock text is the honest form here.
            const parked = row.cooldown_until ? "пауза до " + clock(row.cooldown_until) : "";
            const attempt = row.last_attempt_at ? "последняя попытка " + dt(row.last_attempt_at) : "попыток ещё не было";
            return '<div class="item"><div class="item-main"><div class="item-title">' + esc(row.title || row.id) + "</div>"
              + '<div class="item-meta" title="' + esc(row.reason || "") + '">' + esc(row.state) + " · провалов "
              + Number(row.failures || 0) + " · " + esc(attempt)
              + (parked ? " · " + esc(parked) : "")
              + (reason ? " · " + esc(reason) : "") + "</div></div>"
              + '<div class="item-side"><button class="btn btn-ghost btn-sm" type="button" data-source-reset="'
              + esc(row.id) + '">Снять cooldown</button></div></div>';
          }).join("")
          : '<div class="item"><div class="item-main"><div class="item-meta">Сейчас ни один источник не в карантине и не в сбоях.</div></div></div>')
        + (hidden
          ? '<div class="item"><div class="item-main"><div class="item-meta">Показаны не все: ещё ' + hidden
            + " в таком же состоянии — полный список в таблице ниже.</div></div></div>"
          : "");
    } catch (error) {
      root.innerHTML = '<div class="empty">Здоровье источников недоступно: ' + esc(error?.message || error) + "</div>";
    }
  }

  /* ------------------------------------------------ reprint backfill report */
  // GET /api/sources/reprint-backfill answers from the backfill module's process
  // memory, so the block has to keep the three states apart: "never ran here",
  // "stopped on budget with a remainder", "finished". A number that was never
  // measured renders as «нет данных» — never as a tidy zero — and the four
  // counters (scanned / remaining / unfingerprinted / undated) stay on the page
  // as four different facts because they mean four different things.
  const backfillFact = (value) => {
    if (typeof value === "boolean") return value ? "да" : "нет";
    if (typeof value === "number") return Number.isFinite(value) ? String(value) : "нет данных";
    return "нет данных";
  };

  function renderReprintBackfill(data) {
    const root = $("#reprint-backfill");
    if (!root) return;
    const d = data || {};
    const chip = d.state === "finished"
      ? '<span class="tag tag-ok">завершён</span>'
      : d.state === "remainder"
        ? '<span class="tag tag-warn">остаток: ' + backfillFact(d.candidates_remaining) + "</span>"
        : '<span class="tag tag-none">нет данных</span>';
    root.innerHTML =
      '<div class="item"><div class="item-main"><div class="item-title">Бэкфилл репринтов</div>'
      + '<div class="item-meta">' + chip + " " + esc(d.state_note || "отчёт не записан в этом процессе") + "</div></div></div>"
      + '<div class="item"><div class="item-main"><div class="item-meta">'
      + "просканировано строк: " + backfillFact(d.rows_scanned)
      + " · найдено связей: " + backfillFact(d.links_found)
      + " · записано связей: " + backfillFact(d.links_written)
      + " · осталось кандидатов: " + backfillFact(d.candidates_remaining)
      + '</div><div class="item-meta">'
      + "без отпечатка: " + backfillFact(d.unfingerprinted_rows)
      + " · без даты: " + backfillFact(d.undated_rows)
      + " · остановлен по: " + (d.stopped_by ? esc(d.stopped_by) : "нет данных")
      + " · бюджет исчерпан: " + backfillFact(d.budget_exhausted)
      + " · пробный проход: " + backfillFact(d.dry_run)
      + "</div></div></div>"
      + '<div class="item"><div class="item-main"><div class="item-meta">'
      + "Отчёт живёт в памяти процесса (" + esc(d.storage || "в памяти процесса") + "): "
      + "после перезапуска здесь снова «нет данных»"
      + (d.recorded_at ? " · записан " + esc(dt(d.recorded_at)) : "")
      + "</div></div></div>";
  }

  async function loadReprintBackfill() {
    const root = $("#reprint-backfill");
    if (!root) return;
    try {
      renderReprintBackfill(await api("/api/sources/reprint-backfill"));
    } catch (error) {
      root.innerHTML = '<div class="empty">Статус бэкфилла репринтов недоступен: ' + esc(error?.message || error) + "</div>";
    }
  }

  async function explainTrend(clusterId) {
    const target = document.querySelector('[data-trend-explain-body="' + CSS.escape(clusterId) + '"]');
    if (!target) return;
    if (target.dataset.loaded === "1") {
      target.innerHTML = "";
      target.dataset.loaded = "0";
      return;
    }
    target.textContent = "Считаю…";
    try {
      const data = await api("/api/trends/" + encodeURIComponent(clusterId) + "/explain");
      const reasons = (data && data.reasons) || [];
      target.innerHTML = reasons.length
        ? reasons.map((item) => '<span class="trend-reason">' + esc(item.text || item.code) + "</span>").join("")
        : '<span class="trend-reason">Объяснение недоступно.</span>';
      target.dataset.loaded = "1";
    } catch (error) {
      target.innerHTML = '<span class="trend-reason">Не удалось получить объяснение: ' + esc(error?.message || error) + "</span>";
    }
  }

  /* ------------------------------------------------------- router skip report */
  // GET /api/router/skips/{cluster} answers out of S3's in-process registry: a
  // restart empties it and the LRU drops the oldest clusters. So "no record" is
  // rendered as absent data — never as "nothing was skipped".
  const routerSkipRow = (item) => `
    <div class="trend-reason trend-skip-row">
      <b>${esc(item.channel_id || "нет данных")}</b>
      <span>${esc(item.platform || "площадка не записана")}</span>
      <span class="platform-capability capability-${esc(item.capability)}">${esc(item.capability_label || item.capability || "нет данных")}</span>
      <span class="trend-skip-why">${esc(item.reason || "причина не записана")}</span>
    </div>`;

  function routerSkipsMemoryNote(data) {
    const limit = Number(data && data.registry_limit);
    const cap = Number.isFinite(limit) ? ` · лимит ${esc(limit)}` : "";
    return `<div class="trend-reason trend-skip-memory">реестр в памяти процесса${cap}: после перезапуска список пуст</div>`;
  }

  function renderRouterSkips(data) {
    const rows = (data && data.skipped) || [];
    const memory = routerSkipsMemoryNote(data);
    if (!data || data.remembered !== true) {
      return '<div class="trend-reason"><span class="tag tag-none">нет данных</span> '
        + esc((data && data.state_note) || "маршрутизатор не помнит этот кластер в текущем процессе")
        + "</div>" + memory;
    }
    if (!rows.length) {
      return '<div class="trend-reason">Ни одна площадка не пропущена: кластер роутился в этом процессе без исключений.</div>' + memory;
    }
    return rows.map(routerSkipRow).join("") + memory;
  }

  async function explainSkips(clusterId) {
    const target = document.querySelector('[data-trend-skips-body="' + CSS.escape(clusterId) + '"]');
    if (!target) return;
    if (target.dataset.loaded === "1") {
      target.innerHTML = "";
      target.dataset.loaded = "0";
      return;
    }
    target.textContent = "Считаю…";
    try {
      const data = await api("/api/router/skips/" + encodeURIComponent(clusterId));
      target.innerHTML = renderRouterSkips(data);
      target.dataset.loaded = "1";
    } catch (error) {
      // A failed answer is not cached as loaded: the next click retries it.
      target.innerHTML = '<div class="trend-reason"><span class="tag tag-none">нет данных</span> '
        + esc(error?.message || error)
        + "</div>";
    }
  }

  async function resetSourceCooldown(sourceId) {
    try {
      await post("/api/sources/" + encodeURIComponent(sourceId) + "/cooldown/reset", {});
      toast("Cooldown снят: " + sourceId, "ok");
      await loadSourcesHealth();
      refresh();
    } catch (error) {
      toast("Не удалось снять cooldown: " + (error?.message || error), "err");
    }
  }

  /* ------------------------------------------------ community rules history */
  // GET /api/community-policy/history returns append-only snapshots, newest
  // first. It stores `rules_hash` per snapshot but no "changed" flag, so the
  // moment the rules themselves moved is derived the same way the writer does:
  // the hash differs from the previous snapshot of the same destination.
  const POLICY_VERDICT_RU = {
    ok: ["правила подтверждены", "tag-ok"],
    stale: ["правила устарели", "tag-warn"],
    unknown: ["правила не прочитаны", "tag-err"],
    no_target: ["место не выбрано", "tag-none"],
  };

  function policyVerdictChip(status) {
    const key = String(status || "").trim().toLowerCase();
    const known = POLICY_VERDICT_RU[key];
    // An unrecognised verdict is printed verbatim, never mapped onto "ok".
    return known
      ? `<span class="tag ${known[1]}">${esc(known[0])}</span>`
      : `<span class="tag tag-none">вердикт: ${esc(key || "нет данных")}</span>`;
  }

  function renderPolicyHistory(root, items) {
    if (!items.length) {
      root.innerHTML = '<div class="empty">История недоступна: по этому месту ещё не сохранялось ни одной проверки правил.</div>';
      return;
    }
    root.innerHTML = items
      .map((row, index) => {
        const hash = String(row.rules_hash || "");
        const older = items[index + 1];
        const comparable = older
          && String(older.platform) === String(row.platform)
          && String(older.target_id) === String(row.target_id);
        let changeChip;
        if (comparable) {
          changeChip = hash !== String(older.rules_hash || "")
            ? '<span class="tag tag-warn">правила изменились</span>'
            : '<span class="tag tag-none">без изменений</span>';
        } else {
          // No older row in this slice: claiming "unchanged" would be a guess.
          changeChip = '<span class="tag tag-none">раньше этой записи история не спрашивалась</span>';
        }
        const requirements = (row.requirements || []).slice(0, 6);
        return '<div class="policy-history-row' + (comparable && hash !== String(older.rules_hash || "") ? " is-changed" : "") + '">'
          + '<div class="policy-history-head"><div class="item-main">'
          + '<div class="item-title">' + esc(row.target_name || row.target_id || "Место без названия")
          + " · " + esc(row.platform) + " · " + esc(row.target_type || "—") + "</div>"
          + '<div class="item-meta">проверка ' + esc(dt(row.checked_at))
          + " · правила " + (row.rules_available ? "прочитаны" : "не прочитаны")
          + " · хэш " + esc(hash ? hash.slice(0, 12) : "—")
          + " · уверенность " + Number(row.confidence || 0).toFixed(2) + "</div></div>"
          + '<div class="item-side">' + policyVerdictChip(row.verdict_status) + changeChip + "</div></div>"
          + (requirements.length
            ? `<details class="policy-history-rules"><summary>Что требовали правила (${requirements.length} из ${(row.requirements || []).length})</summary>`
              + `<ul>${requirements.map((line) => `<li>${esc(line)}</li>`).join("")}</ul></details>`
            : '<div class="item-meta">В этой записи не сохранено ни одного требования.</div>')
          + "</div>";
      })
      .join("");
  }

  async function loadPolicyHistory(root, platform, targetId) {
    root.textContent = "Читаю историю правил…";
    delete root.dataset.loaded;
    try {
      const params = new URLSearchParams();
      if (platform) params.set("platform", platform);
      if (targetId) params.set("target_id", targetId);
      params.set("limit", "30");
      const data = await api("/api/community-policy/history?" + params.toString());
      const items = (data && data.items) || [];
      renderPolicyHistory(root, items);
      // Only a real answer counts as loaded: after "история недоступна" the next
      // click must retry the request instead of hiding the message.
      if (items.length) root.dataset.loaded = "1";
    } catch (error) {
      root.innerHTML = '<div class="empty">История недоступна: ' + esc(error?.message || error) + "</div>";
      delete root.dataset.loaded;
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
      scheduled: "есть отложенные публикации",
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
          : (item.state === "working" || item.state === "scheduled") ? "tag-acc" : "";
        return `<div class="delivery-health-platform">
          <strong>${esc(PLATFORM_RU[item.platform] || item.platform)}</strong>
          <span class="tag ${tone}">${esc(label)}</span>
          <span class="delivery-health-hint">${n(item.posted)} опубликовано · ${n(item.scheduled || 0)} отложено · ${n(item.unknown)} сверка · ${n(item.permanent)} постоянно</span>
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
      scheduled: "отложен на площадке",
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
      scheduled: "tag-acc",
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
        return statuses.some((status) => ["scheduled", "publishing", "retry_authorized", "failed"].includes(status));
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
      const scheduledCount = summary.scheduled || 0;
      const allSkipped = rows.length > 0 && rows.every((row) => row.status === "skipped");
      const overall = needsAttention ? "нужно внимание"
        : scheduledCount ? "есть отложенные публикации"
        : activeCount ? "доставка идёт"
        : successCount ? "есть результат"
        : allSkipped ? "не настроено"
        : "ожидает доставки";
      const overallClass = needsAttention ? "tag-warn"
        : (scheduledCount || activeCount) ? "tag-acc"
        : (successCount ? "tag-ok" : "");
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

  // The run-level chip of the journal. It used to be two states and a cancelled
  // run fell into "ок" — the exact opposite of what `f038373` added the outcome
  // for, and a different lie from the pipeline strip's red: here the cancellation
  // was reported as success. Three states now, and a run that both failed and was
  // cut short still reports the failure first, because that is the one the
  // operator has to act on.
  const runOutcomeChip = (stages) => {
    if (stages.some((s) => s.status === "error")) {
      return '<span class="tag tag-warn">с ошибками</span>';
    }
    if (stages.some((s) => s.status === "cancelled")) {
      return '<span class="tag tag-cancelled">прерван</span>';
    }
    return '<span class="tag tag-ok">ок</span>';
  };

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
          <div class="item-meta">${stages.map((s) => `${esc(s.stage.replace(/^s\d_/, ""))}:${typeof s.out === "number" ? n(s.out) : "—"}`).join("  ")}</div>
        </div>
        <div class="item-side">${runOutcomeChip(stages)}</div>
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
    const agentRoles = q.agent_team && q.agent_team.roles && typeof q.agent_team.roles === "object"
      ? q.agent_team.roles : {};
    const agentRoleTitles = {
      topic_router: "Маршрутизатор тем",
      format_planner: "Планировщик формата",
      editorial_strategist: "Редакционный стратег",
      writer: "Автор",
      fact_checker: "Фактчекер",
      review_board: "Коллегия качества",
      platform_editor: "Нативный редактор площадки",
    };
    const agentStatuses = {
      planned: "План готов",
      skipped: "Пропущен",
      fallback: "Резервный план",
      completed: "Завершено",
      passed: "Принято",
      rejected: "Отклонено",
      ai_native: "AI-редактура готова",
      deterministic: "Базовая адаптация",
      deterministic_guardrail: "Guardrail: оставлен исходник",
      deterministic_fallback: "Fallback: оставлен исходник",
      special_workflow: "Отдельный workflow",
      blocked_missing_media: "Нет нужного медиа",
      unknown: "Статус неизвестен",
    };
    const agentList = (label, values) => {
      const items = Array.isArray(values)
        ? values.filter((value) => String(value || "").trim()).slice(0, 6) : [];
      return items.length
        ? `<div class="agent-role-list"><strong>${esc(label)}</strong><ul class="drawer-issues">${items.map((value) => `<li>${esc(value)}</li>`).join("")}</ul></div>`
        : "";
    };
    const agentRoleHtml = ([key, role]) => {
      if (!role || typeof role !== "object") return "";
      const rows = [];
      const add = (label, value) => {
        if (value === null || value === undefined || String(value).trim() === "") return;
        rows.push(kv(label, String(value)));
      };
      const scoreText = (value) => value !== null && value !== undefined && Number.isFinite(Number(value))
        ? pct(Number(value)) : "—";
      let extra = "";
      if (key === "topic_router") {
        add("Угол", role.angle);
        add("Соответствие", scoreText(role.score));
        add("Обоснование", role.reason);
      } else if (key === "format_planner") {
        add("Формат", role.format);
        add("Обоснование", role.reason);
      } else if (key === "editorial_strategist") {
        add("Польза читателю", role.reader_value);
        add("Центральный вопрос", role.core_question);
        add("Идея лида", role.lead_idea);
        add("Модель", [role.provider, role.model].filter(Boolean).join(" · "));
        add("Причина fallback", role.reason);
        extra = agentList("План раскрытия", role.outline) + agentList("Обязательные оговорки", role.caveats);
      } else if (key === "platform_editor") {
        const history = Array.isArray(role.history) ? role.history : [];
        add("Площадка", PLATFORM_RU[role.platform] || role.platform);
        add("Формат", role.format);
        add("Тип адресата", role.target_type);
        add("Адресат", role.destination);
        add("AI использован", role.used_ai ? "да" : "нет");
        add("Модель", [role.provider, role.model].filter(Boolean).join(" · "));
        add("Причина / результат", role.reason);
        add("Записей в истории", history.length ? `${history.length} из 10 последних` : "1");
        add("Обновлено", dt(role.updated_at));
        if (history.length > 1) {
          extra = agentList(
            "Последние решения",
            history.slice(-5).reverse().map((item) => [
              dt(item.updated_at),
              agentStatuses[item.status] || item.status || "—",
              item.model || item.provider || "",
              item.reason || "",
            ].filter(Boolean).join(" · "))
          );
        }
      } else if (key === "writer") {
        add("Провайдер", role.provider);
      } else if (key === "fact_checker") {
        add("Проверяемых утверждений", n(role.claim_count));
        add("Подтверждено источниками", n(role.verified_count));
      } else if (key === "review_board") {
        add("Гейт", role.gate);
        add("Причина", role.reason);
        add("Переписываний", n(role.rewrites_used));
        const reviewScores = Object.entries(role.scores || {}).slice(0, 8);
        if (reviewScores.length) {
          extra += `<div class="kv">${reviewScores.map(([name, value]) =>
            kv(name, Number.isFinite(Number(value)) ? (name === "tone" ? (Number(value) * 10).toFixed(1) + "/10" : scoreText(value)) : String(value))
          ).join("")}</div>`;
        }
        extra += agentList("Замечания", role.issues);
      }
      return `<div class="agent-role">
        <div class="agent-role-head">
          <strong>${esc(agentRoleTitles[key] || key)}</strong>
          <span class="tag">${esc(agentStatuses[role.status] || role.status || "—")}</span>
        </div>
        ${rows.length ? `<div class="kv">${rows.join("")}</div>` : ""}
        ${extra}
      </div>`;
    };
    const platformEdits = q.agent_team && q.agent_team.platform_edits
      && typeof q.agent_team.platform_edits === "object" ? q.agent_team.platform_edits : {};
    const platformEditorRoles = Object.entries(platformEdits).map(([platform, trace]) => [
      "platform_editor",
      { ...(trace || {}), platform },
    ]);
    const agentEntries = [
      ...Object.entries(agentRoles),
      ...platformEditorRoles,
    ];
    const agentTrace = agentEntries.length
      ? `<div class="drawer-block agent-team"><h4>Команда ИИ · история решений</h4>
          <div class="agent-team-grid">${agentEntries.map(agentRoleHtml).join("")}</div>
        </div>`
      : "";
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

      ${p.status === "published" ? `<div class="drawer-block short-video-tools">
        <h4>Короткие видео · Shorts / TikTok / VK Клипы</h4>
        <p class="drawer-meta">Ферма собирает оригинальный вертикальный MP4 из текста публикации. YouTube Shorts могут уходить через настроенный live-конвейер. TikTok требует отдельного просмотра и подтверждения для каждого видео.</p>
        <div class="short-video-actions">
          <button class="btn btn-primary btn-sm" type="button" onclick="tgfarmRenderShort('${esc(p.id)}')" ${state.shortVideoBusy.has(p.id) ? "disabled" : ""}>${p.video ? "Пересобрать MP4" : "Создать вертикальное видео"}</button>
          <button class="btn btn-ghost btn-sm" type="button" onclick="tgfarmPrepareTikTok('${esc(p.id)}')">Подготовить TikTok</button>
          ${p.video ? `<a class="btn btn-ghost btn-sm" href="/api/media/${encodeURIComponent(p.video.id)}" download="tgfarm-short-${esc(p.id)}.mp4">Скачать MP4 для VK Клипов</a>` : ""}
          <a class="btn btn-ghost btn-sm" href="https://vk.com/clips" target="_blank" rel="noopener noreferrer">Открыть VK Клипы ↗</a>
        </div>
        <p class="drawer-meta">VK Клипы: TgFarm подготавливает MP4 и передаёт его для ручной загрузки. Не выдаём обычный пост VK за публикацию в ленте Клипов.</p>
      </div>` : ""}

      <div class="drawer-block"><h4>Текст</h4><div class="drawer-text">${esc(p.text)}</div></div>

      <div class="drawer-block short-video-tools">
        <h4>Нативная версия для любой площадки</h4>
        <p class="drawer-meta">TgFarm учитывает аудиторию, формат, адресата и редакционные ограничения. Для Reddit, vc.ru и Пикабу дополнительно проверяет доступные правила. Это только предпросмотр, публикация не запускается.</p>
        <div class="short-video-actions">
          <select id="editorial-preview-platform-${esc(p.id)}" aria-label="Площадка для нативной версии">
            ${(state.platformData?.platforms || []).map((item) => `<option value="${esc(item.id)}">${esc(item.name)} · ${esc(item.supported ? "адаптер есть" : "ручной режим")}</option>`).join("")}
          </select>
          <button class="btn btn-primary btn-sm" type="button" onclick="tgfarmBuildEditorialPreview('${esc(p.id)}')">Собрать нативную версию</button>
        </div>
        <div class="community-target-result" id="editorial-preview-result-${esc(p.id)}"></div>
      </div>

      <div class="drawer-block short-video-tools">
        <h4>Подготовить публикацию · vc.ru / Пикабу</h4>
        <p class="drawer-meta">Сначала TgFarm может подобрать конкретную тему vc.ru или сообщество Пикабу, прочитать их публичное описание и правила, а затем заполнить редактор. Автопубликация через API не подтверждена: черновик не будет отправлен без твоего участия.</p>
        <div class="short-video-actions">
          <button class="btn btn-primary btn-sm" type="button" onclick="tgfarmRecommendCommunityTarget('${esc(p.id)}','vc_ru')">Подобрать тему vc.ru</button>
          <button class="btn btn-ghost btn-sm" type="button" onclick="tgfarmRecommendCommunityTarget('${esc(p.id)}','pikabu')">Найти сообщество Пикабу</button>
        </div>
        <div class="community-target-result" id="community-target-result-${esc(p.id)}"></div>
        <div class="short-video-actions">
          <button class="btn btn-primary btn-sm" type="button" onclick="tgfarmPrepareCommunityDraft('${esc(p.id)}','vc_ru')">Подготовить для vc.ru</button>
          <button class="btn btn-ghost btn-sm" type="button" onclick="tgfarmPrepareCommunityDraft('${esc(p.id)}','pikabu')">Подготовить для Пикабу</button>
        </div>
      </div>

      ${p.status === "published" ? `<div class="drawer-block short-video-tools">
        <h4>Короткие видео · Shorts / TikTok / VK Клипы</h4>
        <p class="drawer-meta">Из текущего поста можно собрать оригинальный вертикальный MP4. YouTube загружает Shorts автоматически только при включённой площадке и live-режиме. TikTok всегда просит подтверждение перед каждой отправкой.</p>
        <div class="short-video-actions">
          <button class="btn btn-primary btn-sm" type="button" onclick="tgfarmRenderShort('${esc(p.id)}')" ${state.shortVideoBusy.has(p.id) ? "disabled" : ""}>${p.video ? "Пересобрать MP4" : "Создать вертикальное видео"}</button>
          <button class="btn btn-ghost btn-sm" type="button" onclick="tgfarmPrepareTikTok('${esc(p.id)}')">Подготовить TikTok</button>
          ${p.video ? `<a class="btn btn-ghost btn-sm" href="/api/media/${encodeURIComponent(p.video.id)}" download="tgfarm-short-${esc(p.id)}.mp4">Скачать MP4 для VK Клипов</a>` : ""}
          <a class="btn btn-ghost btn-sm" href="https://vk.com/clips" target="_blank" rel="noopener noreferrer">Открыть VK Клипы ↗</a>
        </div>
        <p class="drawer-meta">VK Клипы: TgFarm готовит MP4 и открывает официальный экран загрузки. Прямую API-публикацию именно в ленту Клипов не заявляем, пока не подтверждён отдельный поддерживаемый endpoint.</p>
      </div>` : ""}

      ${p.format_reason ? `<div class="drawer-block"><h4>Почему такой формат</h4>
        <div class="drawer-text">${esc(p.format_reason)}</div></div>` : ""}

      ${p.external_url ? `<div class="drawer-block"><h4>Публикация</h4>
        <a href="${esc(p.external_url)}" target="_blank" rel="noopener">${esc(p.external_url)}</a></div>` : ""}

      ${agentTrace}
      ${scores.length ? `<div class="drawer-block"><h4>Гейты качества · ${esc(q.reason || "")}</h4>
        <div class="kv">${scores.map(([k, v]) => kv(k, (v >= 0.99 ? "✓ " : "") + (k === "tone" ? (v * 10).toFixed(1) + "/10" : pct(v)))).join("")}</div>
      </div>` : ""}
      ${q.issues && q.issues.length ? `<div class="drawer-block"><h4>Замечания критика</h4>
        <ul class="drawer-issues">${q.issues.map((i) => `<li>${esc(i)}</li>`).join("")}</ul></div>` : ""}

      <div class="drawer-block short-video-tools">
        <h4>Ручная подготовка для остальных площадок</h4>
        <p class="drawer-meta">TgFarm адаптирует заголовок, текст, теги и медиа. Этот режим ничего не публикует: проверь пакет и отправь его через официальный редактор площадки.</p>
        <div class="short-video-actions">
          <select id="manual-platform-${esc(p.id)}" aria-label="Площадка для ручной подготовки">
            <option value="vk_clips">VK Клипы</option>
            <option value="rutube">RUTUBE</option>
            <option value="vc_ru">vc.ru</option>
            <option value="habr">Хабр</option>
            <option value="pikabu">Пикабу</option>
            <option value="tenchat">TenChat</option>
            <option value="medium">Medium</option>
            <option value="whatsapp_channels">WhatsApp Channels</option>
            <option value="twitch">Twitch</option>
            <option value="snapchat">Snapchat</option>
            <option value="wechat">WeChat / Weixin</option>
            <option value="douyin">Douyin</option>
            <option value="rednote">RedNote / Xiaohongshu</option>
          </select>
          <button class="btn btn-primary btn-sm" type="button" onclick="tgfarmPrepareManualDraft('${esc(p.id)}')">Подготовить пакет</button>
        </div>
        <div class="manual-draft-result" id="manual-draft-result-${esc(p.id)}"></div>
      </div>

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

  function setTikTokPublishStatus(message, kind = "") {
    const element = $("#tiktok-publish-status");
    if (!element) return;
    element.textContent = message || "";
    element.className = "platform-auth-message" + (kind ? " is-" + kind : "");
  }

  const TIKTOK_PRIVACY_LABELS = {
    PUBLIC_TO_EVERYONE: "Все",
    MUTUAL_FOLLOW_FRIENDS: "Взаимные подписчики",
    FOLLOWER_OF_CREATOR: "Подписчики",
    SELF_ONLY: "Только я",
  };

  function updateTikTokPublishControls() {
    const consent = $("#tiktok-publish-consent")?.checked === true;
    const caption = $("#tiktok-publish-caption")?.value || "";
    const privacy = $("#tiktok-publish-privacy")?.value || "";
    const count = $("#tiktok-caption-count");
    if (count) {
      count.textContent = `${caption.length} / 2200 UTF-16`;
      count.classList.toggle("is-over-limit", caption.length > 2200);
    }
    const submit = $("#tiktok-publish-submit");
    if (submit) {
      const frozen = ["posted", "unknown", "permanent", "failed"].includes(state.tiktokResultStatus);
      submit.disabled = state.tiktokBusy || frozen || !consent || caption.length === 0 || caption.length > 2200
        || !privacy || state.tiktokPrepared?.live !== true;
      submit.textContent = state.tiktokBusy ? "Отправляю…" : frozen
        ? (state.tiktokResultStatus === "posted" ? "Публикация подтверждена" : "Проверить результат вручную")
        : "Опубликовать сейчас";
    }
  }

  function closeTikTokPublishDialog() {
    const dialog = $("#tiktok-publish-dialog");
    if (state.tiktokBusy) {
      setTikTokPublishStatus("Отправка выполняется. Не запускай повторную публикацию; дождись результата.", "warn");
      return;
    }
    if (dialog?.open) dialog.close();
    const video = $("#tiktok-publish-video");
    if (video) {
      video.pause();
      video.removeAttribute("src");
      video.load();
    }
    state.tiktokPostId = "";
    state.tiktokPrepared = null;
    state.tiktokResultStatus = "";
  }

  window.tgfarmRenderShort = async (postId) => {
    if (!postId || state.shortVideoBusy.has(postId)) return;
    state.shortVideoBusy.add(postId);
    try {
      const result = await post(`/api/posts/${encodeURIComponent(postId)}/video/render`, {});
      toast(
        `Вертикальный MP4 готов: ${Math.round(result.artifact.duration_s)} с · ${n(result.artifact.bytes)} байт`,
        "ok",
      );
      await openDrawer(postId);
      await refresh();
    } catch (error) {
      toast("Не удалось создать видео: " + (error?.message || error), "err");
    } finally {
      state.shortVideoBusy.delete(postId);
      // Re-render the drawer after clearing the busy flag so the primary
      // action is immediately usable again without requiring a page refresh.
      if (!$("#drawer").hidden) await openDrawer(postId);
    }
  };

  window.tgfarmPrepareTikTok = async (postId) => {
    if (!postId || state.tiktokBusy) return;
    state.tiktokPostId = postId;
    state.tiktokPrepared = null;
    state.tiktokResultStatus = "";
    const dialog = $("#tiktok-publish-dialog");
    $("#tiktok-publish-title").textContent = "Подготовка TikTok · " + postId;
    $("#tiktok-publish-account").textContent = "Проверяю OAuth, видео и свежие настройки приватности…";
    $("#tiktok-publish-warning").textContent = "";
    $("#tiktok-publish-permissions").textContent = "";
    $("#tiktok-publish-caption").value = "";
    $("#tiktok-publish-consent").checked = false;
    $("#tiktok-publish-privacy").innerHTML = "";
    $("#tiktok-publish-download").hidden = true;
    $("#tiktok-publish-video-fallback").hidden = true;
    setTikTokPublishStatus("Подготавливаю видео. Публикация не начнётся без отдельного подтверждения.");
    updateTikTokPublishControls();
    if (!dialog.open) dialog.showModal();
    try {
      const prepared = await post(`/api/posts/${encodeURIComponent(postId)}/tiktok/prepare`, {});
      state.tiktokPrepared = prepared;
      const creator = prepared.creator || {};
      const username = creator.username ? "@" + creator.username : (creator.nickname || "TikTok creator");
      $("#tiktok-publish-account").textContent = "Аккаунт: " + username;
      $("#tiktok-publish-warning").textContent = prepared.warning || "";
      const video = $("#tiktok-publish-video");
      const mediaUrl = typeof prepared.download_url === "string" && prepared.download_url.startsWith("/api/media/")
        ? prepared.download_url : "";
      if (video && mediaUrl) {
        video.src = mediaUrl;
        video.load();
      } else {
        $("#tiktok-publish-video-fallback").hidden = false;
      }
      const download = $("#tiktok-publish-download");
      if (download && mediaUrl) {
        download.href = mediaUrl;
        download.hidden = false;
      }
      const select = $("#tiktok-publish-privacy");
      const options = Array.isArray(creator.privacy_level_options) ? creator.privacy_level_options : [];
      select.innerHTML = options.map((value) =>
        `<option value="${esc(value)}">${esc(TIKTOK_PRIVACY_LABELS[value] || value)}</option>`
      ).join("");
      if (prepared.live && options.includes("PUBLIC_TO_EVERYONE")) {
        select.value = "PUBLIC_TO_EVERYONE";
      } else if (options.includes("SELF_ONLY")) {
        select.value = "SELF_ONLY";
      } else if (options.length) {
        select.value = options[0];
      }
      const controls = [];
      if (creator.comment_disabled) controls.push("Комментарии закрыты настройками аккаунта.");
      if (creator.duet_disabled) controls.push("Duet недоступен.");
      if (creator.stitch_disabled) controls.push("Stitch недоступен.");
      if (creator.max_video_post_duration_sec) controls.push("Максимальная длительность аккаунта: " + creator.max_video_post_duration_sec + " с.");
      $("#tiktok-publish-permissions").textContent = controls.join(" ");
      $("#tiktok-publish-caption").value = prepared.caption || "";
      $("#tiktok-publish-consent").checked = false;
      setTikTokPublishStatus(
        prepared.live
          ? "Проверь превью, подпись и приватность. Только после отметки подтверждения начнётся отправка."
          : "Режим не live: можно подготовить и просмотреть видео, но публикация заблокирована.",
        prepared.live ? "" : "warn",
      );
      updateTikTokPublishControls();
    } catch (error) {
      state.tiktokPrepared = null;
      setTikTokPublishStatus("Подготовка не удалась: " + (error?.message || error), "err");
      updateTikTokPublishControls();
    }
  };

  async function submitTikTokPublish(event) {
    event?.preventDefault();
    if (state.tiktokBusy || !state.tiktokPostId || !state.tiktokPrepared) return;
    if (!$("#tiktok-publish-consent")?.checked) {
      setTikTokPublishStatus("Поставь отметку подтверждения именно для этого видео.", "err");
      return;
    }
    const caption = $("#tiktok-publish-caption").value || "";
    const privacy = $("#tiktok-publish-privacy").value || "";
    if (!caption.trim() || caption.length > 2200 || !privacy) {
      setTikTokPublishStatus("Проверь подпись (не больше 2200 UTF-16 units) и приватность.", "err");
      return;
    }
    const postId = state.tiktokPostId;
    state.tiktokBusy = true;
    const close = $("#tiktok-publish-close");
    const cancel = $("#tiktok-publish-cancel");
    if (close) close.disabled = true;
    if (cancel) cancel.disabled = true;
    setTikTokPublishStatus("Отправляю видео через TikTok Content Posting API…");
    updateTikTokPublishControls();
    try {
      const result = await post(`/api/posts/${encodeURIComponent(postId)}/tiktok/publish`, {
        caption,
        privacy_level: privacy,
        consent: true,
      });
      state.tiktokResultStatus = result.status || "unknown";
      if (result.status === "posted") {
        const url = safeExternalUrl(result.url);
        setTikTokPublishStatus(
          (result.detail || "TikTok подтвердил публикацию.")
          + (url ? " Ссылка: " + url : " ID: " + (result.remote_id || "не предоставлен")),
          "ok",
        );
        toast("TikTok подтвердил публикацию", "ok");
      } else if (result.status === "unknown") {
        setTikTokPublishStatus(
          "Результат не подтверждён. Не запускай повтор. Проверь профиль TikTok; запись доставки сохранена для сверки.",
          "warn",
        );
        toast("TikTok: результат требует сверки, автоматический повтор заблокирован", "err");
      } else {
        setTikTokPublishStatus(result.error || result.detail || "TikTok не подтвердил публикацию.", "err");
        toast("TikTok не подтвердил публикацию", "err");
      }
      await refresh();
      updateTikTokPublishControls();
    } catch (error) {
      setTikTokPublishStatus("Ошибка запроса: " + (error?.message || error), "err");
    } finally {
      state.tiktokBusy = false;
      if (close) close.disabled = false;
      if (cancel) cancel.disabled = false;
      updateTikTokPublishControls();
    }
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
      const counter = $("#review-nav-count");
      if (counter) counter.textContent = "0";
      return;
    }
    if (!box) {
      box = document.createElement("div");
      box.id = "alert-banner";
      kpis.parentElement.insertBefore(box, kpis);
    }
    const errors = alerts.filter((a) => a.level === "error").length;
    const counter = $("#review-nav-count");
    if (counter) counter.textContent = String(alerts.length);
    const shown = state.alertsExpanded ? alerts : alerts.slice(0, 3);
    box.className = "attention-panel";
    box.innerHTML =
      '<div class="attention-head"><div><strong>Требует внимания</strong>'
      + '<span class="attention-count">' + alerts.length + "</span>"
      + (errors ? '<span class="attention-errors">' + errors + " критичных</span>" : "") + "</div>"
      + '<button class="btn btn-ghost btn-sm" type="button" id="attention-toggle">'
      + (state.alertsExpanded ? "Свернуть" : "Показать все") + "</button></div>"
      + '<div class="attention-list">'
      + shown.map((a) =>
          '<div class="attention-row attention-row--' + (a.level === "error" ? "err" : "warn") + '">'
          + '<span class="attention-dot"></span>'
          + '<div class="attention-text"><strong>' + esc(a.code) + "</strong>"
          + "<span>" + esc(String(a.message || "").slice(0, 220)) + "</span></div></div>").join("")
      + "</div>";
    const toggle = $("#attention-toggle");
    if (toggle) {
      toggle.addEventListener("click", () => {
        state.alertsExpanded = !state.alertsExpanded;
        renderAlerts(alerts);
      });
    }
  }

  // «Что вообще можно было измерить» — второй вопрос к тому же покрытию.
  // Карта выше отвечает, сколько публикаций имеют снимок; она молчит, сколько
  // из них могли бы иметь вообще. Без этого разделения 0 % покрытия из-за
  // отсутствующего `tg_message_id` и 0 % из-за сборщика, который ни разу не
  // запустился, выглядят на экране одинаково — а это два разных дефекта у
  // разных владельцев. `/api/metrics/attribution` (`s8_analytics/attribution.py`)
  // приносит `unknowable` — сколько публикаций вообще нет идентификатора, за
  // который сборщик смог бы зацепить число, и `coverage`, который равен None
  // (никогда не 0), когда делить не на что. Последнее — почему доля идёт через
  // `rate()`, а не через `pct()`: `pct(null)` печатает ровно то нулевое «0,0%»,
  // которое модуль отказался отправлять.
  //
  // Блок живет здесь, а не между `renderMetricsCoverage` и
  // `renderMetricsFormats`, потому что оба коридора зарезаны шпурками формы
  // (`tests/test_ui_coverage_panel.py::_renderer_body`,
  // `tests/test_ui_evidence_and_cancellation.py::_coverage_body`): новая функция
  // внутри них бы обвинила чужой эндпоинт в отсутствующих полях.
  const ATTRIBUTION_GAP = {
    no_id: { cls: "tag-err", label: "нет внешнего id — измерить нельзя" },
    no_stats: { cls: "tag-warn", label: "id есть, снимков нет" },
    partly: { cls: "tag-ok", label: "часть публикаций измерима" },
    nothing: { cls: "tag-none", label: "публикаций нет" },
  };

  // Контейнер этой карты создаётся здесь, а не в `index.html`: этот
  // пакет владеет только скриптом. Тот же приём, что у `#alert-banner` в
  // `renderAlerts` — нода ищется, отсутствующий вставляется, повторно
  // проход рендера ничего не дублирует.
  function attributionRoot() {
    const existing = $("#metrics-attribution");
    if (existing) return existing;
    const anchor = $("#metrics-coverage-dzen") || $("#metrics-coverage");
    if (!anchor || !anchor.parentElement || !anchor.insertAdjacentElement) return null;
    const node = document.createElement("div");
    node.id = "metrics-attribution";
    node.className = "coverage-attribution";
    anchor.insertAdjacentElement("afterend", node);
    return node;
  }

  const attributionWindow = (v) =>
    (typeof v === "number" ? n(v) + " дн." : "не прочитано");

  function renderMetricsAttribution(payload, failure) {
    const root = attributionRoot();
    if (!root) return;
    // Три тишины — три формулировки, как в карте очереди. Эндпоинт
    // здесь не возвращает ключа `error` (`api.py::io` не ловит исключение,
    // так что падение чтения = HTTP 500 = `failure`), и на этом пути не
    // печатается ни одна цифра: итоги пустого отчёта — нули, а нуль
    // здесь звучит как «измерять нечего», хотя ответа вообще не было.
    if (failure) {
      const message = String(failure.message || failure);
      root.innerHTML = '<div class="alert alert-err"><b>Атрибуция метрик не прочитана:</b> '
        + esc(message.slice(0, 160)) + "</div>"
        + '<div class="empty">Ни одной цифры не показано: вместо них был бы ноль.</div>';
      return;
    }
    const data = payload || {};
    const rows = Object.entries(data.platforms || {});
    // Пустой отчёт — не нулевое покрытие: это отсутствие предмета.
    if (!rows.length) {
      root.innerHTML = '<div class="item-meta"><b>Что можно было измерить</b>: '
        + "ни одной публикации в окне — это не «0 % измерено», это "
        + "отсутствие предмета.</div>";
      return;
    }
    const explained = '<div class="item-meta"><b>Что можно было измерить</b>: у '
      + "какой доли публикаций вообще есть внешний id, за который "
      + "сборщик зацепил бы снимок — 0 % без id и 0 % с id чинятся "
      + "по-разному.</div>";
    const body = rows
      .slice()
      .sort((a, b) => Number(b[1].publications || 0) - Number(a[1].publications || 0))
      .map(([platform, row]) => {
        const gap = Number(row.publications || 0) === 0
          ? ATTRIBUTION_GAP.nothing
          : (Number(row.unknowable || 0) === Number(row.publications || 0)
            ? ATTRIBUTION_GAP.no_id
            : (Number(row.with_stats || 0) === 0
              ? ATTRIBUTION_GAP.no_stats
              : ATTRIBUTION_GAP.partly));
        // Две разные поломки на одной строке: доля и сколько
        // публикаций без идентификатора. Они рядом не случайно:
        // по одной только доле их не различить.
        const verdict = "измеримо " + rate(row.coverage)
          + " · не привязываемо " + n(row.unknowable)
          + " из " + n(row.publications);
        return '<div class="item"><div class="item-main"><div class="item-title">'
          + esc(platform) + "</div>"
          + '<div class="item-meta">' + verdict + "</div>"
          + '<div class="item-meta">публикаций ' + n(row.publications)
          + " · с id " + n(row.with_external_id)
          + " · со снимком " + n(row.with_stats) + "</div>"
          + '<div class="item-meta">последний замер '
          + esc(dt(row.last_measurement_at)) + "</div>"
          + '</div><div class="item-side"><span class="tag ' + gap.cls + '">'
          + esc(gap.label) + "</span></div></div>";
      })
      .join("");
    // `orphan_measurements` — строки метрик, не привязанные ни к одной
    // публикации: они не двигают ничьё покрытие, но доказывают, что
    // сборщик хоть раз работал. `items()` и проверка типа — потому что
    // поля может не быть в ответе старой сборки, а отсутствие — не ноль.
    root.innerHTML = explained + body
      + '<div class="item-meta">строк метрик не от публикаций: '
      + esc(items(data.orphan_measurements, ""))
      + " · окно " + attributionWindow(data.window_days) + ".</div>";
  }

  async function refresh() {
    if (state.inFlight) return;
    state.inFlight = true;
    try {
      const endpoints = {
        status: "/api/status",
        report: "/api/report?days=14",
        runs: "/api/runs?limit=40",
        trends: "/api/trends?limit=25",
        posts: `/api/posts?limit=40${state.postFilter ? "&status=" + state.postFilter : ""}`,
        channels: "/api/channels",
        sources: "/api/sources",
        dzen: "/api/dzen/status",
        crossposts: "/api/crossposts?limit=100",
        deliveryHealth: "/api/crossposts/health",
        memory: `/api/memory?limit=20${state.memoryQuery ? "&query=" + encodeURIComponent(state.memoryQuery) : ""}`,
        platforms: "/api/platforms",
        redditMetrics: "/api/reddit/metrics",
        aiUsage: "/api/ai/usage?days=7",
        metricsFreshness: "/api/metrics/freshness",
        metricsCoverage: "/api/metrics/coverage",
        metricsAttribution: "/api/metrics/attribution?days=30",
        backlog: "/api/backlog",
        metricsFormats: "/api/metrics/formats?days=30",
        distributionPlan: "/api/distribution/plan",
        agentBenchmark: "/api/agents/benchmark",
      };
      // One failing endpoint must not kill the whole dashboard: degraded
      // sections keep their previous content and are reported in a toast.
      const settled = await Promise.allSettled(Object.values(endpoints).map((path) => api(path)));
      const data = {};
      const failed = [];
      const reasons = {};
      Object.keys(endpoints).forEach((key, index) => {
        const outcome = settled[index];
        if (outcome.status === "fulfilled") data[key] = outcome.value;
        else {
          failed.push(key);
          reasons[key] = outcome.reason;
        }
      });
      // The publication card answers the one question the whole dashboard
      // exists for, so it is rendered before the all-failed throw below: when
      // every endpoint is down the other sections keep their stale silence,
      // but this one must say «сеть не отвечает» instead of freezing on an old
      // verdict — and a failed read is re-rendered every pass, never cached.
      renderPublicationDecision("status" in data ? data.status : null,
        "status" in data ? null : reasons.status || new Error("статус не получен"));
      // Same rule as the publication card above: a read that died must be said
      // out loud on every pass, not left showing the previous run's attribution as
      // if it were still current. The numbers ride on `/api/report`'s `dzen` block
      // (`metrics_status()`), which the refresh already fetches for the report.
      renderDzenAttribution(
        "report" in data ? (data.report || {}).dzen : null,
        "report" in data ? null : reasons.report || new Error("отчёт фермы не получен"));
      // Same rule as the publication card above: the queue read is re-rendered on
      // every pass, including the pass where it died. Leaving the previous run's
      // backlog on screen would report a queue that has since moved.
      renderBacklog("backlog" in data ? data.backlog : null,
        "backlog" in data ? null : reasons.backlog || new Error("очередь не получена"));
      // The worklist rides on the very same snapshot as the count above it, so the
      // two can never disagree, and it re-renders on the pass where the read died
      // too — an old list of rows that have since been reconciled would be a lie.
      renderUnresolvedWorklist("backlog" in data ? data.backlog : null,
        "backlog" in data ? null : reasons.backlog || new Error("очередь не получена"));
      if (failed.length === Object.keys(endpoints).length) throw new Error("API недоступен");
      if (failed.length) toast("Часть разделов не обновилась: " + failed.join(", "), "warn");

      if ("status" in data) {
        const mode = data.status.mode || "?";
        const pill = $("#mode-pill");
        pill.textContent = mode === "live" ? "LIVE" : mode === "dry" ? "DRY-RUN" : "OFF";
        pill.className = "pill " + (mode === "live" ? "pill-ok" : mode === "dry" ? "pill-warn" : "pill-err");
        const farmPill = $("#farm-pill");
        if (farmPill) {
          farmPill.textContent = data.status.scheduler_running ? "Ферма: работает" : "Ферма: остановлена";
          farmPill.className = "pill " + (data.status.scheduler_running ? "pill-ok" : "pill-err");
        }
        $("#brand-sub").textContent =
          `${data.status.channels} каналов · ${data.status.sources} источников · ${data.status.embedder}`;
        renderAlerts(data.status.alerts || []);
        renderKPIs(data.status.kpis || data.status);
      }
      if ("dzen" in data) {
        state.dzenStatus = data.dzen;
        renderDzen(data.dzen);
      }
      if ("report" in data) renderReport(data.report);
      if ("metricsFreshness" in data) renderMetricsFreshness(data.metricsFreshness);
      if ("metricsCoverage" in data) renderMetricsCoverage(data.metricsCoverage);
      // Перерисовывается и на том проходе, где чтение умерло:
      // старая атрибуция на экране отчиталась бы о разделе,
      // которого больше нет.
      renderMetricsAttribution("metricsAttribution" in data ? data.metricsAttribution : null,
        "metricsAttribution" in data ? null
          : reasons.metricsAttribution || new Error("атрибуция не получена"));
      if ("metricsFormats" in data) renderMetricsFormats(data.metricsFormats);
      if ("runs" in data) {
        renderPipeline(data.runs.runs || []);
        renderRuns(data.runs.runs || []);
      }
      if ("trends" in data) renderTrends(data.trends.trends || []);
      if ("posts" in data) renderPosts(data.posts.posts || []);
      if ("channels" in data) renderChannels(data.channels.channels || []);
      if ("sources" in data) renderSources(data.sources.sources || []);
      if ("crossposts" in data) {
        state.data.crossposts = data.crossposts;
        renderCrossposts(data.crossposts.crossposts || []);
      }
      if ("deliveryHealth" in data) renderCrosspostHealth(data.deliveryHealth);
      if ("memory" in data) renderMemory(data.memory);
      renderTopicDossier(state.topicDossier);
      if ("platforms" in data) renderPlatforms(data.platforms);
      if ("distributionPlan" in data) renderDistributionPlan(data.distributionPlan);
      if ("redditMetrics" in data) renderRedditMetrics(data.redditMetrics);
      if ("aiUsage" in data) renderAIUsage(data.aiUsage);
      if ("agentBenchmark" in data) {
        renderAgentBenchmark(data.agentBenchmark, "trends" in data ? data.trends.trends || [] : []);
      }
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
      const res = await post("/api/cycle?collect=true&max_clusters=8", {});
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

  window.tgfarmRecommendCommunityTarget = async (id, platform) => {
    if (!id || !["vc_ru", "pikabu"].includes(platform)) return;
    const root = document.getElementById("community-target-result-" + id);
    if (!root) return;
    const label = platform === "vc_ru" ? "vc.ru" : "Пикабу";
    root.textContent = "Читаю актуальный каталог " + label + ", описания и доступные правила…";
    try {
      const result = await post(
        "/api/platform-drafts/" + encodeURIComponent(platform) + "/" + encodeURIComponent(id) + "/targets",
        {},
      );
      const selected = result.selected || null;
      const candidates = result.candidates || [];
      const notes = result.general_notes || [];
      const policyVerdict = result.policy_verdict || null;
      const renderCandidate = (item, isBest = false) => {
        const url = safeExternalUrl(item.url);
        const score = Math.max(0, Math.min(100, Number(item.score || 0)));
        const risks = [
          ...(item.risks || []),
          ...(item.blockers || []).map((value) => "Не рекомендуется: " + value),
        ];
        return `
          <article class="community-target-card ${isBest ? "is-best" : ""}">
            <div class="community-target-head">
              <div><strong>${esc(item.name || item.title || "Без названия")}</strong>
                <span>${isBest ? "Лучший вариант" : item.recommended ? "Подходит для ручной проверки" : "Не рекомендуется"} · совпадение ${score}/100</span></div>
              ${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">Открыть ↗</a>` : ""}
            </div>
            <p>${esc(item.description || "Описание не получено.")}</p>
            ${(item.suggested_tags || item.matches || []).length ? `<div class="community-target-tags">${(item.suggested_tags || item.matches || []).map((value) => `<span>${esc(value)}</span>`).join("")}</div>` : ""}
            ${risks.length ? `<div class="community-target-risks">${risks.map((value) => `<p>! ${esc(value)}</p>`).join("")}</div>` : ""}
            ${item.target_type === "topic"
              ? (item.topic_rules_available && item.rules
                ? `<details><summary>Правила темы</summary><p>${esc(item.rules)}</p></details>`
                : `<small>${item.global_rules_available
                  ? "Отдельные правила темы не распознаны. Проверены общие правила vc.ru, перед отправкой нужна ручная проверка."
                  : "Не удалось прочитать общие правила vc.ru. Тема не подтверждена."}</small>`)
              : (item.rules
                ? `<details><summary>Правила сообщества</summary><p>${esc(item.rules)}</p></details>`
                : `<small>Правила сообщества не удалось распознать; место не рекомендуется.</small>`)}
          </article>`;
      };
      state.communityTargetPlans = state.communityTargetPlans || {};
      state.communityTargetPlans[id + ":" + platform] = result;
      root.innerHTML = `
        <div class="community-target-summary">
          <strong>${selected ? "Место подобрано" : "Безопасное место не найдено"}</strong>
          <p>${esc(result.detail || "")}</p>
          ${policyVerdict ? `<p class="community-policy-verdict ${policyVerdict.allow_publish ? "is-ok" : "is-blocked"}">CommunityPolicy: ${policyVerdict.allow_publish
            ? `правила подтверждены, проверены ${Number(policyVerdict.age_hours || 0).toFixed(1)} ч назад`
            : `публикация запрещена — ${esc(policyVerdict.reason || "правила не подтверждены")}`}</p>` : ""}
          <small>${platform === "vc_ru"
            ? "Перед завершением проверь, что эта тема отображается в редакторе."
            : "Перед завершением проверь, что это сообщество выбрано и теги подходят. Одобрение модераторов не гарантируется."}</small>
        </div>
        ${selected ? `<div class="community-policy-history">
          <button class="btn btn-ghost btn-sm" type="button" onclick="tgfarmPolicyHistory('${esc(id)}','${esc(platform)}')">История правил</button>
          <span class="community-policy-hint">Показывает, когда правила места менялись на самом деле, а не только текущую версию.</span>
          <div class="policy-history-body" id="policy-history-${esc(id)}-${esc(platform)}"></div>
        </div>` : ""}
        ${selected ? renderCandidate(selected, true) : ""}
        ${candidates.filter((item) => !selected || item.slug !== selected.slug).slice(0, 4).map((item) => renderCandidate(item)).join("")}
        ${notes.length ? `<ul class="community-target-notes">${notes.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>` : ""}
        ${result.rules_source ? `<a class="community-target-source" href="${esc(safeExternalUrl(result.rules_source))}" target="_blank" rel="noopener noreferrer">Источник общих правил ↗</a>` : ""}`;
      toast(selected ? label + ": подобрано конкретное место. Открой правила и проверь его." : label + ": подходящее место не найдено, отправлять в случайное сообщество не буду.", selected ? "ok" : "warn");
    } catch (error) {
      root.textContent = "Не удалось подобрать место: " + (error?.message || error);
      toast("Подбор " + label + " не выполнен: " + (error?.message || error), "err");
    }
  };

  // Rules history of the destination that was just picked. Only the post id and
  // the platform id (both validated server-side, no user text) go into the
  // inline handler; the untrusted target id is read back from the stored plan.
  window.tgfarmPolicyHistory = async (id, platform) => {
    const root = document.getElementById("policy-history-" + id + "-" + platform);
    if (!root) return;
    if (root.dataset.loaded === "1") {
      root.innerHTML = "";
      delete root.dataset.loaded;
      return;
    }
    const plan = (state.communityTargetPlans || {})[id + ":" + platform] || {};
    const selected = plan.selected || null;
    // Same precedence the writer uses for `CommunityPolicy.target_id`.
    const targetId = String((selected && (selected.slug || selected.path || selected.name)) || "");
    await loadPolicyHistory(root, platform, targetId);
  };

  window.tgfarmBuildEditorialPreview = async (id) => {
    const platform = document.getElementById("editorial-preview-platform-" + id)?.value || "";
    const root = document.getElementById("editorial-preview-result-" + id);
    if (!root || !platform) return;
    const label = (state.platformData?.platforms || []).find((item) => item.id === platform)?.name || platform;
    root.innerHTML = '<div class="empty">Анализирую тему, формат и ограничения ' + esc(label) + '…</div>';
    try {
      const result = await post(
        "/api/platform-drafts/" + encodeURIComponent(platform) + "/" + encodeURIComponent(id) + "/editorial",
        {},
      );
      if (!result.ok) {
        const candidates = (result.candidates || []).slice(0, 4).map((item) => `
          <article class="community-target-card">
            <strong>${esc(item.name || item.title || "Вариант")}</strong>
            <p>${esc(item.description || "")}</p>
            <small>Совпадение ${Number(item.score || 0)}/100 · ${esc((item.blockers || item.risks || []).join("; "))}</small>
          </article>`).join("");
        root.innerHTML = `
          <div class="community-target-summary">
            <strong>${esc(result.status === "blocked_missing_media" ? "Нужен другой медиаформат" : "Подходящий адресат не найден")}</strong>
            <p>${esc(result.detail || result.brief?.format_reason || "Публикация не подготовлена.")}</p>
          </div>
          ${candidates}
          <small>Публикация не выполнялась.</small>`;
        toast(label + ": " + (result.detail || "нужна дополнительная подготовка."), "warn");
        return;
      }
      const draft = result.draft || {};
      const notes = (result.adaptation?.notes || []).map((item) => `<li>${esc(item)}</li>`).join("");
      const target = result.target?.name || result.target?.title || "";
      const targetUrl = safeExternalUrl(result.target?.url || "");
      const status = result.adaptation?.status === "ai_native" ? "AI-версия" : "детерминированный черновик";
      root.innerHTML = `
        <div class="community-target-summary">
          <strong>${esc(result.brief?.format || result.profile?.format || "Нативный формат")}</strong>
          <p>${esc(result.profile?.audience || "")}</p>
          <small>${esc(result.adaptation?.reason || status)} · обязательна ручная проверка перед отправкой</small>
          ${target ? `<p>Адресат: <strong>${esc(target)}</strong> ${targetUrl ? `<a href="${esc(targetUrl)}" target="_blank" rel="noopener noreferrer">Открыть ↗</a>` : ""}</p>` : ""}
        </div>
        <label class="field"><span>Заголовок</span><textarea rows="2" readonly>${esc(draft.title || "")}</textarea></label>
        <div class="short-video-actions">
          <button class="btn btn-ghost btn-sm" type="button" data-editorial-copy="title">Копировать заголовок</button>
          <button class="btn btn-ghost btn-sm" type="button" data-editorial-copy="body">Копировать текст</button>
        </div>
        <label class="field"><span>Текст</span><textarea rows="8" readonly>${esc(draft.body || "")}</textarea></label>
        ${(draft.hashtags || []).length ? `<p class="drawer-meta">Теги: ${esc(draft.hashtags.map((tag) => "#" + tag).join(" "))}</p>` : ""}
        ${notes ? `<ul class="manual-draft-notes">${notes}</ul>` : ""}
        <small>Черновик не отправлен. Публикация отключена на этом этапе.</small>`;
      root.dataset.editorialTitle = draft.title || "";
      root.dataset.editorialBody = draft.body || "";
      root.querySelectorAll("[data-editorial-copy]").forEach((button) => {
        button.addEventListener("click", () => {
          const value = button.dataset.editorialCopy === "title" ? root.dataset.editorialTitle : root.dataset.editorialBody;
          navigator.clipboard?.writeText(value).then(() => toast("Скопировано", "ok")).catch(() => toast("Не удалось скопировать", "err"));
        });
      });
      toast(label + ": нативный предпросмотр готов.", "ok");
    } catch (error) {
      root.textContent = "Не удалось подготовить нативную версию: " + (error?.message || error);
      toast(label + ": " + (error?.message || error), "err");
    }
  };

  window.tgfarmPrepareManualDraft = async (id) => {
    const platform = document.getElementById("manual-platform-" + id)?.value || "";
    const root = document.getElementById("manual-draft-result-" + id);
    if (!root || !platform) return;
    root.innerHTML = `<div class="empty">Подготавливаю пакет…</div>`;
    try {
      const result = await api("/api/platform-drafts/" + encodeURIComponent(platform) + "/" + encodeURIComponent(id) + "/package");
      const media = (result.media || []).map((item) => `
        <a class="btn btn-ghost btn-sm" href="${esc(item.url)}" download="${esc(item.filename)}">Скачать ${item.kind === "video" ? "MP4" : "изображение"}</a>
      `).join("");
      const notes = (result.notes || []).map((item) => `<li>${esc(item)}</li>`).join("");
      const ready = result.ready
        ? `<span class="tag tag-ok">Пакет готов</span>`
        : `<span class="tag tag-warn">Нужна медиа-подготовка</span>`;
      const externalUrl = safeExternalUrl(result.url) || "#";
      root.innerHTML = `
        <div class="manual-draft-head">
          ${ready}
          <a class="btn btn-ghost btn-sm" href="${esc(externalUrl)}" target="_blank" rel="noopener noreferrer">Открыть ${esc(result.name)} ↗</a>
        </div>
        <p class="drawer-meta">${esc(result.format)} · ${(result.body || "").length} символов · ручная отправка</p>
        <label class="field"><span>Заголовок</span><textarea rows="2" readonly id="manual-title-${esc(id)}">${esc(result.title)}</textarea></label>
        <div class="short-video-actions">
          <button class="btn btn-ghost btn-sm" type="button" onclick="tgfarmCopyManualField('${esc(id)}','title')">Копировать заголовок</button>
          <button class="btn btn-ghost btn-sm" type="button" onclick="tgfarmCopyManualField('${esc(id)}','body')">Копировать текст</button>
          <button class="btn btn-ghost btn-sm" type="button" onclick="tgfarmCopyManualField('${esc(id)}','tags')">Копировать теги</button>
          ${result.needs_video && !result.has_video ? `<button class="btn btn-primary btn-sm" type="button" onclick="tgfarmRenderShort('${esc(id)}')">Создать MP4</button>` : ""}
        </div>
        <label class="field"><span>Текст</span><textarea rows="8" readonly id="manual-body-${esc(id)}">${esc(result.body)}</textarea></label>
        <label class="field"><span>Теги</span><textarea rows="2" readonly id="manual-tags-${esc(id)}">${esc((result.hashtags || []).map((tag) => "#" + tag).join(" "))}</textarea></label>
        <div class="short-video-actions">${media}</div>
        <ul class="manual-draft-notes">${notes}</ul>
        <small class="drawer-meta">Запрос публикации не выполнялся. Проверь правила и отправляй только через официальный редактор выбранной площадки.</small>
      `;
    } catch (error) {
      root.innerHTML = `<div class="empty">${esc(error?.message || error)}</div>`;
      toast("Не удалось подготовить пакет: " + (error?.message || error), "err");
    }
  };

  window.tgfarmCopyManualField = async (id, kind) => {
    const target = document.getElementById("manual-" + kind + "-" + id);
    if (!target) return;
    try {
      await navigator.clipboard.writeText(target.value || "");
      toast("Скопировано", "ok");
    } catch (_) {
      target.focus();
      target.select();
      toast("Текст выделен. Нажми Ctrl+C для копирования.", "warn");
    }
  };

  window.tgfarmPrepareCommunityDraft = async (id, platform) => {
    if (!id || !["vc_ru", "pikabu"].includes(platform)) return;
    const label = platform === "vc_ru" ? "vc.ru" : "Пикабу";
    state.communityTargetPlans = state.communityTargetPlans || {};
    let plan = state.communityTargetPlans[id + ":" + platform] || null;
    try {
      // Rules and public catalogues can change, so re-read them immediately
      // before opening the destination-specific editor.
      plan = await post(
        "/api/platform-drafts/" + encodeURIComponent(platform) + "/" + encodeURIComponent(id) + "/targets",
        {},
      );
      state.communityTargetPlans[id + ":" + platform] = plan;
    } catch (error) {
      plan = null;
    }
    const selected = plan?.selected || null;
    const targetText = selected
      ? "\n\nРекомендуемое место: " + (selected.name || selected.title) + ". Открою редактор с этой страницы, но проверь, что площадка действительно выбрала нужный раздел."
      : "\n\nПодходящее место с достаточным совпадением не найдено. Открою общий редактор, выбери тему/сообщество вручную.";
    if (!confirm("Подготовить черновик для " + label + "? TgFarm заполнит редактор и прикрепит изображение, но НЕ будет нажимать «Опубликовать»." + targetText)) return;
    try {
      const targetQuery = selected
        ? "?target_url=" + encodeURIComponent(selected.url || "")
          + "&target_name=" + encodeURIComponent(selected.name || selected.title || "")
        : "";
      const result = await post(
        "/api/platform-drafts/" + encodeURIComponent(platform) + "/" + encodeURIComponent(id) + "/prepare" + targetQuery,
        {},
      );
      const count = Number(result.media_count || 0);
      const manualSteps = (result.manual_steps || []).join("; ");
      const targetLabel = result.target_name || selected?.name || selected?.title || "";
      const targetMessage = targetLabel ? " Цель: " + targetLabel + "." : "";
      const statusMessage = result.status === "already_prepared"
        ? label + ": открыл ранее подготовленный черновик, дубликат не создавался." + targetMessage
        : result.status === "needs_review"
          ? label + ": прошлая попытка прервалась. Новый черновик не создавался, проверь открытый редактор." + targetMessage
          : label + ": черновик заполнен, изображений: " + count + "." + targetMessage
            + " Проверь выбранную тему/сообщество и опубликуй вручную."
            + (manualSteps ? " Ещё нужно: " + manualSteps + "." : "");
      toast(statusMessage, "ok");
    } catch (error) {
      toast("Не удалось подготовить " + label + ": " + (error?.message || error), "err");
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

  /* ------------------------------------------------------- review board */
  const REVIEW_ACTION_LABELS = {
    confirm_published: "Пост на площадке есть",
    retry_after_check: "Проверил, поста нет — в очередь",
    retry: "Повторить",
    publish_now: "Опубликовать сейчас",
    reject: "Отклонить",
  };

  async function loadReviewBoard(force = false) {
    if (state.reviewBusy && !force) return;
    state.reviewBusy = true;
    const root = $("#review-board");
    if (root && !state.reviewLoaded) root.innerHTML = '<p class="empty">Читаю очередь сверки…</p>';
    try {
      const data = await api("/api/review/queue?limit=25");
      state.review = data || {};
      state.reviewLoaded = true;
      renderReviewBoard();
      const counter = $("#review-nav-count");
      if (counter) counter.textContent = String(Number(data?.total || 0));
    } catch (error) {
      if (root) {
        root.innerHTML = '<p class="empty">Не удалось загрузить очередь: ' + esc(error?.message || error) + "</p>";
      }
      toast("Сверка не загрузилась: " + (error?.message || error), "err");
    } finally {
      state.reviewBusy = false;
    }
  }

  function renderReviewCard(item) {
    const age = Number(item.age_hours || 0);
    const ageLabel = age >= 48 ? Math.round(age / 24) + " дн" : Math.round(age) + " ч";
    const actions = (item.actions || [])
      .map((action) => {
        const label = REVIEW_ACTION_LABELS[action] || action;
        const cls = action === "confirm_published" ? "btn-primary" : "btn-ghost";
        return '<button class="btn ' + cls + ' btn-sm" type="button" data-review-action="'
          + esc(action) + '" data-review-id="' + esc(item.public_id) + '">' + esc(label) + "</button>";
      })
      .join("");
    return '<article class="review-card">'
      + '<div class="review-card-head"><span class="tag">' + esc(item.platform) + "</span>"
      + '<span class="review-age">' + esc(ageLabel) + " · попыток " + Number(item.attempts || 0) + "</span></div>"
      + '<p class="review-hook">' + esc(item.hook || "Без заголовка") + "</p>"
      + '<p class="review-reason">' + esc(item.reason || "") + "</p>"
      + '<div class="review-actions">' + actions + "</div></article>";
  }

  function renderReviewBoard() {
    const root = $("#review-board");
    if (!root) return;
    const data = state.review || {};
    const buckets = data.buckets || {};
    const counts = data.counts || {};
    const titles = data.titles || {};
    const filter = state.reviewFilter || "";
    const shown = filter ? [filter] : ["unknown", "terminal", "held"];
    root.innerHTML = shown
      .map((bucket) => {
        const items = buckets[bucket] || [];
        return '<div class="review-column"><div class="review-column-head"><strong>'
          + esc(titles[bucket] || bucket) + '</strong><span class="tab-counter">'
          + Number(counts[bucket] || 0) + "</span></div>"
          + (items.length
            ? items.map((item) => renderReviewCard(item)).join("")
            : '<p class="empty">Ничего не ждёт решения.</p>')
          + "</div>";
      })
      .join("");
  }

  async function runReviewAction(publicId, action) {
    try {
      if (action === "confirm_published") {
        await post("/api/posts/" + encodeURIComponent(publicId) + "/reconcile", { action: "confirm_published" });
      } else if (action === "retry_after_check") {
        await post("/api/posts/" + encodeURIComponent(publicId) + "/reconcile", {
          action: "retry_after_check",
          confirm_not_published: true,
        });
      } else if (action === "retry") {
        await post("/api/posts/" + encodeURIComponent(publicId) + "/retry", {});
      } else if (action === "publish_now") {
        await post("/api/posts/publish", { post_public_ids: [publicId] });
      } else if (action === "reject") {
        await post("/api/posts/reject", {
          post_public_ids: [publicId],
          reason: "operator review board",
        });
      } else {
        throw new Error("неизвестное действие " + action);
      }
      toast("Готово: " + (REVIEW_ACTION_LABELS[action] || action), "ok");
      await loadReviewBoard(true);
      refresh();
    } catch (error) {
      toast("Действие не выполнено: " + (error?.message || error), "err");
    }
  }

  // Rows one click may return to the queue. The card says «365 failed, 1890
  // attempts»; a button that quietly moved all of them at once would hand the
  // publisher a year of stale drafts, so the panel asks for a pass, not for a pile.
  const REQUEUE_PASS = 50;

  const REQUEUE_REFUSAL_LABELS = {
    publish_outcome_unknown: "исход неизвестен (§80: сверка, не повтор)",
    not_failed: "не финальный отказ",
    already_published: "есть сообщение или отметка публикации",
    journaled_posted: "в журнале уже стоит posted",
    outcome_uncertain: "драйвер умер во время отправки",
    platform_disabled: "площадка выключена",
  };

  function requeueSummary(report) {
    const byPlatform = Object.entries(report.by_platform || {})
      .map(([name, count]) => name + ": " + count)
      .join(", ");
    const refusals = Object.entries(report.refusals || {})
      .map(([reason, count]) => (REQUEUE_REFUSAL_LABELS[reason] || reason) + " — " + count)
      .join("; ");
    // `Number(undefined || 0)` is a finite 0, so the filter below it could never
    // drop anything and a sample with no age printed «самому старшему 0 дн» — a
    // number for a thing that was never measured. Keep only real numbers and let
    // `hrs` say «нет отчёта» for the rest, the same way the queue card does.
    const ages = (report.sample || [])
      .map((row) => row.age_hours)
      .filter((value) => typeof value === "number" && Number.isFinite(value));
    const oldest = ages.length ? hrs(Math.max(...ages)) : "нет отчёта";
    const lines = [
      "Строк в состоянии failed: " + Number(report.scanned || 0) + ".",
      "Можно вернуть: " + Number(report.eligible || 0)
        + " (за один проход не больше " + Number(report.limit || 0) + ")",
      "самому старшему из выборки " + oldest + ".",
      byPlatform ? "Площадки: " + byPlatform + "." : "",
      refusals ? "Не трогает: " + refusals + "." : "",
      Number(report.remaining || 0)
        ? "Ещё ждут следующего прохода: " + Number(report.remaining) + "."
        : "",
    ].filter(Boolean);
    return lines.join(" ");
  }

  async function requeueFailedBatch() {
    let preview;
    try {
      preview = await post("/api/posts/retry-failed", { apply: false, limit: REQUEUE_PASS });
    } catch (error) {
      toast("Предпросмотр не получился: " + (error?.message || error), "err");
      return;
    }
    const summary = requeueSummary(preview);
    if (!Number(preview.eligible || 0)) {
      toast("Возвращать нечего. " + summary, "ok");
      return;
    }
    if (!confirm(summary + "\n\nЭти строки были закрыты как устаревшие. Возврат в очередь —"
      + " обещание издателя, что материал ещё актуален; причина попадёт в журнал."
      + " Площадку никто не трогает: посты просто снова окажутся в scheduled.")) return;
    let reason;
    try {
      reason = prompt("Причина возврата (попадёт в журнал оператора):");
    } catch (error) {
      toast("Браузер не дал ввести причину — возврат отменён.", "err");
      return;
    }
    const text = (reason || "").trim();
    if (!text) {
      toast("Без причины сервер не пишет: эти строки уже были признаны устаревшими.", "err");
      return;
    }
    try {
      const report = await post("/api/posts/retry-failed", {
        apply: true,
        limit: REQUEUE_PASS,
        reason: text,
      });
      if (report.refused) {
        toast("Отказано: " + report.refused, "err");
        return;
      }
      toast("В очередь вернулось постов: " + Number(report.changed || 0) + ". " + summary, "ok");
      await loadReviewBoard(true);
      refresh();
    } catch (error) {
      toast("Проход не выполнился: " + (error?.message || error), "err");
    }
  }

  /* ------------------------------------------------------------ connections */
  const CONNECT_MODES = { oauth: "OAuth", vkhost: "VK", browser: "Браузер", token: "Токен", manual: "Вручную" };

  function connectStatusOf(row) {
    if (row.authorized === true) {
      return { key: "ready", label: row.account ? "Подключено · " + row.account : "Подключено" };
    }
    if (row.authorized === false) return { key: "failed", label: "Вход не прошёл" };
    if (row.profile_saved || row.session_verified) return { key: "pending", label: "Сессия сохранена, нужна проверка" };
    if (row.keys_present) return { key: "pending", label: "Ключи сохранены, нужна проверка" };
    return { key: "needs", label: "Требует входа" };
  }

  function connectMatches(row, filter) {
    if (!filter) return true;
    if (["ready", "needs", "failed", "pending"].includes(filter)) return connectStatusOf(row).key === filter;
    return row.mode === filter;
  }

  async function loadConnections(force = false) {
    if (state.connectBusy && !force) return;
    state.connectBusy = true;
    const grid = $("#connect-grid");
    if (grid && !state.connections) grid.innerHTML = '<div class="empty">Читаю площадки…</div>';
    try {
      const data = await api("/api/connect/matrix");
      state.connections = data || { platforms: [] };
      state.connectLoaded = true;
      renderConnections();
    } catch (error) {
      if (grid) grid.innerHTML = '<div class="empty">Не удалось получить список площадок: ' + esc(error?.message || error) + "</div>";
      toast("Подключения не загрузились: " + (error?.message || error), "err");
    } finally {
      state.connectBusy = false;
    }
  }

  const CONNECT_BRAND = {
    vk: "#0077ff", vk_clips: "#0077ff", telegram: "#2aabee", dzen: "#f44336", max: "#7d5cff",
    x: "#e7eaf0", reddit: "#ff4500", youtube: "#ff0033", tiktok: "#fe2c55", instagram: "#e1306c",
    threads: "#8a8f98", facebook: "#1877f2", linkedin: "#0a66c2", pinterest: "#e60023",
    odnoklassniki: "#ee8208", rutube: "#1f9e4a", discord: "#5865f2", mastodon: "#6364ff",
    bluesky: "#1185fe", medium: "#f4f4f4", vc_ru: "#3d68ff", pikabu: "#5fa93a", habr: "#6f9ad1",
    tenchat: "#f5c518", twitch: "#9146ff", tumblr: "#36465d", telegraph: "#4a9eda",
    snapchat: "#fffc00", wechat: "#07c160", douyin: "#fe2c55", rednote: "#ff2442",
    whatsapp_channels: "#25d366",
  };

  const CONNECT_GROUPS = [
    { key: "ready", title: "Подключено", hint: "Доступ подтверждён площадкой" },
    { key: "pending", title: "Ждёт проверки", hint: "Сессия или ключи сохранены, доступ ещё не подтверждён" },
    { key: "failed", title: "Вход не прошёл", hint: "Площадка отклонила доступ — нужно повторить" },
    { key: "needs", title: "Не подключено", hint: "Нажми «Подключить» на карточке" },
  ];

  function connectCard(row) {
    const status = connectStatusOf(row);
    const mode = CONNECT_MODES[row.mode] || row.mode;
    const brand = CONNECT_BRAND[row.id] || "#5b8cff";
    const monogram = String(row.name || row.id).replace(/[^A-Za-zА-Яа-я0-9]/g, "").slice(0, 2).toUpperCase() || "??";
    const primary = status.key === "ready" ? "Переподключить" : "Подключить";
    const monogramStyle = "color:" + brand + ";border-color:" + brand + ";background:color-mix(in srgb, " + brand + " 14%, transparent)";
    const pill = '<span class="connect-mode-chip">Вход: ' + esc(mode) + "</span>"
      + '<span class="connect-pill connect-pill--' + esc(status.key) + '">' + esc(status.label) + "</span>";
    return '<article class="connect-card connect-card--' + esc(status.key) + '">'
      + '<div class="connect-card-top"><span class="connect-monogram" style="' + monogramStyle + '">' + esc(monogram) + "</span>"
      + '<div class="connect-card-title"><strong>' + esc(row.name || row.id) + "</strong>"
      + "<span>" + esc(row.category || "") + "</span></div>"
      + pill + "</div>"
      + '<p class="connect-hint">' + esc(row.hint || "") + "</p>"
      + '<div class="connect-actions">'
      + '<button class="btn ' + (status.key === "ready" ? "btn-ghost" : "btn-primary") + ' btn-sm" type="button" data-connect-start="'
      + esc(row.id) + '">' + primary + "</button>"
      + '<button class="btn btn-ghost btn-sm" type="button" data-connect-check="' + esc(row.id) + '">Проверить</button>'
      + "</div></article>";
  }

  function renderConnections() {
    const grid = $("#connect-grid");
    const data = state.connections || { platforms: [] };
    const rows = data.platforms || [];
    const counts = { ready: 0, pending: 0, needs: 0, failed: 0 };
    rows.forEach((row) => { counts[connectStatusOf(row).key] += 1; });
    const counter = $("#connect-nav-count");
    if (counter) counter.textContent = String(counts.ready);
    const stats = $("#connect-stats");
    if (stats) {
      stats.innerHTML =
        '<span class="connect-stat connect-stat--ok">' + counts.ready + " подключено</span>"
        + '<span class="connect-stat connect-stat--warn">' + (counts.needs + counts.pending) + " без доступа</span>"
        + (counts.failed ? '<span class="connect-stat connect-stat--err">' + counts.failed + " с ошибкой</span>" : "");
    }
    if (!grid) return;
    grid.classList.add("connect-grid--sections");
    const filter = state.connectFilter || "";
    const visible = rows.filter((row) => connectMatches(row, filter));
    if (!visible.length) {
      grid.innerHTML = '<div class="empty">Под этот фильтр площадок нет.</div>';
      return;
    }
    const sections = filter
      ? [{ key: "filter", title: "", hint: "", rows: visible }]
      : CONNECT_GROUPS.map((group) => ({
          ...group,
          rows: visible.filter((row) => connectStatusOf(row).key === group.key),
        })).filter((group) => group.rows.length);
    grid.innerHTML = sections.map((section) => {
      const head = section.title
        ? '<div class="connect-section"><div class="connect-section-head"><strong>' + esc(section.title)
          + "</strong><span>" + section.rows.length + "</span></div>"
          + (section.hint ? '<p class="connect-section-hint">' + esc(section.hint) + "</p>" : "")
          + "</div>"
        : "";
      return '<section class="connect-section-wrap">' + head
        + '<div class="connect-section-grid">' + section.rows.map((row) => connectCard(row)).join("") + "</div></section>";
    }).join("");
  }

  function connectDialog() { return $("#connect-dialog"); }

  function openConnectDialog(platformId) {
    const rows = (state.connections || {}).platforms || [];
    const row = rows.find((item) => item.id === platformId);
    if (!row) return;
    state.connectTarget = row;
    const dialog = connectDialog();
    if (!dialog) return;
    $("#connect-dialog-title").textContent = row.name || row.id;
    $("#connect-dialog-sub").textContent = (CONNECT_MODES[row.mode] || row.mode) + " · " + (row.category || "");
    const body = $("#connect-dialog-body");
    if (row.mode === "token") {
      const fields = (row.fields || []).map((field) =>
        '<label class="connect-field"><span>' + esc(field.label) + "</span>"
        + '<input type="' + (field.secret ? "password" : "text") + '" data-connect-field="' + esc(field.key) + '" '
        + 'autocomplete="off" spellcheck="false" placeholder="' + esc(field.key) + '"></label>'
      ).join("");
      body.innerHTML = '<p class="connect-dialog-text">' + esc(row.hint) + "</p>" + fields
        + '<div class="connect-dialog-actions"><button class="btn btn-primary btn-sm" type="button" data-connect-save="' + esc(row.id) + '">Сохранить и проверить</button></div>'
        + '<p class="connect-dialog-status" id="connect-dialog-status"></p>'
        + '<p class="connect-dialog-note">Значения уходят только в локальный .env и никогда не возвращаются в интерфейс.</p>';
    } else if (row.mode === "oauth") {
      body.innerHTML = '<ol class="connect-steps"><li>Откроется страница согласия площадки.</li>'
        + "<li>Подтверди доступ — площадка сама вернёт токен.</li>"
        + "<li>Панель подхватит его и обновит статус карточки.</li></ol>"
        + '<div class="connect-dialog-actions"><button class="btn btn-primary btn-sm" type="button" data-connect-oauth="' + esc(row.id) + '">Открыть страницу согласия</button></div>'
        + '<p class="connect-dialog-status" id="connect-dialog-status"></p>';
    } else if (row.mode === "vkhost") {
      body.innerHTML = '<p class="connect-dialog-text">Откроется VKHost. Подтверди доступ — токен перехватится и сохранится автоматически.</p>'
        + '<div class="connect-dialog-actions"><button class="btn btn-primary btn-sm" type="button" data-connect-browser="' + esc(row.id) + '">Открыть VKHost</button></div>'
        + '<p class="connect-dialog-status" id="connect-dialog-status"></p>';
    } else if (row.mode === "browser") {
      const extra = row.id === "dzen"
        ? '<p class="connect-dialog-note">У Дзена отдельный профиль: используй кнопку «Дзен: войти» на вкладке «Площадки».</p>'
        : "";
      body.innerHTML = '<ol class="connect-steps"><li>Откроется окно браузера с профилем площадки.</li>'
        + "<li>Войди в аккаунт и вернись сюда — окно можно закрыть.</li>"
        + "<li>Панель проверит доступность сессии и сохранит профиль.</li></ol>"
        + '<p class="connect-dialog-note">Важно: успешный вход ещё не гарантирует возможность публикации. Отправка выполняется тем способом, который поддерживает адаптер площадки: API, браузерная сессия, штатный редактор или ручная подготовка.</p>'
        + '<p class="connect-dialog-text">' + esc(row.hint) + "</p>" + extra
        + '<div class="connect-dialog-actions"><button class="btn btn-primary btn-sm" type="button" data-connect-browser="' + esc(row.id) + '">Открыть окно входа</button></div>'
        + '<p class="connect-dialog-status" id="connect-dialog-status"></p>';
    } else {
      body.innerHTML = '<p class="connect-dialog-text">Автоматического входа нет: материал для площадки готовится вручную.</p>';
    }
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "open");
  }

  function closeConnectDialog() {
    const dialog = connectDialog();
    if (!dialog) return;
    if (typeof dialog.close === "function" && dialog.open) dialog.close();
    else dialog.removeAttribute("open");
    state.connectTarget = null;
  }

  async function runBrowserConnect(platformId) {
    const status = $("#connect-dialog-status");
    const button = document.querySelector('[data-connect-browser="' + CSS.escape(platformId) + '"]');
    if (status) status.textContent = "Открыл окно браузера. Войди в аккаунт — я жду подтверждения…";
    if (button) { button.disabled = true; button.classList.add("is-busy"); }
    try {
      const result = await post("/api/connect/" + encodeURIComponent(platformId) + "/authorize", {});
      if (status) status.textContent = (result.detail || "Готово") + (result.account ? " · " + result.account : "");
      toast("Вход: " + platformId + " — " + (result.state || "ok"), "ok");
      await loadConnections(true);
    } catch (error) {
      if (status) status.textContent = "Не удалось завершить вход: " + (error?.message || error);
      toast("Вход не завершён: " + (error?.message || error), "err");
    } finally {
      if (button) { button.disabled = false; button.classList.remove("is-busy"); }
    }
  }

  async function runOauthConnect(platformId) {
    const status = $("#connect-dialog-status");
    const button = document.querySelector('[data-connect-oauth="' + CSS.escape(platformId) + '"]');
    if (button) { button.disabled = true; button.classList.add("is-busy"); }
    try {
      const result = await post("/api/platforms/" + encodeURIComponent(platformId) + "/auth/start", {});
      const url = safeExternalUrl(result.authorize_url);
      if (!url) throw new Error("площадка не выдала адрес согласия");
      window.open(url, "_blank", "noopener,noreferrer");
      if (status) status.textContent = "Ждём подтверждения на стороне площадки…";
      for (let attempt = 0; attempt < 100; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 3000));
        const state = await api("/api/connect/" + encodeURIComponent(platformId) + "/status");
        if (state.authorized === true) {
          if (status) status.textContent = "Готово" + (state.account ? " · " + state.account : "");
          toast("Подключено: " + platformId, "ok");
          await loadConnections(true);
          return;
        }
      }
      if (status) status.textContent = "Подтверждение не пришло за 5 минут. Можно закрыть окно и повторить.";
    } catch (error) {
      if (status) status.textContent = "OAuth не запустился: " + (error?.message || error);
      toast("OAuth не запустился: " + (error?.message || error), "err");
    } finally {
      if (button) { button.disabled = false; button.classList.remove("is-busy"); }
    }
  }

  async function saveConnectTokens(platformId) {
    const values = {};
    document.querySelectorAll("[data-connect-field]").forEach((input) => {
      const value = String(input.value || "").trim();
      if (value) values[input.dataset.connectField] = value;
    });
    const status = $("#connect-dialog-status");
    if (!Object.keys(values).length) {
      if (status) status.textContent = "Заполни хотя бы одно поле — пустые поля не затирают сохранённое.";
      toast("Заполни хотя бы одно поле", "warn");
      return;
    }
    const button = document.querySelector('[data-connect-save="' + CSS.escape(platformId) + '"]');
    if (button) { button.disabled = true; button.classList.add("is-busy"); button.textContent = "Сохраняю…"; }
    if (status) status.textContent = "Сохраняю ключи в локальный .env…";
    try {
      await put("/api/platforms/" + encodeURIComponent(platformId) + "/credentials", { values });
    } catch (error) {
      if (status) status.textContent = "Не сохранено: " + (error?.message || error) + ". Окно открыто, значение можно поправить.";
      toast("Не удалось сохранить ключи: " + (error?.message || error), "err");
      if (button) { button.disabled = false; button.classList.remove("is-busy"); button.textContent = "Сохранить и проверить"; }
      return false;
    }
    const keys = Object.keys(values).join(", ");
    if (status) status.textContent = "Сохранено: " + keys + ". Закрываю окно и проверяю доступ в фоне…";
    toast("Ключи сохранены: " + platformId, "ok");
    await new Promise((resolve) => setTimeout(resolve, 700));
    closeConnectDialog();
    await loadConnections(true);
    // The check may take a network timeout on a blocked platform: run it after the
    // dialog is gone so the operator never waits in front of a spinner.
    checkConnect(platformId, false).then((check) => {
      if (check && check.authorized === true) {
        toast("Доступ подтверждён: " + platformId, "ok");
      } else if (check) {
        toast("Ключи сохранены, но доступ не подтверждён: " + platformId, "warn");
      }
    });
    return true;
  }

  async function checkConnect(platformId, quiet = false) {
    try {
      const result = await post("/api/platforms/" + encodeURIComponent(platformId) + "/auth/check", {});
      const ok = result.authorized === true;
      if (!quiet) toast((ok ? "Доступ подтверждён: " : "Доступ не подтверждён: ") + platformId, ok ? "ok" : "warn");
      const status = $("#connect-dialog-status");
      if (status && result.detail) status.textContent = String(result.detail).slice(0, 200);
      return result;
    } catch (error) {
      if (!quiet) toast("Проверка не удалась: " + (error?.message || error), "err");
      return null;
    } finally {
      await loadConnections(true);
    }
  }

  /* ---------------------------------------------------------------- wiring */
  const VIEW_TITLES = {
    overview: ["Command Center", "Состояние фермы, команда и площадки в одном окне"],
    connect: ["Подключения", "Один клик: сайт площадки, вход, сохранение сессии и токенов"],
    team: ["AI Team", "Редакционные роли и реальные стадии работы конвейера"],
    platforms: ["Центр площадок", "Каталог площадок, статусы подключения и готовность к публикации"],
    plan: ["Планирование", "Редакционный план: темы, слоты и ревизии"],
    trends: ["Тренды", "Кластеры выше порога тепла и объяснение, почему они горячие"],
    posts: ["Очередь публикаций", "Материалы, их статусы и расписание"],
    review: ["Требует внимания", "Неопределённые результаты, финальные отказы и материалы без слота"],
    memory: ["Память публикаций", "Что уже вышло и что нельзя повторять"],
    channels: ["Каналы", "Подключённые каналы и их персона"],
    sources: ["Источники", "Флот сборщиков и их здоровье"],
    runs: ["Запуски", "Журнал прогонов конвейера по стадиям"],
  };

  function switchView(view) {
    state.view = view;
    $$(".tab").forEach((t) => t.classList.toggle("is-active", t.dataset.view === view));
    $$(".view").forEach((v) => v.classList.toggle("is-active", v.dataset.view === view));
    const meta = VIEW_TITLES[view];
    if (meta) {
      const title = $("#view-title");
      const sub = $("#view-sub");
      if (title) title.textContent = meta[0];
      if (sub) sub.textContent = meta[1];
    }
    if (view === "plan" && !state.planLoaded) loadEditorialPlan();
    if (view === "review" && !state.reviewLoaded) loadReviewBoard();
    if (view === "sources") loadSourcesHealth();
    if (view === "connect" && !state.connectLoaded) loadConnections();
    if (view === "sources") loadReprintBackfill();
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
  $("#run-cycle").addEventListener("click", window.tgfarmHostCycle);

  $$(".tab").forEach((t) => t.addEventListener("click", () => switchView(t.dataset.view)));
  const reviewBoard = $("#review-board");
  if (reviewBoard) {
    reviewBoard.addEventListener("click", (event) => {
      const button = event.target.closest("[data-review-action]");
      if (!button) return;
      event.preventDefault();
      runReviewAction(button.dataset.reviewId, button.dataset.reviewAction);
    });
  }
  // The overview's unresolved worklist offers the drawer's action, not a new one:
  // `tgfarmReconcile` is the only path from here to `/api/posts/{id}/reconcile`, and
  // it asks the operator to confirm before anything is written (§80).
  const unresolvedList = $("#overview-unresolved");
  if (unresolvedList) {
    unresolvedList.addEventListener("click", (event) => {
      const button = event.target.closest("[data-unresolved-action]");
      if (!button) return;
      event.preventDefault();
      window.tgfarmReconcile(button.dataset.unresolvedId, button.dataset.unresolvedAction);
    });
  }
  const trendsList = $("#trends-list");
  if (trendsList) {
    trendsList.addEventListener("click", (event) => {
      const button = event.target.closest("[data-trend-explain]");
      if (button) {
        event.preventDefault();
        explainTrend(button.dataset.trendExplain);
        return;
      }
      const skipsButton = event.target.closest("[data-trend-skips]");
      if (!skipsButton) return;
      event.preventDefault();
      explainSkips(skipsButton.dataset.trendSkips);
    });
  }
  const sourcesHealth = $("#sources-health");
  if (sourcesHealth) {
    sourcesHealth.addEventListener("click", (event) => {
      const button = event.target.closest("[data-source-reset]");
      if (!button) return;
      event.preventDefault();
      resetSourceCooldown(button.dataset.sourceReset);
    });
  }
  const connectGrid = $("#connect-grid");
  if (connectGrid) {
    connectGrid.addEventListener("click", (event) => {
      const start = event.target.closest("[data-connect-start]");
      if (start) {
        event.preventDefault();
        openConnectDialog(start.dataset.connectStart);
        return;
      }
      const check = event.target.closest("[data-connect-check]");
      if (check) {
        event.preventDefault();
        checkConnect(check.dataset.connectCheck);
      }
    });
  }
  const connectFilters = $("#connect-filters");
  if (connectFilters) {
    connectFilters.addEventListener("click", (event) => {
      const button = event.target.closest("[data-connect-mode]");
      if (!button) return;
      state.connectFilter = button.dataset.connectMode || "";
      connectFilters.querySelectorAll("button").forEach((item) => item.classList.toggle("is-active", item === button));
      renderConnections();
    });
  }
  const connectDialogEl = $("#connect-dialog");
  if (connectDialogEl) {
    connectDialogEl.addEventListener("click", (event) => {
      if (event.target === connectDialogEl) { closeConnectDialog(); return; }
      const browser = event.target.closest("[data-connect-browser]");
      if (browser) { event.preventDefault(); runBrowserConnect(browser.dataset.connectBrowser); return; }
      const oauth = event.target.closest("[data-connect-oauth]");
      if (oauth) { event.preventDefault(); runOauthConnect(oauth.dataset.connectOauth); return; }
      const save = event.target.closest("[data-connect-save]");
      if (save) { event.preventDefault(); saveConnectTokens(save.dataset.connectSave); }
    });
  }
  const connectClose = $("#connect-dialog-close");
  if (connectClose) connectClose.addEventListener("click", () => closeConnectDialog());

  const reviewRefresh = $("#review-refresh");
  if (reviewRefresh) reviewRefresh.addEventListener("click", () => loadReviewBoard(true));
  const reviewRequeue = $("#review-requeue");
  if (reviewRequeue) reviewRequeue.addEventListener("click", () => requeueFailedBatch());
  $$("#review-filters button").forEach((button) =>
    button.addEventListener("click", () => {
      state.reviewFilter = button.dataset.bucket || "";
      $$("#review-filters button").forEach((item) => item.classList.toggle("is-active", item === button));
      renderReviewBoard();
    }));
  const planRefresh = $("#plan-refresh");
  if (planRefresh) planRefresh.addEventListener("click", () => loadEditorialPlan(true));
  const planApply = $("#plan-apply");
  if (planApply) planApply.addEventListener("click", async () => {
    planApply.disabled = true;
    try {
      const res = await fetch(API + "/api/editorial/plan/refresh", { method: "POST", headers: authHeaders() });
      if (!res.ok) throw new Error("HTTP " + res.status);
      await loadEditorialPlan(true);
      toast("План зафиксирован", "ok");
    } catch (err) {
      toast("Не удалось зафиксировать план: " + (err && err.message ? err.message : err), "err");
    } finally {
      planApply.disabled = false;
    }
  });
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

  $("#agent-benchmark-cluster")?.addEventListener("change", (event) => {
    state.agentBenchmarkCluster = event.target.value || "";
    const button = $("#agent-benchmark-run");
    if (button) button.disabled = !state.agentBenchmarkCluster || state.agentBenchmarkBusy;
  });
  $("#agent-benchmark-run")?.addEventListener("click", runAgentBenchmark);
  $("#agent-benchmark-refresh")?.addEventListener("click", refreshAgentBenchmark);

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

  $("#tiktok-publish-form")?.addEventListener("submit", submitTikTokPublish);
  $("#tiktok-publish-close")?.addEventListener("click", closeTikTokPublishDialog);
  $("#tiktok-publish-cancel")?.addEventListener("click", closeTikTokPublishDialog);
  $("#tiktok-publish-dialog")?.addEventListener("cancel", (event) => {
    if (state.tiktokBusy) {
      event.preventDefault();
      setTikTokPublishStatus("Отправка выполняется. Дождись результата, чтобы не потерять статус.", "warn");
    }
  });
  $("#tiktok-publish-dialog")?.addEventListener("close", () => {
    if (!state.tiktokBusy) {
      state.tiktokPostId = "";
      state.tiktokPrepared = null;
    }
  });
  $("#tiktok-publish-caption")?.addEventListener("input", updateTikTokPublishControls);
  $("#tiktok-publish-privacy")?.addEventListener("change", updateTikTokPublishControls);
  $("#tiktok-publish-consent")?.addEventListener("change", updateTikTokPublishControls);

  $("#distribution-mode-buttons")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-runtime-mode]");
    if (button) window.tgfarmSetRuntimeMode(button.dataset.runtimeMode);
  });

  $("#platform-search")?.addEventListener("input", (event) => {
    state.platformQuery = event.target.value || "";
    renderPlatformCards();
  });
  $("#platform-filters")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-platform-filter]");
    if (!button) return;
    state.platformFilter = button.dataset.platformFilter || "all";
    $$("#platform-filters [data-platform-filter]").forEach((item) =>
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
    const maxWebhook = event.target.closest("[data-platform-max-webhook]");
    if (maxWebhook) {
      await registerMaxWebhook(maxWebhook);
      return;
    }
    const destinationLookup = event.target.closest("[data-platform-destinations]");
    if (destinationLookup) {
      await discoverPlatformDestinations(destinationLookup.dataset.platformDestinations, destinationLookup);
      return;
    }
    const authConfig = event.target.closest("[data-platform-auth-config]");
    if (authConfig) {
      await openPlatformAuthorization(authConfig.dataset.platformAuthConfig, true);
      return;
    }
    const browserAuth = event.target.closest("[data-platform-browser-auth]");
    if (browserAuth) {
      await startPlatformBrowserLogin(browserAuth.dataset.platformBrowserAuth, browserAuth);
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
      $$("#platform-filters [data-platform-filter]").forEach((item) =>
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
