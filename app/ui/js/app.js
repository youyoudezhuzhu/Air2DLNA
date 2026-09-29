/*
 * Air2DLNA —— Web UI 前端脚本
 * ---------------------------------------------------------------------------
 * 纯原生 JavaScript：无框架 / 无构建步骤 / 无外部 CDN 依赖。
 *
 * 约定：
 *   1. 所有 fetch 都经过 apiRequest()，统一带 X-Requested-With: XMLHttpRequest
 *      （后端对写操作有此 CSRF 校验要求），并统一用 AbortController 做超时。
 *   2. 所有服务端字符串一律用 textContent 写入 DOM，绝不拼 innerHTML，避免 XSS。
 *   3. 任何异常都会被捕获（safe() / 统一错误处理），页面不会抛出未捕获错误；
 *      请求失败时显示断线横幅并继续重试。
 */
(function () {
  'use strict';

  /* ======================================================================
   * 常量
   * ==================================================================== */

  // 使用相对路径（UI 以 iframe 形式挂在服务根路径下），兼容子路径部署。
  var API = {
    status: 'api/status',
    config: 'api/config',
    renderers: 'api/renderers',
    discover: 'api/renderers/discover',
    select: 'api/renderer/select',
    logs: 'api/logs',
    health: 'api/health'
  };

  var POLL = {
    status: 2000,             // 状态轮询：2s
    renderersDiscovering: 3000, // 搜索中的设备列表轮询：3s
    renderersIdle: 15000,     // 空闲时的设备列表轮询：15s
    logsAuto: 3000,           // 日志自动刷新间隔
    progress: 500             // 进度条本地插值刷新
  };

  var REQUEST_TIMEOUT_MS = 8000;
  var LOG_LINES = 300;
  var DEFAULT_AIRPLAY_NAME = 'Feiniu AirPlay';

  // 播放/渲染器状态中文映射
  var STATE_LABEL = {
    STOPPED: '已停止',
    PAUSED: '已暂停',
    PLAYING: '播放中',
    BUFFERING: '缓冲中',
    ERROR: '错误'
  };
  var STATE_BADGE = {
    STOPPED: 'badge-idle',
    PAUSED: 'badge-warn',
    PLAYING: 'badge-ok',
    BUFFERING: 'badge-info',
    ERROR: 'badge-error'
  };
  // mDNS 状态映射
  var MDNS_LABEL = {
    registered: 'mDNS 已注册',
    pending: 'mDNS 注册中',
    failed: 'mDNS 注册失败'
  };
  var MDNS_CLASS = {
    registered: 'pill-ok',
    pending: 'pill-warn',
    failed: 'pill-error'
  };

  /* ======================================================================
   * 运行时状态（模块私有，不污染全局）
   * ==================================================================== */

  var state = {
    statusFailures: 0,
    statusInFlight: false,
    renderersInFlight: false,
    logsInFlight: false,
    discovering: false,
    renderers: [],
    renderersSignature: '',
    status: null,
    config: null,
    airplayName: '',
    progressBase: null,       // { position, duration, state, at } 用于本地平滑推进
    logLines: [],
    logsAutoTimer: null,
    statusTimer: null,
    renderersTimer: null,
    progressTimer: null,
    artworkRetried: false,
    focusedRendererUdn: null
  };

  var el = {}; // DOM 元素缓存

  /* ======================================================================
   * 小工具
   * ==================================================================== */

  function $(id) {
    return document.getElementById(id);
  }

  function setText(node, text) {
    if (!node) { return; }
    node.textContent = (text === null || text === undefined) ? '' : String(text);
  }

  function setHidden(node, hidden) {
    if (!node) { return; }
    node.hidden = !!hidden;
  }

  /** 设置内联消息（成功/失败/提示） */
  function setMsg(node, text, kind) {
    if (!node) { return; }
    node.textContent = text || '';
    node.className = 'msg' + (kind ? ' msg-' + kind : '');
  }

  function errMsg(err) {
    if (!err) { return '未知错误'; }
    if (typeof err.message === 'string' && err.message) { return err.message; }
    return String(err);
  }

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  /** 毫秒 -> mm:ss（未知返回 --:--） */
  function formatTime(ms) {
    if (typeof ms !== 'number' || !isFinite(ms) || ms < 0) { return '--:--'; }
    var total = Math.floor(ms / 1000);
    var m = Math.floor(total / 60);
    var s = total % 60;
    return (m < 10 ? '0' + m : String(m)) + ':' + (s < 10 ? '0' + s : String(s));
  }

  /** 秒 -> 1天2小时3分；不足 1 小时时带上秒，避免显示成“0分” */
  function formatUptime(seconds) {
    if (typeof seconds !== 'number' || !isFinite(seconds) || seconds < 0) { return '—'; }
    var total = Math.floor(seconds);
    var d = Math.floor(total / 86400);
    var h = Math.floor((total % 86400) / 3600);
    var m = Math.floor((total % 3600) / 60);
    var s = total % 60;

    if (d > 0 || h > 0) {
      // 形如 1天2小时3分（整小时/整天时省略末段）
      return (d > 0 ? d + '天' : '') + (h > 0 ? h + '小时' : '') + (m > 0 ? m + '分' : '');
    }
    if (m > 0) { return m + '分' + (s > 0 ? s + '秒' : ''); }
    return total + '秒';
  }

  function stateLabel(s) {
    return STATE_LABEL[s] || '未知';
  }

  function stateBadgeClass(s) {
    return STATE_BADGE[s] || 'badge-idle';
  }

  /** 只记录到控制台，绝不向上抛 */
  function warn() {
    try {
      if (window.console && typeof window.console.warn === 'function') {
        window.console.warn.apply(window.console, ['[air2dlna]'].concat(Array.prototype.slice.call(arguments)));
      }
    } catch (e) { /* 忽略 */ }
  }

  /** 包裹事件处理器：同步异常与 Promise 拒绝都不会外泄 */
  function safe(fn) {
    return function () {
      var args = arguments;
      var self = this;
      try {
        var result = fn.apply(self, args);
        if (result && typeof result.then === 'function') {
          result.then(null, function (err) { warn('异步处理失败：', errMsg(err)); });
        }
        return result;
      } catch (err) {
        warn('处理失败：', errMsg(err));
        return undefined;
      }
    };
  }

  /* ======================================================================
   * 统一的 fetch 封装
   *   - 始终带 X-Requested-With
   *   - AbortController 超时
   *   - 统一把 HTTP 错误 / 解析错误 / 超时归一化成 Error
   * ==================================================================== */

  function apiRequest(path, options) {
    var opts = options || {};
    var controller = (typeof window.AbortController === 'function') ? new window.AbortController() : null;
    var timer = null;

    var headers = {
      'X-Requested-With': 'XMLHttpRequest',
      'Accept': 'application/json'
    };

    var init = {
      method: opts.method || 'GET',
      headers: headers,
      credentials: 'same-origin',
      cache: 'no-store'
    };

    // 只有提交 JSON 时才带 Content-Type
    if (opts.body !== undefined && opts.body !== null) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }

    if (controller) {
      init.signal = controller.signal;
      timer = window.setTimeout(function () {
        try { controller.abort(); } catch (e) { /* 忽略 */ }
      }, opts.timeout || REQUEST_TIMEOUT_MS);
    }

    function clearTimer() {
      if (timer) { window.clearTimeout(timer); timer = null; }
    }

    return window.fetch(path, init).then(function (res) {
      // 统一按文本读取，再尝试解析 JSON（日志/错误体都可能是纯文本）
      return res.text().then(function (text) {
        var data = null;
        if (text) {
          try { data = JSON.parse(text); } catch (e) { data = null; }
        }
        if (!res.ok) {
          var message = (data && typeof data.error === 'string' && data.error)
            ? data.error
            : ('请求失败（HTTP ' + res.status + '）');
          var httpErr = new Error(message);
          httpErr.status = res.status;
          throw httpErr;
        }
        if (data === null) {
          var parseErr = new Error('服务器返回了无法解析的数据');
          parseErr.status = res.status;
          throw parseErr;
        }
        return data;
      });
    }).then(function (data) {
      clearTimer();
      return data;
    }, function (err) {
      clearTimer();
      if (err && err.name === 'AbortError') {
        var timeoutErr = new Error('请求超时');
        timeoutErr.timeout = true;
        throw timeoutErr;
      }
      if (err instanceof Error) { throw err; }
      throw new Error('网络请求失败');
    });
  }

  /* ======================================================================
   * 断线横幅
   * ==================================================================== */

  function setDisconnected(disconnected, detail) {
    setHidden(el.netBanner, !disconnected);
    if (disconnected) {
      var text = detail ? '（' + detail + '）' : '';
      setText(el.netBannerMsg, '正在重试…' + text);
    }
  }

  /* ======================================================================
   * 状态轮询与渲染
   * ==================================================================== */

  function pollStatus() {
    if (state.statusInFlight) { return; }
    state.statusInFlight = true;

    apiRequest(API.status).then(function (data) {
      state.statusFailures = 0;
      state.status = data || {};
      setDisconnected(false);
      renderStatus(state.status);
    }, function (err) {
      state.statusFailures += 1;
      setDisconnected(true, errMsg(err));
    }).then(function () {
      state.statusInFlight = false;
    });
  }

  function renderStatus(data) {
    var airplay = (data && data.airplay) || {};
    var server = (data && data.server) || {};
    var renderer = (data && data.renderer) || null;
    var playback = (data && data.playback) || {};

    /* ---- 头部状态胶囊 ---- */
    if (airplay.running) {
      setPill(el.pillAirplay, 'AirPlay：运行中', 'pill-ok');
    } else {
      setPill(el.pillAirplay, 'AirPlay：已停止', 'pill-error');
    }

    var mdns = airplay.mdns;
    if (mdns && MDNS_LABEL[mdns]) {
      setPill(el.pillMdns, MDNS_LABEL[mdns], MDNS_CLASS[mdns] || 'pill-unknown');
    } else {
      setPill(el.pillMdns, 'mDNS：未知', 'pill-unknown');
    }

    var ip = (typeof server.nas_ip === 'string' && server.nas_ip) ? server.nas_ip : '—';
    var port = (typeof server.http_port === 'number') ? server.http_port : '—';
    setPill(el.pillAddr, ip + ':' + port, 'pill-muted');

    /* ---- 当前播放 ---- */
    renderPlayback(playback, renderer);

    /* ---- 服务信息 ---- */
    setText(el.infoVersion, (typeof server.version === 'string' && server.version) ? server.version : '—');
    setText(el.infoUptime, formatUptime(server.uptime_s));
    setText(el.infoRtsp, (typeof airplay.rtsp_port === 'number') ? String(airplay.rtsp_port) : '—');
    setText(el.infoHttp, (typeof server.http_port === 'number') ? String(server.http_port) : '—');

    var client = airplay.client;
    if (client && client.connected) {
      var cname = (typeof client.name === 'string' && client.name) ? client.name : '未知设备';
      var cip = (typeof client.ip === 'string' && client.ip) ? client.ip : '';
      setText(el.infoClient, cip ? (cname + '（' + cip + '）') : cname);
    } else {
      setText(el.infoClient, '无');
    }

    /* ---- 设备离线警告（以列表数据优先，状态接口兜底） ---- */
    updateRendererWarning();
  }

  function setPill(node, text, cls) {
    if (!node) { return; }
    node.textContent = text;
    node.className = 'pill ' + (cls || 'pill-unknown');
  }

  /* ---------- 当前播放 ---------- */

  function renderPlayback(playback, renderer) {
    var pb = playback || {};
    var st = pb.state || 'STOPPED';

    // 状态徽章
    el.playState.textContent = stateLabel(st);
    el.playState.className = 'badge ' + stateBadgeClass(st);

    // 曲目信息
    var title = pb.title;
    if (typeof title !== 'string' || !title) {
      title = (st === 'PLAYING' || st === 'BUFFERING' || st === 'PAUSED') ? '未知曲目' : '未在播放';
    }
    setText(el.trackTitle, title);
    setText(el.trackArtist, (typeof pb.artist === 'string' && pb.artist) ? pb.artist : '—');
    setText(el.trackAlbum, (typeof pb.album === 'string' && pb.album) ? pb.album : '—');

    // 封面（artwork_url 可能为 null / 404）
    renderArtwork(pb.artwork_url);

    // 进度：记录基准值，由 tickProgress() 在轮询间隔内平滑推进
    var pos = (typeof pb.position_ms === 'number' && isFinite(pb.position_ms)) ? pb.position_ms : null;
    var dur = (typeof pb.duration_ms === 'number' && isFinite(pb.duration_ms) && pb.duration_ms > 0) ? pb.duration_ms : null;
    state.progressBase = { position: pos || 0, duration: dur, state: st, at: Date.now() };
    renderProgress(pos, dur);

    // 音量：仅显示，绝不回传
    var volume = (typeof pb.volume === 'number' && isFinite(pb.volume))
      ? pb.volume
      : ((renderer && typeof renderer.volume === 'number' && isFinite(renderer.volume)) ? renderer.volume : null);
    var muted = (pb.muted === true) || !!(renderer && renderer.muted === true);
    renderVolume(volume, muted);

    // 输出设备 + 音频格式
    if (renderer && renderer.name) {
      setText(el.playRenderer, renderer.name + '（' + stateLabel(renderer.state || 'STOPPED') + '）');
    } else {
      setText(el.playRenderer, '未选择 DLNA 设备');
    }
    setText(el.audioFormat, (typeof pb.audio_format === 'string' && pb.audio_format) ? pb.audio_format : '—');
  }

  function renderVolume(volume, muted) {
    var hasVolume = (typeof volume === 'number' && isFinite(volume));
    var shown = hasVolume ? Math.round(clamp(volume, 0, 100)) : 0;
    el.volume.value = String(shown);
    el.volume.disabled = true; // 显示型控件：音量由 AirPlay 端控制
    if (!hasVolume) {
      setText(el.volumeText, '--');
    } else if (muted) {
      setText(el.volumeText, shown + '% 静音');
    } else {
      setText(el.volumeText, shown + '%');
    }
  }

  /** 本地插值推进进度条（仅在 PLAYING 且已知总时长时） */
  function tickProgress() {
    try {
      var base = state.progressBase;
      if (!base || base.state !== 'PLAYING' || !base.duration) { return; }
      var pos = Math.min(base.duration, base.position + (Date.now() - base.at));
      renderProgress(pos, base.duration);
    } catch (e) {
      // 进度只是视觉效果，任何异常都忽略
    }
  }

  function renderProgress(position, duration) {
    var hasPos = (typeof position === 'number' && isFinite(position) && position >= 0);
    var hasDur = (typeof duration === 'number' && isFinite(duration) && duration > 0);

    setText(el.timeCurrent, hasPos ? formatTime(position) : '--:--');
    setText(el.timeTotal, hasDur ? formatTime(duration) : '--:--');

    var percent = (hasPos && hasDur) ? clamp((position / duration) * 100, 0, 100) : 0;
    el.progressFill.style.width = percent.toFixed(2) + '%';

    if (el.progressBar) {
      el.progressBar.setAttribute('aria-valuenow', String(Math.round(percent)));
      el.progressBar.setAttribute('aria-valuetext',
        (hasPos ? formatTime(position) : '--:--') + ' / ' + (hasDur ? formatTime(duration) : '--:--'));
    }
  }

  /* ---------- 封面图 ---------- */

  function renderArtwork(url) {
    if (typeof url !== 'string' || !url) {
      el.artwork.removeAttribute('data-src');
      hideArtwork();
      return;
    }
    // 同一张封面不重复加载，避免闪烁
    if (el.artwork.getAttribute('data-src') === url) { return; }
    el.artwork.setAttribute('data-src', url);
    state.artworkRetried = false;
    el.artwork.hidden = false;
    setHidden(el.artworkPlaceholder, true);
    el.artwork.src = withCacheBuster(url);
  }

  function withCacheBuster(url) {
    // 曲目切换后封面 URL 可能不变，加时间戳避免浏览器缓存旧图
    return url + (url.indexOf('?') === -1 ? '?' : '&') + '_ts=' + Date.now();
  }

  function hideArtwork() {
    // 已隐藏时直接返回：避免移除 src 触发新的 error 事件造成循环
    if (el.artwork.hidden) { return; }
    el.artwork.hidden = true;
    el.artwork.removeAttribute('src');
    setHidden(el.artworkPlaceholder, false);
  }

  /** 封面加载失败：先退回原始 URL 重试一次，仍失败则隐藏元素 */
  function onArtworkError() {
    var original = el.artwork.getAttribute('data-src');
    if (original && !state.artworkRetried) {
      state.artworkRetried = true;
      el.artwork.src = original;
      return;
    }
    hideArtwork();
  }

  /* ======================================================================
   * DLNA 设备列表
   * ==================================================================== */

  function pollRenderers() {
    if (state.renderersInFlight) {
      scheduleRenderers();
      return;
    }
    state.renderersInFlight = true;

    apiRequest(API.renderers).then(function (data) {
      var list = (data && Array.isArray(data.renderers)) ? data.renderers : [];
      state.renderers = list;
      setDiscovering(!!(data && data.discovering));
      renderRenderers(list);
      updateRendererWarning();
    }, function (err) {
      setMsg(el.rendererMsg, '读取设备列表失败：' + errMsg(err), 'error');
    }).then(function () {
      state.renderersInFlight = false;
      scheduleRenderers();
    });
  }

  /** 根据是否正在搜索，动态决定下一次轮询间隔 */
  function scheduleRenderers() {
    if (state.renderersTimer) { window.clearTimeout(state.renderersTimer); }
    var delay = state.discovering ? POLL.renderersDiscovering : POLL.renderersIdle;
    state.renderersTimer = window.setTimeout(pollRenderers, delay);
  }

  function setDiscovering(on) {
    var was = state.discovering;
    state.discovering = !!on;
    setHidden(el.discoverStatus, !state.discovering);
    el.btnDiscover.disabled = state.discovering;
    if (was && !state.discovering) {
      setMsg(el.rendererMsg, '搜索完成', 'ok');
      window.setTimeout(function () {
        // 稍后自动清除“搜索完成”，避免长期占用提示位
        if (el.rendererMsg && el.rendererMsg.textContent === '搜索完成') {
          setMsg(el.rendererMsg, '', '');
        }
      }, 4000);
    }
  }

  function renderRenderers(list) {
    // 记住焦点所在行，重建后恢复，避免键盘操作被打断
    var listHasFocus = el.rendererList.contains(document.activeElement);
    var focusedUdn = state.focusedRendererUdn;

    var signature = JSON.stringify(list.map(function (r) {
      return [r && r.udn, r && r.name, r && r.ip, r && r.model, r && r.manufacturer,
              r && r.online, r && r.selected, r && r.supported_mime];
    }));

    setHidden(el.rendererEmpty, list.length > 0);

    // 数据未变化则不重建 DOM（防止每 2/3 秒闪烁）
    if (signature === state.renderersSignature) {
      if (listHasFocus && focusedUdn) { focusRendererRow(focusedUdn); }
      return;
    }
    state.renderersSignature = signature;

    // 清空列表（textContent = '' 安全且彻底）
    el.rendererList.textContent = '';

    if (!list.length) { return; }

    var frag = document.createDocumentFragment();
    list.forEach(function (r) {
      frag.appendChild(buildRendererRow(r || {}));
    });
    el.rendererList.appendChild(frag);

    if (listHasFocus && focusedUdn) { focusRendererRow(focusedUdn); }
  }

  function focusRendererRow(udn) {
    var buttons = el.rendererList.querySelectorAll('.renderer-row');
    for (var i = 0; i < buttons.length; i++) {
      if (buttons[i].getAttribute('data-udn') === udn) {
        try { buttons[i].focus(); } catch (e) { /* 忽略 */ }
        return;
      }
    }
  }

  function buildRendererRow(r) {
    var udn = (typeof r.udn === 'string') ? r.udn : '';
    var name = (typeof r.name === 'string' && r.name) ? r.name : '未命名设备';
    var ip = (typeof r.ip === 'string' && r.ip) ? r.ip : '—';
    var online = (r.online !== false);

    var li = document.createElement('li');

    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'renderer-row' + (r.selected ? ' is-selected' : '') + (online ? '' : ' is-offline');
    btn.setAttribute('role', 'radio');
    btn.setAttribute('aria-checked', r.selected ? 'true' : 'false');
    btn.setAttribute('data-udn', udn);
    btn.setAttribute('aria-label', name + '，' + ip + '，' + (online ? '在线' : '离线') + (r.selected ? '，当前使用' : ''));

    // 单选指示圆点
    var radio = document.createElement('span');
    radio.className = 'radio';
    radio.setAttribute('aria-hidden', 'true');

    // 名称 / 型号 / 厂商
    var main = document.createElement('span');
    main.className = 'renderer-main';

    var nameEl = document.createElement('strong');
    nameEl.className = 'renderer-name';
    nameEl.textContent = name;
    main.appendChild(nameEl);

    var metaParts = [];
    if (typeof r.model === 'string' && r.model) { metaParts.push(r.model); }
    if (typeof r.manufacturer === 'string' && r.manufacturer) { metaParts.push(r.manufacturer); }
    var meta = document.createElement('span');
    meta.className = 'renderer-meta';
    meta.textContent = metaParts.length ? metaParts.join(' · ') : '型号未知';
    main.appendChild(meta);

    // 右侧：IP + 在线徽章
    var side = document.createElement('span');
    side.className = 'renderer-side';

    var ipEl = document.createElement('span');
    ipEl.className = 'renderer-ip mono';
    ipEl.textContent = ip;
    side.appendChild(ipEl);

    var badge = document.createElement('span');
    badge.className = 'badge ' + (online ? 'badge-ok' : 'badge-off');
    badge.textContent = online ? '在线' : '离线';
    side.appendChild(badge);

    if (r.selected) {
      var chip = document.createElement('span');
      chip.className = 'badge badge-info';
      chip.textContent = '使用中';
      side.appendChild(chip);
    }

    btn.appendChild(radio);
    btn.appendChild(main);
    btn.appendChild(side);

    // 点击整行即选择该设备（离线设备同样可选，仅提示警告）
    btn.addEventListener('click', safe(function () {
      state.focusedRendererUdn = udn;
      selectRenderer(udn, name, online);
    }));

    li.appendChild(btn);
    return li;
  }

  /** 选择 DLNA 渲染器 */
  function selectRenderer(udn, name, online) {
    if (!udn) {
      setMsg(el.rendererMsg, '该设备缺少 UDN，无法选择', 'error');
      return;
    }
    if (!online) {
      setMsg(el.rendererMsg, '注意：「' + name + '」当前离线，选择后桥接不会自动切换设备', 'info');
    } else {
      setMsg(el.rendererMsg, '正在切换到「' + name + '」…', 'info');
    }

    return apiRequest(API.select, { method: 'POST', body: { udn: udn } }).then(function () {
      setMsg(el.rendererMsg, '已选择「' + name + '」', 'ok');
      // 立即刷新列表与状态，让高亮/警告即时更新
      state.renderersSignature = ''; // 强制重建列表
      pollRenderers();
      pollStatus();
    }, function (err) {
      setMsg(el.rendererMsg, '选择设备失败：' + errMsg(err), 'error');
    });
  }

  function startDiscover() {
    setDiscovering(true);
    setMsg(el.rendererMsg, '正在搜索 DLNA 设备…', 'info');
    return apiRequest(API.discover, { method: 'POST' }).then(function () {
      // 立即拉一次列表；之后的轮询由 scheduleRenderers() 以 3s 间隔驱动
      if (state.renderersTimer) { window.clearTimeout(state.renderersTimer); }
      pollRenderers();
    }, function (err) {
      setDiscovering(false);
      setMsg(el.rendererMsg, '启动搜索失败：' + errMsg(err), 'error');
    });
  }

  /** 已选设备离线时给出明确警告：不会自动切换 */
  function updateRendererWarning() {
    var selected = null;
    var list = state.renderers || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i] && list[i].selected) { selected = list[i]; break; }
    }
    // 列表还没数据时，退回使用 /api/status 里的 renderer
    if (!selected && state.status && state.status.renderer && state.status.renderer.udn) {
      selected = state.status.renderer;
    }

    if (selected && selected.online === false) {
      var name = (typeof selected.name === 'string' && selected.name) ? selected.name : '未命名设备';
      setText(el.rendererWarnDetail,
        '已选择的设备「' + name + '」当前不可达。桥接器不会自动切换到其他设备；' +
        '请检查音响电源与网络，或在下方手动选择另一台设备。');
      setHidden(el.rendererWarn, false);
    } else {
      setHidden(el.rendererWarn, true);
      setText(el.rendererWarnDetail, '');
    }
  }

  /* ======================================================================
   * 配置（AirPlay 名称）
   * ==================================================================== */

  function loadConfig() {
    return apiRequest(API.config).then(function (cfg) {
      state.config = cfg || {};
      var name = state.config.airplay_name;
      if (typeof name === 'string' && name) {
        if (!el.airplayName.value || el.airplayName.value === state.airplayName) {
          el.airplayName.value = name;
        }
        state.airplayName = name;
      }
      setMsg(el.nameMsg, '', '');
    }, function (err) {
      setMsg(el.nameMsg, '读取配置失败：' + errMsg(err), 'error');
    });
  }

  function saveAirplayName() {
    var value = (el.airplayName.value || '').trim();
    if (!value) {
      setMsg(el.nameMsg, '名称不能为空', 'error');
      try { el.airplayName.focus(); } catch (e) { /* 忽略 */ }
      return;
    }
    if (value.length > 63) {
      setMsg(el.nameMsg, '名称过长（最多 63 个字符）', 'error');
      return;
    }
    if (value === state.airplayName) {
      setMsg(el.nameMsg, '名称未变化，无需保存', 'info');
      return;
    }

    el.btnSaveName.disabled = true;
    setMsg(el.nameMsg, '正在保存…', 'info');

    // 只提交 airplay_name 一个字段
    return apiRequest(API.config, { method: 'PUT', body: { airplay_name: value } }).then(function (res) {
      var saved = (res && res.config && typeof res.config.airplay_name === 'string' && res.config.airplay_name)
        ? res.config.airplay_name
        : value;
      state.airplayName = saved;
      el.airplayName.value = saved;
      setMsg(el.nameMsg, '已保存：' + saved + '（AirPlay 广播名稍后生效）', 'ok');
    }, function (err) {
      setMsg(el.nameMsg, '保存失败：' + errMsg(err), 'error');
    }).then(function () {
      el.btnSaveName.disabled = false;
    });
  }

  /* ======================================================================
   * 日志
   * ==================================================================== */

  function loadLogs() {
    if (state.logsInFlight) { return; }
    state.logsInFlight = true;

    apiRequest(API.logs + '?lines=' + LOG_LINES).then(function (data) {
      state.logLines = (data && Array.isArray(data.lines)) ? data.lines : [];
      var now = new Date();
      var hh = ('0' + now.getHours()).slice(-2);
      var mm = ('0' + now.getMinutes()).slice(-2);
      var ss = ('0' + now.getSeconds()).slice(-2);
      setMsg(el.logsMsg, '更新时间 ' + hh + ':' + mm + ':' + ss, '');
      renderLogs();
    }, function (err) {
      setMsg(el.logsMsg, '读取日志失败：' + errMsg(err), 'error');
    }).then(function () {
      state.logsInFlight = false;
    });
  }

  /** 在单行日志里识别级别（客户端过滤，不请求后端） */
  function detectLevel(line) {
    var m = /\b(DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL)\b/i.exec(String(line));
    if (!m) { return 'other'; }
    var v = m[1].toUpperCase();
    if (v === 'WARNING') { return 'warn'; }
    if (v === 'CRITICAL') { return 'error'; }
    return v.toLowerCase(); // debug | info | warn | error
  }

  function renderLogs() {
    // 级别为空时按“全部”处理，避免 select 未初始化导致日志不显示
    var level = (el.logsLevel && el.logsLevel.value) ? el.logsLevel.value : 'all';
    var lines = (state.logLines || []).filter(function (line) {
      return level === 'all' || detectLevel(line) === level;
    });

    if (!lines.length) {
      el.logs.textContent = state.logLines.length ? '（当前筛选条件下没有日志）' : '（暂无日志）';
    } else {
      // textContent：日志内容不会被当作 HTML 解析，天然防 XSS
      el.logs.textContent = lines.join('\n');
    }

    // 自动刷新开启时滚动到底部看最新日志
    if (el.logsAuto && el.logsAuto.checked) {
      el.logs.scrollTop = el.logs.scrollHeight;
    }
  }

  function setLogsAutoRefresh(on) {
    if (state.logsAutoTimer) {
      window.clearInterval(state.logsAutoTimer);
      state.logsAutoTimer = null;
    }
    if (on) {
      state.logsAutoTimer = window.setInterval(function () {
        loadLogs();
      }, POLL.logsAuto);
      loadLogs();
    }
  }

  /* ======================================================================
   * 初始化
   * ==================================================================== */

  function cacheElements() {
    el.netBanner = $('net-banner');
    el.netBannerMsg = $('net-banner-msg');

    el.pillAirplay = $('pill-airplay');
    el.pillMdns = $('pill-mdns');
    el.pillAddr = $('pill-addr');

    el.airplayName = $('airplay-name');
    el.btnSaveName = $('btn-save-name');
    el.nameMsg = $('name-msg');

    el.btnDiscover = $('btn-discover');
    el.btnRefresh = $('btn-refresh');
    el.discoverStatus = $('discover-status');
    el.rendererMsg = $('renderer-msg');
    el.rendererWarn = $('renderer-warn');
    el.rendererWarnDetail = $('renderer-warn-detail');
    el.rendererList = $('renderer-list');
    el.rendererEmpty = $('renderer-empty');

    el.playState = $('play-state');
    el.trackTitle = $('track-title');
    el.trackArtist = $('track-artist');
    el.trackAlbum = $('track-album');
    el.artwork = $('artwork');
    el.artworkPlaceholder = $('artwork-placeholder');
    el.progressBar = $('progress-bar');
    el.progressFill = $('progress-fill');
    el.timeCurrent = $('time-current');
    el.timeTotal = $('time-total');
    el.volume = $('volume');
    el.volumeText = $('volume-text');
    el.audioFormat = $('audio-format');
    el.playRenderer = $('play-renderer');

    el.infoVersion = $('info-version');
    el.infoUptime = $('info-uptime');
    el.infoRtsp = $('info-rtsp');
    el.infoHttp = $('info-http');
    el.infoClient = $('info-client');

    el.btnLogsRefresh = $('btn-logs-refresh');
    el.logsAuto = $('logs-auto');
    el.logsLevel = $('logs-level');
    el.logsMsg = $('logs-msg');
    el.logs = $('logs');
  }

  function bindEvents() {
    el.btnSaveName.addEventListener('click', safe(saveAirplayName));

    // 输入框回车即保存
    el.airplayName.addEventListener('keydown', safe(function (ev) {
      if (ev && ev.key === 'Enter') {
        ev.preventDefault();
        saveAirplayName();
      }
    }));

    el.btnDiscover.addEventListener('click', safe(startDiscover));
    el.btnRefresh.addEventListener('click', safe(function () {
      setMsg(el.rendererMsg, '正在刷新设备列表…', 'info');
      state.renderersSignature = ''; // 强制重建
      pollRenderers();
    }));

    // 封面加载失败（404 / 格式不支持）时隐藏图片
    el.artwork.addEventListener('error', safe(onArtworkError));

    el.btnLogsRefresh.addEventListener('click', safe(loadLogs));
    el.logsAuto.addEventListener('change', safe(function () {
      setLogsAutoRefresh(!!el.logsAuto.checked);
    }));
    el.logsLevel.addEventListener('change', safe(renderLogs));

    // 音量滑块为显示型控件，这里不绑定任何回传逻辑
    el.volume.addEventListener('input', safe(function () {
      renderVolume(Number(el.volume.value), false);
    }));

    // 页面重新可见时立刻刷新一次，避免后台标签页数据陈旧
    document.addEventListener('visibilitychange', safe(function () {
      if (!document.hidden) {
        pollStatus();
        pollRenderers();
        if (el.logsAuto.checked) { loadLogs(); }
      }
    }));
  }

  function init() {
    cacheElements();
    bindEvents();

    // 初始占位：默认名称提示
    el.airplayName.placeholder = DEFAULT_AIRPLAY_NAME;

    // 首屏请求（各自独立失败，互不影响）
    loadConfig();
    loadLogs();
    pollStatus();
    pollRenderers();

    // 定时轮询
    state.statusTimer = window.setInterval(pollStatus, POLL.status);
    state.progressTimer = window.setInterval(tickProgress, POLL.progress);
  }

  // 未捕获的 Promise 拒绝只记录，不影响页面
  window.addEventListener('unhandledrejection', function (ev) {
    warn('未处理的异步错误：', ev && ev.reason ? errMsg(ev.reason) : '');
    if (ev && typeof ev.preventDefault === 'function') { ev.preventDefault(); }
  });

  // 同步未捕获异常同样只记录
  window.addEventListener('error', function (ev) {
    warn('页面错误：', ev && ev.message ? ev.message : '');
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', safe(init));
  } else {
    safe(init)();
  }
})();
