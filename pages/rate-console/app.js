(() => {
  "use strict";

  const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
  const $ = (selector) => document.querySelector(selector);

  const state = {
    sessionId: "",
    sessions: [],
    monitors: {},
    chartMonitors: {},
    chartAutoSend: {},
    chartCron: "",
    chartCronBySession: {},
    cronDialogSession: "",
    chartPoints: {},
    priceExtremes: {},
    rates: [],
    ratesStamp: "",
    ratesError: "",
    currencyCache: [],
    selectedChart: "",
    onlyCurrent: false,
    umoOptions: [],
    addedSessions: [],
    platforms: [],
    manualTarget: false,
  };

  const el = {
    status: $("#status"),
    refresh: $("#refresh"),
    sessionSelect: $("#session-select"),
    sessionSelectField: $("#session-select-field"),
    sessionAdd: $("#session-add"),
    sessionRefresh: $("#session-refresh"),
    sessionHint: $("#session-hint"),
    umoDialog: $("#umo-dialog"),
    umoTitle: $("#umo-title"),
    umoClose: $("#umo-close"),
    umoCancel: $("#umo-cancel"),
    umoConfirm: $("#umo-confirm"),
    umoPlatform: $("#umo-platform"),
    umoTarget: $("#umo-target"),
    umoTargetField: $("#umo-target-field"),
    umoManual: $("#umo-manual"),
    umoManualField: $("#umo-manual-field"),
    umoManualToggle: $("#umo-manual-toggle"),
    umoStatus: $("#umo-status"),
    ratesBody: $("#rates-body"),
    ratesEmpty: $("#rates-empty"),
    ratesStamp: $("#rates-stamp"),
    monitorForm: $("#monitor-form"),
    monitorCurrency: $("#monitor-currency"),
    monitorType: $("#monitor-type"),
    monitorCondition: $("#monitor-condition"),
    monitorThreshold: $("#monitor-threshold"),
    thresholdHint: $("#threshold-hint"),
    monitorsBody: $("#monitors-body"),
    monitorsEmpty: $("#monitors-empty"),
    monitorsCount: $("#monitors-count"),
    onlyCurrent: $("#only-current-session"),
    chartForm: $("#chart-form"),
    chartCurrency: $("#chart-currency"),
    chartAddNote: $("#chart-add-note"),
    chartAddLabel: $("#chart-add-label"),
    chartList: $("#charts-body"),
    chartsBody: $("#charts-body"),
    chartsCount: $("#charts-count"),
    chartsOnlyCurrent: $("#charts-only-current"),
    chartsEmpty: $("#charts-empty"),
    chartTitle: $("#chart-title"),
    chartMeta: $("#chart-meta"),
    chartCanvas: $("#chart-canvas"),
    chartEmpty: $("#chart-empty"),
    cronInput: $("#cron-input"),
    cronApply: $("#cron-apply"),
    cronDialog: $("#cron-dialog"),
    cronSession: $("#cron-session"),
    cronPresetList: $("#cron-preset-list"),
    cronDialogInput: $("#cron-dialog-input"),
    cronDialogHint: $("#cron-dialog-hint"),
    cronSave: $("#cron-save"),
    cronReset: $("#cron-reset"),
    cronCancel: $("#cron-cancel"),
    cronClose: $("#cron-close"),
    toast: $("#toast"),
  };

  const PRICE_TYPES = {
    buy: { field: "结汇价", label: "结汇价" },
    sell: { field: "购汇价", label: "购汇价" },
  };

  /** 走势图推送时间的快捷选项。 */
  const CRON_PRESETS = [
    { label: "每天 9 点", cron: "0 9 * * *" },
    { label: "工作日中午", cron: "0 12 * * 1-5" },
    { label: "每周一", cron: "0 9 * * 1" },
    { label: "每 6 小时", cron: "0 */6 * * *" },
  ];

  let toastTimer = 0;

  /** 显示一条浮动提示。 */
  function toast(message, isError) {
    el.toast.textContent = message;
    el.toast.classList.toggle("is-error", Boolean(isError));
    el.toast.hidden = false;
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => {
      el.toast.hidden = true;
    }, isError ? 4200 : 2400);
  }

  /** 更新右上角状态：仅在刷新中或出错时显示，完成后自动隐藏。 */
  function setStatus(kind, text) {
    if (!kind) {
      el.status.hidden = true;
      el.status.textContent = "";
      el.status.className = "status";
      return;
    }
    el.status.hidden = false;
    el.status.className = `status is-${kind}`;
    el.status.textContent = text;
  }

  /** 把媒体查询或 AstrBot 下发的主题同步到文档根节点。 */
  function applyTheme(context) {
    const root = document.documentElement;
    if (context && typeof context.isDark === "boolean") {
      root.setAttribute("data-theme", context.isDark ? "dark" : "light");
      return;
    }
    const prefersDark = window.matchMedia?.("(prefers-color-scheme: dark)")?.matches;
    root.setAttribute("data-theme", prefersDark ? "dark" : "light");
  }

  async function callGet(route, params) {
    if (!bridge || typeof bridge.apiGet !== "function") {
      throw new Error("AstrBot 插件页面桥接不可用，请从 Dashboard 打开本页面。");
    }
    return bridge.apiGet(route, params);
  }

  async function callPost(route, body) {
    if (!bridge || typeof bridge.apiPost !== "function") {
      throw new Error("AstrBot 插件页面桥接不可用，请从 Dashboard 打开本页面。");
    }
    return bridge.apiPost(route, body);
  }

  /** 读取后端状态并刷新全部视图。 */
  async function loadState() {
    const data = await callGet("state");
    state.sessions = data.sessions || [];
    state.monitors = data.monitors || {};
    state.chartMonitors = data.chart_monitors || {};
    state.chartAutoSend = data.chart_auto_send || {};
    state.chartCron = data.chart_cron || "";
    state.chartCronBySession = data.chart_cron_by_session || {};
    state.chartPoints = data.chart_points || {};
    state.priceExtremes = data.price_extremes || {};

    el.cronInput.value = data.cron || "";

    if (!state.sessionId) {
      state.sessionId = state.sessions[0] || "";
    }
    syncSessionUi();
    renderMonitors();
    renderCharts();
    // 已配置的币种也要进入下拉候选，避免牌价不可用时无币种可选
    renderCurrencyOptions();
  }

  /** 拉取可选的会话列表（UMO）。 */
  async function loadUmoOptions() {
    const data = await callGet("umo-options");
    state.umoOptions = data.sessions || [];
    // 仅在尚未选择会话时自动选中第一个；手动输入的会话保持不变
    if (!state.sessionId && state.umoOptions.length) {
      state.sessionId = state.umoOptions[0].umo;
    }
    syncSessionUi();
    renderMonitors();
    renderCharts();
  }

  /** 合并后端返回的会话与本次新添加入口，供下拉展示。 */
  function allSessionOptions() {
    const options = state.umoOptions.slice();
    state.addedSessions.forEach((umo) => {
      if (options.some((item) => item.umo === umo)) return;
      const parts = umo.split(":", 3);
      options.push({
        umo,
        platform_id: parts[0] || "",
        message_type: parts[1] || "",
        session_id: parts[2] || "",
        rule_count: 0,
        chart_count: 0,
        auto_send: false,
        added: true,
      });
    });
    return options;
  }

  /** 同步会话下拉与提示。 */
  function syncSessionUi() {
    const options = allSessionOptions();
    const known = options.some((item) => item.umo === state.sessionId);
    const html = options.map((item) => {
      const selected = item.umo === state.sessionId ? "selected" : "";
      return `<option value="${escapeAttr(item.umo)}" ${selected}>${escapeHtml(describeUmo(item))}</option>`;
    });
    // 当前会话不在候选中时补一条，避免选择器回落到第一项
    if (state.sessionId && !known) {
      html.unshift(
        `<option value="${escapeAttr(state.sessionId)}" selected>${escapeHtml(state.sessionId)}</option>`,
      );
    }
    if (!html.length) {
      html.push('<option value="">暂无会话，点「添加 UMO」新建</option>');
    }
    el.sessionSelect.innerHTML = html.join("");
    el.sessionSelect.value = state.sessionId || "";

    updateSessionHint();
  }

  /** 生成下拉项的展示文本。 */
  function describeUmo(item) {
    const typeName =
      item.message_type === "GroupMessage"
        ? "群"
        : item.message_type === "FriendMessage"
          ? "私聊"
          : item.message_type;
    const marks = [];
    if (item.rule_count) marks.push(`${item.rule_count} 条规则`);
    if (item.chart_count) marks.push(`${item.chart_count} 个追踪`);
    const tail = marks.length ? ` · ${marks.join(" · ")}` : " · 未配置";
    const label = item.name ? `${item.name}（${item.session_id}）` : item.session_id;
    return `${item.platform_id} · ${typeName} ${label}${tail}`;
  }

  function updateSessionHint() {
    if (!state.sessionId) {
      el.sessionHint.textContent = "未选择会话";
      return;
    }
    const rules = (state.monitors[state.sessionId] || []).length;
    el.sessionHint.textContent = rules
      ? `已载入，共 ${rules} 条规则`
      : "已载入，暂无规则";
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function escapeAttr(value) {
    return escapeHtml(value);
  }

  /**
   * 取出一条监控规则对应的现价。
   * 规则的 type=buy 对应结汇价(foreignBuy)，type=sell 对应购汇价(foreignSell)。
   */
  function rulePrice(rule) {
    const rate = state.rates.find((item) => item.name === rule.currency);
    if (!rate) return null;
    const value = Number(rule.type === "buy" ? rate.buy : rate.sell);
    return Number.isFinite(value) && value > 0 ? value : null;
  }

  /** 拉取实时牌价并渲染表格；失败时保留上次成功的币种列表。 */
  async function loadRates() {
    try {
      const data = await callGet("rates");
      state.rates = data.rates || [];
      state.ratesError = "";
      state.ratesStamp = data.fetched_at || "";
      // 记录成功获取过的币种，供牌价不可用时继续选择
      if (state.rates.length) {
        state.currencyCache = state.rates.map((item) => item.name);
      }
    } catch (error) {
      state.rates = [];
      state.ratesError = error.message || "获取牌价失败";
    }

    el.ratesStamp.textContent = state.ratesError
      ? `牌价暂不可用：${state.ratesError}`
      : state.ratesStamp
        ? `更新于 ${state.ratesStamp}`
        : "";
    renderRates();
    renderCurrencyOptions();
    // 牌价是规则表现价的来源，拿到后必须重画一次，否则「当前牌价」一列会一直是空
    renderMonitors();

    if (state.ratesError) {
      throw new Error(state.ratesError);
    }
  }

  /** 汇总所有已知币种：实时牌价 → 历史缓存 → 已追踪/已监控/有极值数据的币种。 */
  function knownCurrencies() {
    const names = new Set(state.rates.map((item) => item.name));
    state.currencyCache.forEach((name) => names.add(name));
    Object.values(state.chartMonitors).forEach((list) => {
      (list || []).forEach((name) => names.add(name));
    });
    Object.values(state.monitors).forEach((rules) => {
      (rules || []).forEach((rule) => {
        if (rule.currency) names.add(rule.currency);
      });
    });
    Object.keys(state.priceExtremes).forEach((name) => names.add(name));
    return [...names].filter(Boolean);
  }

  function renderRates() {
    const rows = state.rates;
    el.ratesEmpty.hidden = rows.length > 0;
    el.ratesBody.innerHTML = rows
      .map(
        (item) => `
        <tr>
          <td>${escapeHtml(item.name)}</td>
          <td class="code-cell">${escapeHtml(item.code)}</td>
          <td class="num">${escapeHtml(item.buy)}</td>
          <td class="num">${escapeHtml(item.sell)}</td>
          <td class="num">${escapeHtml(item.cash_buy)}</td>
          <td class="num">${escapeHtml(item.cash_sell)}</td>
          <td>
            <button class="add-btn" type="button" data-rate-name="${escapeAttr(item.name)}">监控</button>
          </td>
        </tr>`,
      )
      .join("");
  }

  /** 用已知币种填充下拉，并尽量保留用户已选的币种。 */
  function renderCurrencyOptions() {
    const names = knownCurrencies();
    [
      { select: el.monitorCurrency, empty: "暂无可选币种" },
      { select: el.chartCurrency, empty: "暂无可选币种" },
    ].forEach(({ select, empty }) => {
      const previous = select.value;
      if (!names.length) {
        select.innerHTML = `<option value="">${empty}</option>`;
        return;
      }
      select.innerHTML = names
        .map((name) => `<option value="${escapeAttr(name)}">${escapeHtml(name)}</option>`)
        .join("");
      select.value = names.includes(previous) ? previous : names[0];
    });
    el.chartAddNote.hidden = !state.ratesError;
    el.chartAddNote.textContent = state.ratesError
      ? "牌价暂不可用，新币种需等牌价恢复后才能添加"
      : "";
    updateThresholdHint();
  }

  /** 在阈值输入框右侧显示所选价格类型的当前价。 */
  function updateThresholdHint() {
    const currency = el.monitorCurrency.value;
    const type = el.monitorType.value;
    const typeLabel = PRICE_TYPES[type]?.label || "购汇价";

    if (!currency) {
      el.thresholdHint.textContent = "先选择币种";
      el.thresholdHint.classList.add("is-empty");
      return;
    }

    const rate = state.rates.find((item) => item.name === currency);
    const price = rate ? Number(type === "buy" ? rate.buy : rate.sell) : NaN;
    if (!Number.isFinite(price) || price <= 0) {
      el.thresholdHint.textContent = `暂无${typeLabel}`;
      el.thresholdHint.classList.add("is-empty");
      return;
    }

    // 币种已由左侧下拉体现，这里只强调价格类型与数值
    el.thresholdHint.textContent = `${typeLabel} ${price.toFixed(4)}`;
    el.thresholdHint.classList.remove("is-empty");
  }

  /** 汇总要展示的监控规则，默认覆盖所有已配置会话。 */
  function visibleRules() {
    const sessions = state.onlyCurrent
      ? [state.sessionId].filter(Boolean)
      : Object.keys(state.monitors);
    const rows = [];
    sessions.forEach((sessionId) => {
      (state.monitors[sessionId] || []).forEach((rule) => {
        rows.push({ sessionId, rule });
      });
    });
    return rows;
  }

  function renderMonitors() {
    const rows = visibleRules();
    el.monitorsEmpty.hidden = rows.length > 0;
    el.monitorsEmpty.textContent = state.onlyCurrent
      ? "当前会话还没有监控规则"
      : "还没有任何监控规则";
    el.monitorsCount.textContent = rows.length ? `${rows.length} 条` : "";
    el.monitorsBody.innerHTML = rows
      .map(({ sessionId, rule }) => {
        const typeLabel = PRICE_TYPES[rule.type]?.label || "购汇价";
        const price = rulePrice(rule);
        // 高于：还差「阈值-现价」；低于：还差「现价-阈值」。差值不大于 0 即已越线
        const remain =
          price === null
            ? null
            : rule.condition === "高于"
              ? Number(rule.threshold) - price
              : price - Number(rule.threshold);
        const priceCell =
          price === null ? '<span class="hint">—</span>' : price.toFixed(4);
        const remainCell =
          remain === null
            ? '<span class="hint">—</span>'
            : remain > 0
              ? `<span class="delta">${remain.toFixed(4)}</span>`
              : '<span class="delta is-hit">已越线</span>';
        const triggered = rule.last_triggered;
        return `
        <tr>
          <td class="session-cell">${escapeHtml(sessionId)}</td>
          <td>${escapeHtml(rule.currency)}</td>
          <td>${typeLabel}</td>
          <td>${escapeHtml(`${rule.condition} ${rule.threshold}`)}</td>
          <td class="num">${priceCell}</td>
          <td class="num">${remainCell}</td>
          <td><span class="tag${triggered ? "" : " tag-muted"}">${triggered ? "已推送" : "等待中"}</span></td>
          <td>
            <button class="btn-danger" type="button" data-del-session="${escapeAttr(sessionId)}" data-del-currency="${escapeAttr(rule.currency)}" data-del-type="${escapeAttr(rule.type || "sell")}">删除</button>
          </td>
        </tr>`;
      })
      .join("");
  }

  /** 汇总要展示的会话（只保留确实有追踪币种的会话）。 */
  function visibleChartSessions() {
    const sessions = Object.keys(state.chartMonitors)
      .filter((sid) => (state.chartMonitors[sid] || []).length > 0)
      .sort();
    return state.onlyCurrent
      ? sessions.filter((sid) => sid === state.sessionId)
      : sessions;
  }

  /** 生成单个会话的追踪币种标签组。 */
  function chartChipsHtml(sessionId, names) {
    if (!names.length) {
      return '<span class="hint">—</span>';
    }
    return names
      .map((name) => {
        const points = state.chartPoints[name] || 0;
        const selected = name === state.selectedChart ? " is-selected" : "";
        const pointsText = points ? `已采集 ${points} 次` : "尚未采集";
        return `<span class="chip${selected}">
            <span class="chip-name" role="button" tabindex="0" data-pick-currency="${escapeAttr(name)}">${escapeHtml(name)}</span>
            <span class="hint">${pointsText}</span>
            <button type="button" title="移除追踪" data-drop-session="${escapeAttr(sessionId)}" data-drop-currency="${escapeAttr(name)}">×</button>
          </span>`;
      })
      .join("");
  }

  function renderCharts() {
    const sessions = visibleChartSessions();
    const allNames = new Set();
    sessions.forEach((sid) => {
      (state.chartMonitors[sid] || []).forEach((name) => allNames.add(name));
    });

    el.chartsEmpty.hidden = sessions.length > 0;
    el.chartsEmpty.textContent = state.onlyCurrent
      ? "当前会话还没有追踪币种"
      : "还没有任何追踪币种";
    el.chartsCount.textContent = sessions.length
      ? `${sessions.length} 个会话 · ${allNames.size} 个币种`
      : "";

    el.chartsBody.innerHTML = sessions
      .map((sessionId) => {
        const names = state.chartMonitors[sessionId] || [];
        const isCurrent = sessionId === state.sessionId;
        const autoOn = Boolean(state.chartAutoSend[sessionId]);
        const override = state.chartCronBySession[sessionId] || "";
        const effective = override || state.chartCron;
        // 会话标识拆成「平台 / 类型 + 会话 ID」两行，避免过长挤掉右侧内容
        const parts = sessionId.split(":");
        const platform = parts.length === 3 ? parts[0] : "";
        const typeLabel =
          parts.length === 3
            ? parts[1] === "GroupMessage"
              ? "群"
              : "私聊"
            : "";
        const shortId = parts.length === 3 ? parts[2] : sessionId;
        return `
        <tr${isCurrent ? ' class="is-current"' : ""}>
          <td class="session-cell">
            <span class="session-name" title="${escapeAttr(sessionId)}">${escapeHtml(shortId)}</span>
            <span class="session-meta">
              ${escapeHtml([platform, typeLabel].filter(Boolean).join(" · "))}${isCurrent ? '<span class="tag">当前</span>' : ""}
            </span>
          </td>
          <td><div class="cell-chips">${chartChipsHtml(sessionId, names)}</div></td>
          <td>
            <label class="switch">
              <input type="checkbox" data-auto-session="${escapeAttr(sessionId)}"${autoOn ? " checked" : ""}>
              <span>${autoOn ? "已开启" : "已关闭"}</span>
            </label>
          </td>
          <td class="cell-cron">
            <span class="cron-value">${escapeHtml(effective)}</span>
            ${override ? "" : '<span class="cron-flag">默认</span>'}
          </td>
          <td>
            <div class="cell-actions">
              <button class="btn btn-quiet btn-mini" type="button" data-edit-cron="${escapeAttr(sessionId)}">设置时间</button>
              <button class="btn-danger" type="button" data-clear-session="${escapeAttr(sessionId)}">全部移除</button>
            </div>
          </td>
        </tr>`;
      })
      .join("");

    // 图表选择：只在当前可见的币种范围内保留选中项
    if (state.selectedChart && !allNames.has(state.selectedChart)) {
      state.selectedChart = "";
    }
    if (!state.selectedChart && allNames.size) {
      state.selectedChart = [...allNames][0];
    }
    if (state.selectedChart) {
      drawChart(state.selectedChart);
    } else {
      el.chartTitle.textContent = "走势图";
      el.chartMeta.textContent = "";
      el.chartEmpty.hidden = false;
      clearCanvas();
    }

    el.chartAddLabel.textContent = state.sessionId
      ? `添加追踪币种到 ${state.sessionId}`
      : "添加追踪币种";
  }

  function clearCanvas() {
    const ctx = el.chartCanvas.getContext("2d");
    ctx.clearRect(0, 0, el.chartCanvas.width, el.chartCanvas.height);
  }

  /** 用 Canvas 绘制所选币种的结汇价与购汇价曲线。 */
  async function drawChart(currency) {
    el.chartTitle.textContent = `${currency} 走势`;
    let records = [];
    try {
      const data = await callGet("charts/data", { currency });
      records = data.records || [];
    } catch (error) {
      el.chartMeta.textContent = error.message;
      el.chartEmpty.hidden = false;
      clearCanvas();
      return;
    }

    if (records.length < 2) {
      el.chartMeta.textContent = records.length
        ? "采集次数太少，暂时画不出走势"
        : "还没有采集到数据";
      el.chartEmpty.hidden = false;
      clearCanvas();
      return;
    }

    el.chartEmpty.hidden = true;
    const short = (value) => String(value || "").slice(5, 16);
    el.chartMeta.textContent =
      `${short(records[0].time)} 至 ${short(records[records.length - 1].time)}` +
      ` · 共 ${records.length} 次采集`;

    const canvas = el.chartCanvas;
    const ctx = canvas.getContext("2d");
    const ratio = window.devicePixelRatio || 1;
    const cssWidth = canvas.clientWidth || 960;
    const cssHeight = 360;
    canvas.width = Math.round(cssWidth * ratio);
    canvas.height = Math.round(cssHeight * ratio);
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, cssWidth, cssHeight);

    const styles = window.getComputedStyle(document.documentElement);
    const colors = {
      buy: styles.getPropertyValue("--accent").trim() || "#2f6f5e",
      sell: styles.getPropertyValue("--danger").trim() || "#a8453a",
      ink: styles.getPropertyValue("--ink-2").trim() || "#5c5952",
      faint: styles.getPropertyValue("--ink-3").trim() || "#8a867d",
      grid: styles.getPropertyValue("--line").trim() || "#e2e0db",
    };

    const pad = { top: 18, right: 18, bottom: 52, left: 62 };
    const plotW = cssWidth - pad.left - pad.right;
    const plotH = cssHeight - pad.top - pad.bottom;

    const values = records.flatMap((item) => [Number(item.buy), Number(item.sell)]);
    const finite = values.filter((value) => Number.isFinite(value) && value > 0);
    if (!finite.length) {
      el.chartMeta.textContent = "数据无效";
      el.chartEmpty.hidden = false;
      return;
    }

    let min = Math.min(...finite);
    let max = Math.max(...finite);
    const span = max - min || Math.max(max * 0.01, 0.01);
    min -= span * 0.15;
    max += span * 0.15;

    const x = (index) => pad.left + (plotW * index) / (records.length - 1);
    const y = (value) => pad.top + plotH - ((value - min) / (max - min)) * plotH;

    ctx.font = "11px -apple-system, 'Segoe UI', sans-serif";
    ctx.lineWidth = 1;

    // 横向网格与纵轴刻度
    const gridLines = 4;
    for (let i = 0; i <= gridLines; i += 1) {
      const value = min + ((max - min) * i) / gridLines;
      const py = y(value);
      ctx.strokeStyle = colors.grid;
      ctx.beginPath();
      ctx.moveTo(pad.left, py);
      ctx.lineTo(pad.left + plotW, py);
      ctx.stroke();

      ctx.fillStyle = colors.faint;
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      ctx.fillText(value.toFixed(4), pad.left - 8, py);
    }

    // 横轴时间标签：首尾分别左右对齐，避免被画布裁切；日期与时间分两行更易读
    ctx.fillStyle = colors.faint;
    ctx.textBaseline = "top";
    const tickCount = Math.min(6, records.length);
    let lastDrawn = "";
    for (let i = 0; i < tickCount; i += 1) {
      const index = Math.round(((records.length - 1) * i) / Math.max(tickCount - 1, 1));
      const raw = String(records[index].time || "");
      const datePart = raw.slice(5, 10);
      const timePart = raw.slice(11, 16);
      const label = `${datePart} ${timePart}`;
      // 相邻刻度取到同一时刻时只画一次，避免重复标签
      if (label === lastDrawn) continue;
      lastDrawn = label;

      const first = i === 0;
      const last = i === tickCount - 1;
      ctx.textAlign = first ? "left" : last ? "right" : "center";
      const labelX = first ? pad.left : last ? pad.left + plotW : x(index);
      ctx.fillText(datePart, labelX, pad.top + plotH + 10);
      ctx.fillText(timePart, labelX, pad.top + plotH + 24);
    }

    const drawSeries = (key, color) => {
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.lineJoin = "round";
      ctx.beginPath();
      let started = false;
      records.forEach((item, index) => {
        const value = Number(item[key]);
        if (!Number.isFinite(value) || value <= 0) {
          started = false;
          return;
        }
        if (started) {
          ctx.lineTo(x(index), y(value));
        } else {
          ctx.moveTo(x(index), y(value));
          started = true;
        }
      });
      ctx.stroke();
    };

    drawSeries("buy", colors.buy);
    drawSeries("sell", colors.sell);

    // 图例
    const legend = [
      { color: colors.buy, text: "结汇价" },
      { color: colors.sell, text: "购汇价" },
    ];
    let legendX = pad.left;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    legend.forEach((item) => {
      ctx.fillStyle = item.color;
      ctx.fillRect(legendX, 6, 14, 2);
      ctx.fillStyle = colors.ink;
      ctx.fillText(item.text, legendX + 20, 7);
      legendX += 20 + ctx.measureText(item.text).width + 20;
    });
  }

  /** 切换标签页。 */
  function activateTab(panelId) {
    document.querySelectorAll(".tab").forEach((tab) => {
      const active = tab.dataset.panel === panelId;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
    document.querySelectorAll(".panel").forEach((panel) => {
      const active = panel.id === panelId;
      panel.classList.toggle("is-active", active);
      panel.hidden = !active;
    });
    if (panelId === "panel-charts") {
      renderCharts();
    }
  }

  /**
   * 刷新全部数据。
   * 三个数据源相互独立：牌价失败不应阻断会话/配置的加载，
   * 否则币种下拉会变空、导致无法新增追踪币种。
   */
  async function refreshAll() {
    setStatus("busy", "正在刷新");
    const results = await Promise.allSettled([
      loadState(),
      loadRates(),
      loadUmoOptions(),
    ]);
    const failed = results
      .filter((item) => item.status === "rejected")
      .map((item) => item.reason?.message || "加载失败");

    if (!failed.length) {
      setStatus("", "");
      return;
    }
    setStatus("error", "刷新失败");
    toast(failed[0], true);
  }

  /** 切换当前会话，并刷新依赖会话的视图。 */
  function useSession(next) {
    state.sessionId = next;
    state.selectedChart = "";
    syncSessionUi();
    renderMonitors();
    renderCharts();
  }

  el.refresh.addEventListener("click", refreshAll);

  el.sessionSelect.addEventListener("change", () => {
    useSession(el.sessionSelect.value);
  });

  el.sessionRefresh.addEventListener("click", async () => {
    try {
      await loadUmoOptions();
      toast("已重新扫描会话");
    } catch (error) {
      toast(error.message || "扫描失败", true);
    }
  });

  // ---------- 添加 UMO 对话框 ----------

  /** 按当前选中的机器人渲染可选的群组/用户。 */
  function renderUmoTargets() {
    const platform = state.platforms.find(
      (item) => item.platform_id === el.umoPlatform.value,
    );
    const targets = platform ? platform.targets || [] : [];

    if (state.manualTarget || !targets.length) {
      state.manualTarget = true;
      el.umoTargetField.hidden = true;
      el.umoManualField.hidden = false;
      el.umoManualToggle.textContent = targets.length ? "改为选择列表" : "";
      el.umoManualToggle.hidden = !targets.length;
      el.umoStatus.textContent = targets.length
        ? ""
        : platform?.unavailable || "没有可选的群组或用户";
      return;
    }

    state.manualTarget = false;
    el.umoTargetField.hidden = false;
    el.umoManualField.hidden = true;
    el.umoManualToggle.textContent = "手动填写 UMO";
    el.umoManualToggle.hidden = false;
    // 列表可用时也把获取失败的说明显示出来，便于排查缺项
    el.umoStatus.textContent = platform?.unavailable || "";

    el.umoTarget.innerHTML = targets
      .map((target) => {
        const typeLabel = target.message_type === "GroupMessage" ? "群" : "私聊";
        const name = target.name ? `${target.name}（${target.session_id}）` : target.session_id;
        return `<option value="${escapeAttr(target.umo)}">${escapeHtml(`${typeLabel} · ${name}`)}</option>`;
      })
      .join("");
  }

  /** 打开添加 UMO 对话框并拉取机器人及其群组/好友。 */
  async function openUmoDialog() {
    el.umoDialog.hidden = false;
    state.manualTarget = false;
    el.umoPlatform.innerHTML = '<option value="">加载中…</option>';
    el.umoTarget.innerHTML = "";
    el.umoManual.value = "";
    el.umoStatus.textContent = "";

    try {
      const data = await callGet("platform-targets");
      state.platforms = data.platforms || [];
    } catch (error) {
      state.platforms = [];
      el.umoStatus.textContent = error.message || "读取机器人失败";
    }

    if (!state.platforms.length) {
      el.umoPlatform.innerHTML = '<option value="">无可用机器人</option>';
      state.manualTarget = true;
      renderUmoTargets();
      return;
    }

    el.umoPlatform.innerHTML = state.platforms
      .map(
        (item) =>
          `<option value="${escapeAttr(item.platform_id)}">${escapeHtml(
            item.display === item.platform_id
              ? item.platform_id
              : `${item.display}（${item.platform_id}）`,
          )}</option>`,
      )
      .join("");
    renderUmoTargets();
  }

  function closeUmoDialog() {
    el.umoDialog.hidden = true;
  }

  /** 确认添加：把选中的 UMO 加入下拉并切换过去。 */
  function confirmUmo() {
    const umo = state.manualTarget
      ? el.umoManual.value.trim()
      : el.umoTarget.value;
    if (!umo) {
      toast("请选择群组或用户", true);
      return;
    }
    const parts = umo.split(":", 3);
    if (parts.length !== 3 || parts.some((part) => !part)) {
      toast("UMO 格式应为 机器人:MessageType:会话 ID", true);
      return;
    }
    if (!state.addedSessions.includes(umo) && !state.umoOptions.some((i) => i.umo === umo)) {
      state.addedSessions.push(umo);
    }
    closeUmoDialog();
    useSession(umo);
    toast("已添加并切换到该会话");
  }

  el.sessionAdd.addEventListener("click", openUmoDialog);
  el.umoClose.addEventListener("click", closeUmoDialog);
  el.umoCancel.addEventListener("click", closeUmoDialog);
  el.umoConfirm.addEventListener("click", confirmUmo);
  el.umoPlatform.addEventListener("change", renderUmoTargets);
  el.umoManualToggle.addEventListener("click", () => {
    state.manualTarget = !state.manualTarget;
    renderUmoTargets();
    if (state.manualTarget) el.umoManual.focus();
  });
  el.umoManual.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      confirmUmo();
    }
  });
  el.umoDialog.addEventListener("click", (event) => {
    if (event.target === el.umoDialog) closeUmoDialog();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !el.umoDialog.hidden) closeUmoDialog();
  });

  /** 同步两个面板的「仅看当前会话」开关。 */
  function setOnlyCurrent(value) {
    state.onlyCurrent = value;
    el.onlyCurrent.checked = value;
    el.chartsOnlyCurrent.checked = value;
    renderMonitors();
    renderCharts();
  }

  el.onlyCurrent.addEventListener("change", () => {
    setOnlyCurrent(el.onlyCurrent.checked);
  });

  el.chartsOnlyCurrent.addEventListener("change", () => {
    setOnlyCurrent(el.chartsOnlyCurrent.checked);
  });

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => activateTab(tab.dataset.panel));
  });

  // 牌价表内的「监控」按钮：把币种带入规则表单
  el.ratesBody.addEventListener("click", (event) => {
    const button = event.target.closest("[data-rate-name]");
    if (!button) return;
    el.monitorCurrency.value = button.dataset.rateName;
    activateTab("panel-monitors");
    updateThresholdHint();
    el.monitorThreshold.focus();
  });

  // 币种或价格类型变化时，实时更新阈值旁边的当前价提示
  el.monitorCurrency.addEventListener("change", updateThresholdHint);
  el.monitorType.addEventListener("change", updateThresholdHint);

  el.monitorForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.sessionId) {
      toast("请先填写会话标识", true);
      return;
    }
    if (!el.monitorCurrency.value) {
      toast("请先选择币种", true);
      return;
    }
    try {
      const result = await callPost("monitors/save", {
        session_id: state.sessionId,
        currency: el.monitorCurrency.value,
        type: el.monitorType.value,
        condition: el.monitorCondition.value,
        threshold: el.monitorThreshold.value,
      });
      state.monitors = result.monitors || state.monitors;
      renderMonitors();
      el.monitorThreshold.value = "";
      toast(`${result.action || "保存"}成功：${result.currency}`);
    } catch (error) {
      toast(error.message || "保存失败", true);
    }
  });

  el.monitorsBody.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-del-currency]");
    if (!button) return;
    try {
      // 规则表可能同时展示多个会话，删除必须用该行自己的会话标识
      const result = await callPost("monitors/delete", {
        session_id: button.dataset.delSession,
        currency: button.dataset.delCurrency,
        type: button.dataset.delType,
      });
      state.monitors = result.monitors || state.monitors;
      renderMonitors();
      toast("已删除规则");
    } catch (error) {
      toast(error.message || "删除失败", true);
    }
  });

  el.chartForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.sessionId) {
      toast("请先填写会话标识", true);
      return;
    }
    const currency = el.chartCurrency.value;
    if (!currency) {
      toast("请先选择币种", true);
      return;
    }
    try {
      const result = await callPost("charts/add", {
        session_id: state.sessionId,
        currency,
      });
      state.chartMonitors = result.chart_monitors || state.chartMonitors;
      renderCharts();
      toast("已加入曲线追踪");
    } catch (error) {
      toast(error.message || "添加失败", true);
    }
  });

  // 点击币种查看走势
  el.chartsBody.addEventListener("click", async (event) => {
    const edit = event.target.closest("[data-edit-cron]");
    if (edit) {
      openCronDialog(edit.dataset.editCron);
      return;
    }

    const pick = event.target.closest("[data-pick-currency]");
    if (pick) {
      state.selectedChart = pick.dataset.pickCurrency;
      renderCharts();
      return;
    }

    // 从指定会话移除单个币种
    const drop = event.target.closest("[data-drop-currency]");
    if (drop) {
      const sessionId = drop.dataset.dropSession;
      const currency = drop.dataset.dropCurrency;
      if (!sessionId) return;
      try {
        const result = await callPost("charts/delete", {
          session_id: sessionId,
          currency,
        });
        state.chartMonitors = result.chart_monitors || state.chartMonitors;
        renderCharts();
        toast(`已从该会话移除 ${currency}`);
      } catch (error) {
        toast(error.message || "移除失败", true);
      }
      return;
    }

    // 清空某个会话的全部追踪
    const clear = event.target.closest("[data-clear-session]");
    if (!clear) return;
    const sessionId = clear.dataset.clearSession;
    const names = [...(state.chartMonitors[sessionId] || [])];
    if (!names.length) return;
    try {
      let result = null;
      for (const currency of names) {
        result = await callPost("charts/delete", {
          session_id: sessionId,
          currency,
        });
      }
      if (result) {
        state.chartMonitors = result.chart_monitors || state.chartMonitors;
      }
      renderCharts();
      toast(`已移除该会话的 ${names.length} 个追踪币种`);
    } catch (error) {
      toast(error.message || "移除失败", true);
    }
  });

  // 按会话切换定时推送开关
  el.chartsBody.addEventListener("change", async (event) => {
    const input = event.target.closest("[data-auto-session]");
    if (!input) return;
    const sessionId = input.dataset.autoSession;
    const enabled = input.checked;
    try {
      const result = await callPost("settings", {
        session_id: sessionId,
        chart_auto_send: enabled,
      });
      state.chartAutoSend = result.chart_auto_send || state.chartAutoSend;
      renderCharts();
      toast(enabled ? "已开启该会话的定时推送" : "已关闭该会话的定时推送");
    } catch (error) {
      input.checked = !enabled;
      toast(error.message || "修改失败", true);
    }
  });

  /** 打开推送时间设置弹窗。 */
  function openCronDialog(sessionId) {
    state.cronDialogSession = sessionId;
    const override = state.chartCronBySession[sessionId] || "";
    const effective = override || state.chartCron || "0 12 * * 1-5";

    el.cronSession.textContent = sessionId;
    el.cronDialogInput.value = override;
    el.cronDialogInput.placeholder = state.chartCron || "0 12 * * 1-5";
    el.cronDialogHint.textContent = override
      ? "当前为该会话单独设置"
      : `当前使用默认频率 ${effective}`;
    el.cronReset.hidden = !override;
    el.cronDialog.hidden = false;
    el.cronDialogInput.focus();
  }

  function closeCronDialog() {
    el.cronDialog.hidden = true;
    state.cronDialogSession = "";
  }

  /** 保存或清除某个会话的推送时间。 */
  async function saveCronDialog(cron) {
    const sessionId = state.cronDialogSession;
    if (!sessionId) return;
    try {
      const result = await callPost("settings", {
        session_id: sessionId,
        chart_cron: cron,
      });
      state.chartCronBySession = result.chart_cron_by_session || {};
      renderCharts();
      closeCronDialog();
      toast(cron ? `已设置推送时间：${cron}` : "已恢复为默认推送频率");
    } catch (error) {
      el.cronDialogHint.textContent = error.message || "推送时间设置失败";
      toast(error.message || "推送时间设置失败", true);
    }
  }

  el.cronPresetList.innerHTML = CRON_PRESETS.map(
    (preset) =>
      `<button class="cron-chip" type="button" data-cron="${escapeAttr(preset.cron)}">${escapeHtml(preset.label)}</button>`,
  ).join("");

  el.cronPresetList.addEventListener("click", (event) => {
    const chip = event.target.closest("[data-cron]");
    if (!chip) return;
    el.cronDialogInput.value = chip.dataset.cron;
    el.cronDialogInput.focus();
  });

  el.cronSave.addEventListener("click", () => {
    const cron = el.cronDialogInput.value.trim();
    if (!cron) {
      el.cronDialogHint.textContent = "请填写 Cron 表达式，或点「恢复默认」";
      el.cronDialogInput.focus();
      return;
    }
    saveCronDialog(cron);
  });

  el.cronReset.addEventListener("click", () => {
    saveCronDialog("");
  });

  el.cronCancel.addEventListener("click", closeCronDialog);
  el.cronClose.addEventListener("click", closeCronDialog);
  el.cronDialog.addEventListener("click", (event) => {
    if (event.target === el.cronDialog) closeCronDialog();
  });

  // 保存后台检查频率（抓牌价、采集走势数据并检查阈值的周期）
  el.cronApply.addEventListener("click", async () => {
    const cron = el.cronInput.value.trim();
    if (!cron) {
      toast("请填写检查频率", true);
      el.cronInput.focus();
      return;
    }
    try {
      const result = await callPost("settings", { cron });
      el.cronInput.value = result.cron || cron;
      toast("检查频率已保存");
    } catch (error) {
      toast(error.message || "保存失败", true);
    }
  });

  el.cronInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      el.cronApply.click();
    }
  });

  document.querySelectorAll("[data-cron-target]").forEach((button) => {
    button.addEventListener("click", () => {
      const target = document.getElementById(button.dataset.cronTarget);
      if (target) {
        target.value = button.dataset.cron;
      }
    });
  });

  let resizeTimer = 0;
  window.addEventListener("resize", () => {
    if (!state.selectedChart) return;
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => drawChart(state.selectedChart), 180);
  });

  // 等待 AstrBot 注入的桥接上下文，拿到主题后加载数据
  Promise.resolve(bridge?.ready?.())
    .then((context) => {
      applyTheme(context);
      bridge?.onContext?.(applyTheme);
    })
    .catch(() => applyTheme(null))
    .finally(() => {
      refreshAll();
      // 牌价与规则表现价每 60 秒自动刷新一次，页面挂着也能看到最新值
      window.setInterval(() => {
        loadRates().catch(() => {});
      }, 60000);
    });
})();
