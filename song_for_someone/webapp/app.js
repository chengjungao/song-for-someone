/* 界面逻辑：状态机 + 接口调用 + 歌词防抖体检 + 本地插值进度条。
 *
 * 这个文件只用浏览器原生能力，不引入任何框架、不打任何包。
 * 和网页服务（web.py）之间只通过 JSON 接口说话。
 *
 * 三条关于进度条的红线（写在验收里）：
 *   1. 不出现假百分比（不许把估算当真实进度）；
 *   2. 条子封顶 90%，超过预估就换成流动条纹；
 *   3. 每次生成都固定显示免责脚注。
 */
'use strict';

(function () {
  // ---------------------------------------------------------------- 小工具
  function $(id) { return document.getElementById(id); }

  function esc(text) {
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function numOrNull(value) {
    var raw = String(value == null ? '' : value).trim();
    if (raw === '') { return null; }
    var n = Number(raw);
    return isNaN(n) ? null : n;
  }

  function clampInt(value, fallback, min, max) {
    var n = parseInt(value, 10);
    if (isNaN(n)) { n = fallback; }
    if (n < min) { n = min; }
    if (n > max) { n = max; }
    return n;
  }

  function mmss(seconds) {
    if (seconds == null || isNaN(seconds)) { return null; }
    var total = Math.round(seconds);
    var m = Math.floor(total / 60);
    var s = total % 60;
    return m + ' 分 ' + (s < 10 ? '0' + s : s) + ' 秒';
  }

  function humanKB(bytes) {
    if (bytes == null) { return null; }
    var mb = bytes / (1024 * 1024);
    if (mb < 1) { return (bytes / 1024).toFixed(0) + ' KB'; }
    return mb.toFixed(1) + ' MB';
  }

  function tuneName(code) {
    if (!code) { return null; }
    return String(code)
      .replace(/\bmajor\b/gi, '大调')
      .replace(/\bminor\b/gi, '小调');
  }

  // ---------------------------------------------------------------- 接口
  function api(path, options) {
    var opts = options || {};
    var init = { method: opts.method || 'GET', headers: {} };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    return fetch(path, init).then(function (resp) {
      return resp.json().catch(function () {
        return { ok: false, error: { code: 'bad_response', message: '服务返回的内容看不懂。' } };
      }).then(function (payload) {
        if (!payload.ok) {
          var err = payload.error || {};
          err.status = resp.status;
          throw err;
        }
        return payload.data;
      });
    });
  }

  // ---------------------------------------------------------------- 状态
  var state = {
    meta: null,
    duration: 120,
    lastReport: null,
    forceNext: false,
    yesNext: false,
    taskId: null,
    pollTimer: null,
    tickTimer: null,
    baseElapsed: 0,
    baseAt: 0,
    estimated: 15,
    lastClips: 0
  };

  var SCREENS = ['screen-form', 'screen-progress', 'screen-result', 'screen-env', 'screen-songs'];

  function show(screenId) {
    SCREENS.forEach(function (id) {
      var node = $(id);
      if (node) { node.classList.toggle('hidden', id !== screenId); }
    });
    window.scrollTo(0, 0);
  }

  // ---------------------------------------------------------------- 首屏
  function loadMeta() {
    return api('/api/meta').then(function (data) {
      state.meta = data;
      renderStyles(data.styles || []);
      renderExamples(data.examples || []);
      renderDurations(data.durations || [120]);
    });
  }

  function renderStyles(styles) {
    var select = $('style-select');
    select.innerHTML = '';
    styles.forEach(function (s) {
      var opt = document.createElement('option');
      opt.value = s.key;
      opt.textContent = s.name + '（' + s.badge + '）';
      opt.dataset.prompt = s.prompt || '';
      opt.dataset.duration = s.duration || 120;
      opt.dataset.note = s.note || '';
      select.appendChild(opt);
    });
    if (styles.length) { applyStyle(select.value); }
  }

  function applyStyle(key) {
    var select = $('style-select');
    var opt = select.querySelector('option[value="' + key.replace(/"/g, '') + '"]');
    if (!opt) { return; }
    $('prompt-input').value = opt.dataset.prompt || '';
    $('style-note').textContent = opt.dataset.note || '';
    var dur = Number(opt.dataset.duration);
    if (!isNaN(dur)) { setDuration(dur); }
  }

  function renderExamples(examples) {
    var select = $('example-select');
    examples.forEach(function (item) {
      var opt = document.createElement('option');
      opt.value = item.key;
      opt.textContent = item.name;
      select.appendChild(opt);
    });
  }

  function renderDurations(list) {
    var box = $('duration-options');
    box.innerHTML = '';
    list.forEach(function (seconds) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'chip';
      btn.dataset.value = seconds;
      btn.textContent = labelForDuration(seconds);
      btn.addEventListener('click', function () { setDuration(seconds); });
      box.appendChild(btn);
    });
    var has = list.some(function (s) { return Number(s) === state.duration; });
    setDuration(has ? state.duration : list[0]);
  }

  function labelForDuration(seconds) {
    if (seconds < 60) { return seconds + ' 秒'; }
    var minutes = seconds / 60;
    var text = (Math.round(minutes * 10) / 10) + ' 分钟';
    if (seconds === 120) { text += ' 推荐'; }
    return text;
  }

  function setDuration(seconds) {
    state.duration = seconds;
    var chips = document.querySelectorAll('#duration-options .chip');
    Array.prototype.forEach.call(chips, function (chip) {
      chip.classList.toggle('selected', Number(chip.dataset.value) === Number(seconds));
    });
  }

  // ---------------------------------------------------------------- 环境角标
  function loadEnv() {
    return api('/api/env').then(function (data) {
      var badge = $('env-badge');
      badge.classList.remove('badge-idle', 'badge-ok', 'badge-warn', 'badge-bad');
      if (data.service_ok) {
        badge.classList.add('badge-ok');
        badge.textContent = '环境 正常';
      } else if (data.service_detail) {
        badge.classList.add('badge-warn');
        badge.textContent = '环境 需注意';
      } else {
        badge.classList.add('badge-bad');
        badge.textContent = '环境 不可用';
      }
      badge.dataset.detail = data.service_detail || '';
    }).catch(function () {
      var badge = $('env-badge');
      badge.classList.remove('badge-idle');
      badge.classList.add('badge-warn');
      badge.textContent = '环境 未知';
    });
  }

  // ---------------------------------------------------------------- 歌词体检
  function scheduleCheck() {
    if (state.checkTimer) { clearTimeout(state.checkTimer); }
    state.checkTimer = setTimeout(runCheck, 500);
  }

  function runCheck() {
    var lyrics = $('lyrics-input').value;
    api('/api/lyrics/check', { method: 'POST', body: { lyrics: lyrics } })
      .then(renderCheck)
      .catch(function () { /* 体检失败不打扰用户 */ });
  }

  function renderCheck(report) {
    state.lastReport = report;
    var box = $('lyrics-check');

    $('lyrics-count').textContent = report.total_chars + ' 字 / 上限 ' + report.max_chars;
    $('lyrics-cjk').textContent = '汉字占比 ' + Math.round((report.cjk_ratio || 0) * 100) + '%';

    if (!report.total_chars) {
      box.classList.add('hidden');
      return;
    }
    box.classList.remove('hidden');

    var html = '';

    var statusClass = report.ok ? 'ok' : 'bad';
    var statusText = report.ok
      ? '看着没问题，可以出歌了'
      : '建议先改一下再出歌';
    html += '<div class="check-head ' + statusClass + '">歌词体检 · ' + esc(statusText) + '</div>';

    var labels = { error: '必须改', warn: '建议改', hint: '可以更好' };
    var order = ['error', 'warn', 'hint'];
    var byLevel = { error: [], warn: [], hint: [] };
    (report.issues || []).forEach(function (issue) {
      if (byLevel[issue.level]) { byLevel[issue.level].push(issue); }
    });
    order.forEach(function (level) {
      byLevel[level].forEach(function (issue) {
        var where = (issue.section_index == null) ? '' : '（第 ' + (issue.section_index + 1) + ' 段）';
        html += '<div class="issue issue-' + level + '">'
          + '<span class="issue-tag">' + labels[level] + '</span>'
          + '<span class="issue-text">' + esc(issue.message) + where + '</span></div>';
      });
    });

    if (report.sections && report.sections.length) {
      var parts = report.sections.map(function (s) {
        var mark = s.is_instrumental ? '♪ ' : '';
        return esc(mark + s.label) + ' ' + s.lines + ' 行';
      });
      html += '<div class="structure">歌的结构：' + parts.join(' → ') + '</div>';
    }

    html += '<div class="check-foot">这些来自本机的歌词检查，和命令行里的判断完全一致。歌词是创作，'
      + '这里只是经验区间，你的歌你说了算。</div>';

    box.innerHTML = html;

    var errors = 0;
    var warnings = 0;
    (report.issues || []).forEach(function (issue) {
      if (issue.level === 'error') { errors += 1; }
      else if (issue.level === 'warn') { warnings += 1; }
    });
    updateGate(errors, warnings);
  }

  function updateGate(errors, warnings) {
    var button = $('btn-generate');
    var hint = $('form-hint');
    if (errors > 0) {
      button.textContent = '我知道，还是生成';
      button.classList.add('warn');
      state.forceNext = true;
      hint.textContent = '有 ' + errors + ' 处「必须改」的地方。改好再点更稳；确实想先试试，就直接点上面这个按钮。';
      hint.classList.remove('hidden');
    } else if (warnings > 0) {
      button.textContent = '开始出歌';
      button.classList.remove('warn');
      state.forceNext = false;
      state.yesNext = true;
      hint.textContent = '有 ' + warnings + ' 处「建议改」的地方，不改也能出，直接点「开始出歌」即可。';
      hint.classList.remove('hidden');
    } else {
      button.textContent = '开始出歌';
      button.classList.remove('warn');
      state.forceNext = false;
      state.yesNext = false;
      hint.classList.add('hidden');
    }
  }

  // ---------------------------------------------------------------- 提交生成
  function collectPayload() {
    return {
      prompt: $('prompt-input').value.trim(),
      lyrics: $('lyrics-input').value,
      duration: state.duration,
      out_prefix: $('out-prefix').value.trim(),
      style_key: $('style-select').value,
      force: state.forceNext,
      yes: state.yesNext || state.forceNext,
      fixed_id: numOrNull($('field-fixed-id').value),
      tempo: numOrNull($('field-tempo').value),
      tune: $('field-tune').value.trim(),
      steps: clampInt($('field-steps').value, 8, 1, 100),
      versions: clampInt($('field-versions').value, 1, 1, 8),
      file_type: $('field-file-type').value,
      draft_first: $('field-draft-first').checked
    };
  }

  function submitGenerate() {
    var payload = collectPayload();
    if (!payload.prompt || !payload.lyrics.trim()) {
      var hint = $('form-hint');
      hint.textContent = '请先写好歌词、选好风格。';
      hint.classList.remove('hidden');
      return;
    }
    var button = $('btn-generate');
    button.disabled = true;
    button.textContent = '正在交给程序…';

    api('/api/generate', { method: 'POST', body: payload }).then(function (data) {
      button.disabled = false;
      button.textContent = '开始出歌';
      startProgress(data);
    }).catch(function (err) {
      button.disabled = false;
      button.textContent = '开始出歌';
      handleSubmitError(err);
    });
  }

  function handleSubmitError(err) {
    var code = err.code;
    if (code === 'busy') {
      var hint = $('form-hint');
      hint.textContent = (err.message || '正在生成中。') + ' 正帮你接到那一首的进度。';
      hint.classList.remove('hidden');
      if (err.data && err.data.task_id) {
        startProgress({ task_id: err.data.task_id, estimated_total: err.data.estimated_total, out_name: err.data.out_name });
      }
      return;
    }
    if (code === 'lyrics_error' || code === 'lyrics_warning') {
      var isErr = code === 'lyrics_error';
      var box = $('form-hint');
      box.textContent = (err.message || '') + (isErr ? ' 确实想先试试，就再点一次按钮。' : '');
      box.classList.remove('hidden');
      if (isErr) { state.forceNext = true; $('btn-generate').textContent = '我知道，还是生成'; }
      else { state.yesNext = true; }
      return;
    }
    if (code === 'service_unreachable' || code === 'submit_failed') {
      showEnv(err.message || '出歌的程序还没打开。');
      return;
    }
    showGenError(err);
  }

  // ---------------------------------------------------------------- 进度
  function startProgress(data) {
    stopTimers();
    state.taskId = data.task_id;
    state.estimated = Number(data.estimated_total) || 15;
    state.baseElapsed = 0;
    state.baseAt = performance.now();
    state.lastClips = 0;
    $('progress-lang').textContent = data.language_label || '';
    $('progress-phase').textContent = '正在准备…';
    $('progress-sub').textContent = '';
    setFill(0, false);
    show('screen-progress');

    state.pollTimer = setInterval(pollTask, 1000);
    state.tickTimer = setInterval(tickProgress, 200);
    pollTask();
  }

  function pollTask() {
    if (!state.taskId) { return; }
    api('/api/task/' + state.taskId).then(function (data) {
      state.baseElapsed = Number(data.elapsed) || 0;
      state.baseAt = performance.now();
      state.estimated = Number(data.estimated_total) || state.estimated;
      if (data.language_label) { $('progress-lang').textContent = data.language_label; }

      if (data.status === 'done') {
        stopTimers();
        renderResult(data);
      } else if (data.status === 'failed' || data.status === 'timeout') {
        stopTimers();
        handleTaskError(data.error);
      }
    }).catch(function () { /* 一次轮询失败不致命，下一次再试 */ });
  }

  function liveElapsed() {
    return state.baseElapsed + (performance.now() - state.baseAt) / 1000;
  }

  function tickProgress() {
    var elapsed = liveElapsed();
    var ratio = state.estimated > 0 ? elapsed / state.estimated : 1;
    var phase = phaseFor(ratio);
    $('progress-phase').textContent = phase.title;
    $('progress-sub').textContent = phase.sub(elapsed, state.estimated);
    setFill(phase.width, phase.flowing);
  }

  function phaseFor(ratio) {
    if (ratio <= 0.15) {
      return {
        title: '正在把你的歌词交给模型',
        width: ratio * 100,
        flowing: false,
        sub: function (elapsed) { return '已经等了 ' + Math.round(elapsed) + ' 秒'; }
      };
    }
    if (ratio <= 0.40) {
      return { title: '正在理解歌词，安排段落', width: ratio * 100, flowing: false, sub: etaText };
    }
    if (ratio <= 0.75) {
      return { title: '正在编曲', width: ratio * 100, flowing: false, sub: etaText };
    }
    if (ratio <= 1.0) {
      return { title: '正在演唱', width: 75 + (ratio - 0.75) * 60, flowing: false, sub: etaText };
    }
    return {
      title: '还在唱，比平时久一些',
      width: 100,
      flowing: true,
      sub: function (elapsed) { return '已经等了 ' + Math.round(elapsed) + ' 秒，还在等。'; }
    };
  }

  function etaText(elapsed, estimated) {
    var remain = Math.max(3, Math.round(estimated - elapsed));
    return '已经等了 ' + Math.round(elapsed) + ' 秒 · 估计还要 ' + remain + ' 秒左右';
  }

  function setFill(width, flowing) {
    var bar = $('progress-bar');
    var capped = Math.max(0, Math.min(90, width));
    bar.style.width = capped + '%';
    bar.classList.toggle('flowing', !!flowing);
    $('progress-track').classList.toggle('overflowing', !!flowing);
  }

  function stopTimers() {
    if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
    if (state.tickTimer) { clearInterval(state.tickTimer); state.tickTimer = null; }
  }

  // ---------------------------------------------------------------- 结果
  function renderResult(data) {
    var result = data.result || {};
    var files = result.files || [];
    var clips = Math.round(Number(data.elapsed) || 0);

    $('result-success').classList.remove('hidden');
    $('result-error').classList.add('hidden');
    $('result-title').textContent = '好了，出歌用了 ' + clips + ' 秒';
    $('result-name').textContent = data.out_name || (files[0] && files[0].name) || '';

    var first = files[0];
    if (first) {
      var audio = $('result-audio');
      audio.src = first.media_url;
      var dl = $('result-download');
      dl.href = first.download_url;
      dl.setAttribute('download', first.name);
    }

    var metaBits = [];
    if (first) { metaBits.push(humanKB(first.size_bytes)); }
    var durText = mmss(result.duration);
    if (durText) { metaBits.push('时长 ' + durText); }
    if (result.bpm != null) { metaBits.push(result.bpm + ' 拍/分钟'); }
    var tn = tuneName(result.key);
    if (tn) { metaBits.push(tn); }
    $('result-meta').textContent = metaBits.filter(Boolean).join(' · ');

    var summary = result.summary || {};
    $('result-lang').textContent = summary.language_label || '';

    var extra = files.slice(1);
    var filesBox = $('result-files');
    if (extra.length) {
      filesBox.innerHTML = '<p class="note">这次一共出了 ' + files.length + ' 版，逐版试听：' + '</p>'
        + extra.map(function (f) {
          return '<div class="alt-file"><span>' + esc(f.name) + '</span>'
            + '<audio controls preload="none" src="' + esc(f.media_url) + '"></audio>'
            + '<a class="link-btn" href="' + esc(f.download_url) + '" download>下载</a></div>';
        }).join('');
      filesBox.classList.remove('hidden');
    } else {
      filesBox.innerHTML = '';
    }

    renderDetails(summary);
    renderReproduce(result.reproduce);
    show('screen-result');
  }

  function renderDetails(summary) {
    var rows = [
      ['歌多长', summary.duration != null ? labelForDuration(summary.duration) : null],
      ['演唱语言', summary.language_label],
      ['每分钟多少拍', summary.tempo != null ? summary.tempo : '模型自定'],
      ['调式', summary.tune ? summary.tune : '模型自定'],
      ['生成步数', summary.steps],
      ['一次出几版', summary.versions],
      ['文件格式', summary.file_type],
      ['让模型先打草稿', summary.draft_first ? '开' : '关'],
      ['固定编号', summary.fixed_id != null ? summary.fixed_id : '未固定（每次都不一样）']
    ];
    $('result-details').innerHTML = rows.map(function (row) {
      return '<div class="detail-row"><dt>' + esc(row[0]) + '</dt><dd>' + esc(row[1] == null ? '—' : row[1]) + '</dd></div>';
    }).join('');
  }

  function renderReproduce(command) {
    var block = $('reproduce-block');
    var node = $('reproduce-text');
    if (command) {
      // 命令行原文放在折叠区里，等宽显示、可一键复制。
      // 这是给「会电脑的」读者的进阶入口：同一件东西，网页里能做，命令行也能做。
      node.textContent = command;
      block.classList.remove('hidden');
    } else {
      node.textContent = '';
      block.classList.add('hidden');
    }
  }

  function copyReproduce() {
    var text = $('reproduce-text').textContent || '';
    if (!text) { return; }
    var button = $('btn-copy-reproduce');
    var flash = function () {
      button.textContent = '已复制';
      setTimeout(function () { button.textContent = '复制'; }, 1500);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(flash).catch(function () { fallbackCopy(text, flash); });
    } else {
      fallbackCopy(text, flash);
    }
  }

  function fallbackCopy(text, done) {
    var area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'absolute';
    area.style.left = '-9999px';
    document.body.appendChild(area);
    area.select();
    try { document.execCommand('copy'); } catch (err) { /* 复制不了就算了，命令就在眼前 */ }
    document.body.removeChild(area);
    done();
  }

  function handleTaskError(error) {
    var code = (error && error.code) || 'generate_failed';
    if (code === 'service_unreachable' || code === 'submit_failed') {
      showEnv((error && error.message) || '出歌的程序还没打开。');
      return;
    }
    showGenError(error || {});
  }

  function showGenError(error) {
    $('result-success').classList.add('hidden');
    $('result-error').classList.remove('hidden');

    var titles = {
      generate_failed: '这次没生成出来',
      timeout: '等太久了，已停止等待',
      download_failed: '歌出来了，但没能存到电脑上',
      cancelled: '已停止等待'
    };
    $('error-title').textContent = titles[error.code] || '这次没生成出来';
    $('error-message').textContent = error.message || '出错了。';

    var detail = error.detail || '';
    var wrap = $('error-detail-wrap');
    if (detail) {
      $('error-detail').textContent = detail;
      wrap.classList.remove('hidden');
    } else {
      wrap.classList.add('hidden');
    }
    show('screen-result');
  }

  // ---------------------------------------------------------------- 环境页
  function showEnv(message) {
    $('env-title').textContent = '出歌的程序还没打开';
    $('env-lead').textContent = (message || '')
      + ' 你电脑上负责「真正写歌」的那个程序没有在运行，所以现在点也没用。'
      + '它是另外的一个软件，需要先单独打开它。';
    $('doctor-panel').classList.add('hidden');
    show('screen-env');
    loadEnv();
  }

  function loadDoctor() {
    var panel = $('doctor-panel');
    panel.classList.remove('hidden');
    panel.innerHTML = '<p class="note">正在检查…（第一次可能要等一两分钟）</p>';
    api('/api/doctor').then(function (report) {
      panel.innerHTML = renderDoctor(report);
    }).catch(function () {
      panel.innerHTML = '<p class="note">自检没能跑完，稍后再试。</p>';
    });
  }

  function renderDoctor(report) {
    var colors = { ok: 'ok', warn: 'warn', fail: 'bad', skip: 'skip' };
    var html = '<h3>环境自检</h3>';
    (report.checks || []).forEach(function (c) {
      var cls = colors[c.level] || 'skip';
      html += '<div class="doctor-row doctor-' + cls + '">'
        + '<span class="doctor-tag">' + esc(c.level_label) + '</span>'
        + '<div class="doctor-body"><strong>' + esc(c.title) + '</strong>';
      if (c.detail) { html += '<div class="doctor-detail">' + esc(c.detail) + '</div>'; }
      if (c.fix) { html += '<div class="doctor-fix">怎么办：' + esc(c.fix) + '</div>'; }
      html += '</div></div>';
    });
    return html;
  }

  // ---------------------------------------------------------------- 我的作品
  function loadSongs() {
    var list = $('songs-list');
    list.innerHTML = '<p class="note">正在读取…</p>';
    show('screen-songs');
    api('/api/songs').then(function (data) {
      var songs = data.songs || [];
      if (!songs.length) {
        list.innerHTML = '<p class="note">还没有出过的歌。出第一首以后，这里就能找到它。</p>';
        return;
      }
      list.innerHTML = songs.map(function (s, index) {
        var bits = [];
        if (s.size_bytes != null) { bits.push(humanKB(s.size_bytes)); }
        if (s.bpm != null) { bits.push(s.bpm + ' 拍/分钟'); }
        var tn = tuneName(s.key);
        if (tn) { bits.push(tn); }
        if (s.created_at) { bits.push(esc(s.created_at)); }
        var canRefill = !!s.refill;
        return '<div class="song-row">'
          + '<div class="song-main"><div class="song-name">' + esc(s.name) + '</div>'
          + '<div class="song-meta">' + esc(bits.join(' · ')) + '</div></div>'
          + '<audio controls preload="none" src="' + esc(s.media_url) + '"></audio>'
          + '<div class="song-actions">'
          + '<a class="link-btn" href="' + esc(s.download_url) + '" download>下载</a>'
          + (canRefill ? '<button class="link-btn js-refill" type="button" data-index="' + index + '">照这版再来一次</button>' : '')
          + '</div></div>';
      }).join('');

      Array.prototype.forEach.call(list.querySelectorAll('.js-refill'), function (btn) {
        btn.addEventListener('click', function () { refillFrom(songs[Number(btn.dataset.index)]); });
      });
    }).catch(function () {
      list.innerHTML = '<p class="note">读取作品列表失败了。</p>';
    });
  }

  function refillFrom(song) {
    var r = song.refill || {};
    $('prompt-input').value = r.prompt || '';
    $('lyrics-input').value = r.lyrics || '';
    if (r.duration != null) { setDuration(Number(r.duration)); }
    if (r.fixed_id != null) { $('field-fixed-id').value = r.fixed_id; }
    if (r.tempo != null) { $('field-tempo').value = r.tempo; }
    $('field-tune').value = r.tune || '';
    if (r.steps != null) { $('field-steps').value = r.steps; }
    if (r.versions != null) { $('field-versions').value = r.versions; }
    if (r.file_type) { $('field-file-type').value = r.file_type; }
    $('field-draft-first').checked = r.draft_first !== false;
    runCheck();
    show('screen-form');
  }

  // ---------------------------------------------------------------- 事件绑定
  function bindEvents() {
    $('style-select').addEventListener('change', function (event) { applyStyle(event.target.value); });

    $('lyrics-input').addEventListener('input', scheduleCheck);

    $('example-select').addEventListener('change', function (event) {
      var key = event.target.value;
      if (!key) { return; }
      api('/api/example?name=' + encodeURIComponent(key)).then(function (data) {
        $('lyrics-input').value = data.text || '';
        runCheck();
      }).catch(function () { /* 忽略 */ });
      event.target.value = '';
    });

    $('btn-clear').addEventListener('click', function () {
      $('lyrics-input').value = '';
      $('lyrics-count').textContent = '0 字 / 上限 4096';
      $('lyrics-cjk').textContent = '汉字占比 —';
      $('lyrics-check').classList.add('hidden');
      state.lastReport = null;
      state.forceNext = false;
      state.yesNext = false;
      $('btn-generate').textContent = '开始出歌';
      $('btn-generate').classList.remove('warn');
      $('form-hint').classList.add('hidden');
    });

    $('btn-generate').addEventListener('click', submitGenerate);
    $('btn-songs').addEventListener('click', loadSongs);
    $('btn-back-form-2').addEventListener('click', function () { show('screen-form'); });

    $('env-badge').addEventListener('click', function () { loadDoctor(); show('screen-env'); loadEnv(); });

    $('btn-env-from-progress').addEventListener('click', function () { showEnv(''); });
    $('btn-retry').addEventListener('click', function () {
      loadEnv();
      show('screen-form');
      if ($('lyrics-input').value.trim()) { submitGenerate(); }
    });
    $('btn-doctor').addEventListener('click', loadDoctor);
    $('btn-back-form').addEventListener('click', function () { show('screen-form'); });

    $('btn-again').addEventListener('click', function () { show('screen-form'); submitGenerate(); });
    $('btn-edit').addEventListener('click', function () { show('screen-form'); });
    $('btn-copy-reproduce').addEventListener('click', copyReproduce);

    $('btn-error-retry').addEventListener('click', function () { show('screen-form'); submitGenerate(); });
    $('btn-error-doctor').addEventListener('click', function () { loadDoctor(); show('screen-env'); });
    $('btn-error-back').addEventListener('click', function () { show('screen-form'); });
  }

  // ---------------------------------------------------------------- 启动
  function resumeCurrent() {
    return api('/api/tasks/current').then(function (data) {
      if (data && data.task_id) {
        startProgress({ task_id: data.task_id, estimated_total: data.estimated_total, out_name: data.out_name });
        return true;
      }
      return false;
    }).catch(function () { return false; });
  }

  function boot() {
    bindEvents();
    show('screen-form');
    loadMeta().catch(function () { /* 忽略首屏数据失败 */ });
    loadEnv();
    resumeCurrent();
  }

  document.addEventListener('DOMContentLoaded', boot);
})();
