(function () {
  "use strict";

  var DEFAULT_API = "http://127.0.0.1:8790";
  var MIN_RECORD_MS = 400;
  var MAX_RECORD_MS = 20000;
  var MAX_TEXT = 300;
  var MIME_LIST = [
    "audio/webm;codecs=opus", "audio/webm",
    "audio/ogg;codecs=opus", "audio/mp4"
  ];

  var talkBtn = document.getElementById("voice-talk");
  var handsBtn = document.getElementById("voice-handsfree");
  var replayBtn = document.getElementById("voice-replay");
  var sendBtn = document.getElementById("voice-send");
  var stateEl = document.getElementById("voice-state");
  var inputEl = document.getElementById("voice-text");
  // 手机上输入框单独贴在底部，不进控制台；PC 上这块是隐藏的
  var phoneForm = document.getElementById("phone-chat");
  var phoneInput = document.getElementById("phone-chat-text");
  var asrEl = document.getElementById("voice-asr");
  var replyEl = document.getElementById("voice-reply");

  if (!talkBtn || !stateEl) {
    return;
  }

  var apiBase = null;
  var apiTried = false;
  var health = null;
  var stream = null;
  var recorder = null;
  var chunks = [];
  var startedAt = 0;
  var stopTimer = null;
  var recording = false;
  var busy = false;
  var queue = [];
  var playing = false;
  var current = null;
  var wasSpeaking = false;
  var lastUrls = [];
  var firstMs = 0;
  var pendingIncoming = [];
  var audioGen = 0;        // 每次打断/开新录音递增，晚到的 TTS 回包直接丢弃
  var turnGen = 0;         // 每轮对话递增，旧轮补合成的语音不再插播
  var FETCH_TIMEOUT_MS = 90000;   // 服务端等模型/ASR 时可能很慢，但不能永远卡住

  // 免提：麦克风一直开着，检测到人声自动录一句，停顿后自动发送
  var handsFree = false;
  var vadTimer = null;
  var audioCtx = null;
  var analyser = null;
  var vadData = null;
  var vadHits = 0;
  var vadSilenceAt = 0;
  var hfCooldownUntil = 0;
  var discardRecord = false;
  var HF_THRESHOLD = 0.035;      // 时域 RMS 阈值，安静房间约 0.01，正常说话约 0.08+
  var HF_SILENCE_MS = 900;       // 停顿多久算说完
  var HF_COOLDOWN_MS = 700;      // 瑾瑜说完后的静默，避免自己的尾音被当成新一句
  var HF_TICK_MS = 80;

  // 手机和电脑是同一场通话：会话键固定，表情/动作/待播话两端都走同一个
  function sessionId() {
    return "stage-shared";
  }

  // 每台设备一个实例号，只用来区分「这句是谁说的」，不参与记忆
  var CLIENT = (function () {
    try {
      var got = window.sessionStorage.getItem("rp.client");
      if (!got) {
        got = "c-" + Math.random().toString(36).slice(2, 10);
        window.sessionStorage.setItem("rp.client", got);
      }
      return got;
    } catch (error) {
      return "c-local";
    }
  })();

  var SESSION = sessionId();

  function setState(text, state) {
    stateEl.textContent = text;
    stateEl.dataset.state = state || "idle";
  }

  function beginTimeout(ms) {
    if (!window.AbortController) {
      return { signal: undefined, done: function () {} };
    }
    var ctrl = new AbortController();
    var timer = window.setTimeout(function () { ctrl.abort(); }, ms);
    return {
      signal: ctrl.signal,
      done: function () { window.clearTimeout(timer); }
    };
  }

  function timedFetch(url, options, ms) {
    var guard = beginTimeout(ms);
    var opts = options || {};
    if (guard.signal) {
      opts.signal = guard.signal;
    }
    return fetch(url, opts).then(function (resp) {
      guard.done();
      return resp;
    }, function (error) {
      guard.done();
      if (error && error.name === "AbortError") {
        throw new Error("请求超时，稍后再试");
      }
      throw error;
    });
  }

  function resolveApi() {
    if (apiBase) {
      return Promise.resolve(apiBase);
    }
    if (apiTried) {
      return Promise.resolve(null);
    }
    apiTried = true;
    var bases = [];
    if (window.RP_API_BASE) {
      bases.push(window.RP_API_BASE.replace(/\/$/, ""));
    }
    if (window.location.origin && window.location.origin !== "null") {
      bases.push(window.location.origin);
    }
    bases.push(DEFAULT_API);
    var unique = bases.filter(function (base, index) {
      return bases.indexOf(base) === index;
    });

    function tryNext(at) {
      if (at >= unique.length) {
        return Promise.resolve(null);
      }
      var base = unique[at];
      return timedFetch(base + "/health", { cache: "no-store" }, 8000)
        .then(function (resp) {
          if (!resp.ok) {
            throw new Error("HTTP " + resp.status);
          }
          return resp.json();
        })
        .then(function (info) {
          apiBase = base;
          health = info;
          return base;
        })
        .catch(function () {
          return tryNext(at + 1);
        });
    }

    return tryNext(0);
  }

  function postJson(base, path, body, timeoutMs) {
    return timedFetch(base + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }, timeoutMs || FETCH_TIMEOUT_MS).then(function (resp) {
      return resp.json().catch(function () {
        return { ok: false, error: "HTTP " + resp.status };
      }).then(function (data) {
        if (!resp.ok || !data.ok) {
          throw new Error(data.error || ("HTTP " + resp.status));
        }
        return data;
      });
    });
  }

  function pickMime() {
    if (!window.MediaRecorder) {
      return "";
    }
    for (var i = 0; i < MIME_LIST.length; i += 1) {
      if (MediaRecorder.isTypeSupported(MIME_LIST[i])) {
        return MIME_LIST[i];
      }
    }
    return "";
  }

  function ensureMic() {
    if (stream) {
      return Promise.resolve(stream);
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return Promise.reject(new Error("当前浏览器不支持麦克风（需要 localhost 或 HTTPS）"));
    }
    return navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
    }).then(function (got) {
      stream = got;
      return got;
    });
  }

  // 只关掉自己占用的那条麦克风流，避免误停别处的录音
  function dropMic(mic) {
    if (!mic || stream !== mic) {
      return;
    }
    mic.getTracks().forEach(function (track) { track.stop(); });
    stream = null;
  }

  function blobToBase64(blob) {
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () {
        var raw = String(reader.result || "");
        resolve(raw.indexOf(",") >= 0 ? raw.split(",")[1] : raw);
      };
      reader.onerror = function () { reject(new Error("读取录音失败")); };
      reader.readAsDataURL(blob);
    });
  }

  // ---- 免提：用 AnalyserNode 的时域数据算音量，检测到人声就自动录音 ----
  function vadLevel() {
    if (!analyser || !vadData) {
      return 0;
    }
    analyser.getByteTimeDomainData(vadData);
    var sum = 0;
    for (var i = 0; i < vadData.length; i += 1) {
      var v = (vadData[i] - 128) / 128;
      sum += v * v;
    }
    return Math.sqrt(sum / vadData.length);
  }

  function vadTick() {
    if (!handsFree || !analyser) {
      return;
    }
    var now = performance.now();
    // 瑾瑜在说话 / 正在请求时完全不收声，避免把它自己的尾音当成人话
    if (busy || playing || queue.length || pendingIncoming.length ||
        now < hfCooldownUntil) {
      vadHits = 0;
      vadSilenceAt = 0;
      return;
    }
    var level = vadLevel();
    if (recording) {
      if (level >= HF_THRESHOLD) {
        vadSilenceAt = 0;
      } else if (!vadSilenceAt) {
        vadSilenceAt = now;
      } else if (now - vadSilenceAt >= HF_SILENCE_MS) {
        vadSilenceAt = 0;
        stopRecord();
      }
      return;
    }
    if (level >= HF_THRESHOLD) {
      vadHits += 1;
      if (vadHits >= 2) {
        vadHits = 0;
        startRecord(true);
      }
    } else {
      vadHits = 0;
    }
  }

  function hfStart() {
    if (handsFree) {
      return;
    }
    if (!window.MediaRecorder) {
      fail("当前浏览器不支持录音（需要 localhost 或 HTTPS）");
      return;
    }
    handsFree = true;
    if (handsBtn) {
      handsBtn.setAttribute("aria-pressed", "true");
    }
    setState("正在打开免提…", "loading");
    ensureMic().then(function (mic) {
      if (!handsFree) {
        // 麦克风还没拿到用户就关了免提，别让麦克风一直开着
        dropMic(mic);
        return;
      }
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) {
        throw new Error("当前浏览器不支持音频分析");
      }
      audioCtx = new Ctx();
      analyser = audioCtx.createAnalyser();
      analyser.fftSize = 512;
      analyser.smoothingTimeConstant = 0.4;
      vadData = new Uint8Array(analyser.fftSize);
      audioCtx.createMediaStreamSource(mic).connect(analyser);
      vadHits = 0;
      vadSilenceAt = 0;
      hfCooldownUntil = 0;
      if (audioCtx.state === "suspended" && audioCtx.resume) {
        audioCtx.resume();
      }
      vadTimer = window.setInterval(vadTick, HF_TICK_MS);
      setState("免提已开：直接说话，瑾瑜会自己接", "ready");
    }).catch(function (error) {
      hfStop(null);
      fail("麦克风不可用：" + (error && error.message ? error.message : error));
    });
  }

  function hfStop(message) {
    handsFree = false;
    if (handsBtn) {
      handsBtn.setAttribute("aria-pressed", "false");
    }
    if (vadTimer) {
      window.clearInterval(vadTimer);
      vadTimer = null;
    }
    vadHits = 0;
    vadSilenceAt = 0;
    analyser = null;
    vadData = null;
    if (audioCtx) {
      try { audioCtx.close(); } catch (error) {}
      audioCtx = null;
    }
    if (recording) {
      discardRecord = true;
      stopRecord();
    }
    if (stream) {
      stream.getTracks().forEach(function (track) { track.stop(); });
      stream = null;
    }
    if (message) {
      setState(message, "idle");
    }
  }

  function stopPlayback() {
    audioGen += 1;             // 作废还在合成中的旧语音，避免录音时突然插播
    queue = [];
    playing = false;
    wasSpeaking = false;
    if (current) {
      try { current.pause(); } catch (error) {}
      current = null;
    }
  }

  function pump() {
    if (playing) {
      return;
    }
    var url = queue.shift();
    if (!url) {
      if (wasSpeaking) {
        wasSpeaking = false;
        hfCooldownUntil = performance.now() + HF_COOLDOWN_MS;
        setState(handsFree
          ? "免提已开：直接说话，瑾瑜会自己接"
          : "说完 · 首句 " + firstMs + " ms", "ready");
      }
      return;
    }
    wasSpeaking = true;
    playing = true;
    var audio = new Audio(url);
    current = audio;
    setState("正在说话…", "loading");
    audio.onended = function () {
      playing = false;
      current = null;
      pump();
    };
    audio.onerror = function () {
      playing = false;
      current = null;
      pump();
    };
    var started = audio.play();
    if (started && started.catch) {
      started.catch(function () {
        playing = false;
        current = null;
        pump();
      });
    }
  }

  function enqueue(base64) {
    if (!base64) {
      return;
    }
    var url = "data:audio/mpeg;base64," + base64;
    queue.push(url);
    lastUrls.push(url);
    replayBtn.hidden = false;
    pump();
  }

  function renderAsr(data) {
    asrEl.hidden = false;
    asrEl.dataset.error = "false";
    var mode = data.mode && data.mode.asr;
    asrEl.textContent = (mode === "text" ? "（打字）" : "识别：") + String(data.asr || "");
  }

  function renderReply(data) {
    var notes = [];
    if (data.mode && data.mode.llm === "mock") {
      notes.push("（本地兜底：没接上模型）");
    }
    if (data.mode && data.mode.tts !== "real") {
      notes.push("（语音合成不可用：请装 edge-tts）");
    }
    replyEl.hidden = false;
    replyEl.textContent = String(data.reply || "").trim() +
      (notes.length ? "\n" + notes.join(" ") : "");
  }

  // 服务器 AI 回包里的舞台指令交给 Live2D 控制器执行
  function applyStage(stage) {
    try {
      if (stage && window.JinyuStage) {
        window.JinyuStage.apply(stage);
      }
    } catch (error) {
      console.warn("[voice] stage apply failed:", error);
    }
  }

  function fail(message) {
    setState(message, "error");
    replyEl.hidden = false;
    replyEl.textContent = message;
  }

  // 瑾瑜在别的窗口（QQ/桌宠）主动说到通话里：显示正文并合成语音
  function speakIncoming(item) {
    var text = String((item && item.text) || "").trim();
    if (!text) {
      return;
    }
    replyEl.hidden = false;
    replyEl.textContent = text;
    applyStage(item.stage);
    setState(item.origin === "proactive" ? "瑾瑜主动开口…" : "瑾瑜从别的窗口说话…", "loading");
    var gen = audioGen;
    resolveApi().then(function (base) {
      return base ? postJson(base, "/call/tts", { text: text }) : null;
    }).then(function (data) {
      if (gen !== audioGen) {
        // 用户已经开始录音/新的一轮，旧话不再插播
        return;
      }
      if (data && data.audio) {
        enqueue(data.audio);
      } else {
        setState("收到瑾瑜的话（没合成出声音）", "idle");
      }
    }).catch(function () {
      if (gen !== audioGen) {
        return;
      }
      setState("收到瑾瑜的话（语音合成失败）", "idle");
    });
  }

  function flushPending() {
    if (busy || recording || !pendingIncoming.length) {
      return;
    }
    while (pendingIncoming.length) {
      speakIncoming(pendingIncoming.shift());
    }
  }

  function pollInbox() {
    if (!apiBase) {
      return;
    }
    timedFetch(apiBase + "/call/inbox?session=" + encodeURIComponent(SESSION)
          + "&client=" + encodeURIComponent(CLIENT),
          { cache: "no-store" }, 10000)
      .then(function (resp) { return resp.ok ? resp.json() : null; })
      .then(function (data) {
        if (!data || !data.ok || !data.items || !data.items.length) {
          return;
        }
        data.items.forEach(function (item) {
          if (busy || recording) {
            pendingIncoming.push(item);
          } else {
            speakIncoming(item);
          }
        });
      })
      .catch(function () {});
  }

  window.setInterval(pollInbox, 4000);

  // 每分钟问一次服务端：AI 看着当前画面，有没有想主动说的话
  var proactiveAt = Date.now();
  var proactiveMode = "";
  var PROACTIVE_MS = 60000;

  function maybeProactive() {
    if (busy || recording || document.hidden) {
      return;
    }
    var mode = (window.JinyuScreen && window.JinyuScreen.mode)
      ? (window.JinyuScreen.mode() || "") : "";
    if (!mode) {
      return;
    }
    var now = Date.now();
    if (mode !== proactiveMode) {
      // 刚打开/切换了画面：先看满一分钟再考虑开口
      proactiveMode = mode;
      proactiveAt = now;
      return;
    }
    if (now - proactiveAt < PROACTIVE_MS) {
      return;
    }
    proactiveAt = now;
    resolveApi().then(function (base) {
      if (!base) {
        return null;
      }
      return postJson(base, "/call/proactive", { session: SESSION, view: mode });
    }).then(function (data) {
      if (!data || !data.ok || !data.speak || !data.text) {
        return;
      }
      var item = { text: data.text, stage: data.stage, origin: "proactive" };
      if (busy || recording) {
        pendingIncoming.push(item);
      } else {
        speakIncoming(item);
      }
    }).catch(function () {});
  }

  window.setInterval(maybeProactive, 15000);

  function sendTurn(body, kind) {
    if (busy) {
      return;
    }
    busy = true;
    proactiveAt = Date.now();
    stopPlayback();
    var gen = ++turnGen;
    lastUrls = [];
    replayBtn.hidden = true;
    asrEl.hidden = true;
    replyEl.hidden = true;
    firstMs = 0;
    setState(kind === "voice" ? "识别中…" : "思考中…", "loading");

    var began = Date.now();
    resolveApi().then(function (base) {
      if (!base) {
        throw new Error("通话服务未启动：请运行 python call_server.py");
      }
      body.session = SESSION;
      body.client = CLIENT;
      // 告诉服务端当前打开的是屏幕还是摄像头，避免拿另一路的旧画面充数
      body.view = (window.JinyuScreen && window.JinyuScreen.mode)
        ? (window.JinyuScreen.mode() || "none") : "";
      return postJson(base, "/call/turn", body);
    }).then(function (data) {
      busy = false;
      if (data.pass) {
        // 这一轮模型没给正文，通话这边不出声，也不报错
        renderAsr(data);
        replyEl.hidden = false;
        replyEl.textContent = "（这一轮没出声，主人再说一次？）";
        setState("瑾瑜没出声", "idle");
        flushPending();
        return;
      }
      firstMs = (data.ms && data.ms.first_audio) || 0;
      var waited = Date.now() - began;
      renderAsr(data);
      renderReply(data);
      applyStage(data.stage);
      if (data.audio) {
        setState("说完 · 首句 " + firstMs + " ms（前端等了 " + waited + " ms）", "ready");
        enqueue(data.audio);
      } else {
        setState(data.note || "没合成出声音，先看文字", "idle");
      }
      if (data.rest) {
        var rest = data.rest;
        resolveApi().then(function (base) {
          return base ? postJson(base, "/call/tts", { text: rest }) : null;
        }).then(function (more) {
          if (gen !== turnGen) {
            // 用户已经开了新的一轮：旧句子不再补播
            return;
          }
          if (more && more.audio) {
            enqueue(more.audio);
          }
          flushPending();
        }).catch(function () {
          if (gen === turnGen) {
            flushPending();
          }
        });
      } else {
        flushPending();
      }
    }).catch(function (error) {
      busy = false;
      fail("通话失败：" + error.message);
      flushPending();
    });
  }

  function startRecord(auto) {
    if (busy || recording) {
      return;
    }
    if (!window.MediaRecorder) {
      fail("当前浏览器不支持录音（需要 localhost 或 HTTPS）");
      return;
    }
    recording = true;
    startedAt = Date.now();
    if (!auto) {
      talkBtn.setAttribute("aria-pressed", "true");
    }
    setState(auto ? "免提录音中… 说完停一下" : "录音中… 松开就发送", "recording");
    stopPlayback();
    // 趁说话这几秒先补一帧，松开时服务端就能拿到当前画面而不是旧帧
    if (window.JinyuScreen && window.JinyuScreen.scanNow) {
      window.JinyuScreen.scanNow();
    }

    ensureMic().then(function (mic) {
      if (!recording) {
        // 松开得比麦克风授权还快：别留下一条一直开着的麦克风
        dropMic(mic);
        return;
      }
      var mime = pickMime();
      try {
        recorder = mime ? new MediaRecorder(mic, { mimeType: mime }) : new MediaRecorder(mic);
      } catch (error) {
        throw new Error("无法开始录音：" + error.message);
      }
      chunks = [];
      recorder.ondataavailable = function (event) {
        if (event.data && event.data.size) {
          chunks.push(event.data);
        }
      };
      recorder.onstop = function () {
        finishRecord(mime || recorder.mimeType || "audio/webm");
      };
      recorder.start();
      stopTimer = window.setTimeout(stopRecord, MAX_RECORD_MS);
    }).catch(function (error) {
      recording = false;
      talkBtn.setAttribute("aria-pressed", "false");
      if (handsFree) {
        hfStop(null);
      }
      fail("麦克风不可用：" + (error && error.message ? error.message : error));
    });
  }

  function stopRecord() {
    if (stopTimer) {
      window.clearTimeout(stopTimer);
      stopTimer = null;
    }
    if (!recorder || recorder.state === "inactive") {
      recording = false;
      discardRecord = false;
      talkBtn.setAttribute("aria-pressed", "false");
      return;
    }
    recorder.stop();
  }

  function finishRecord(mime) {
    recording = false;
    talkBtn.setAttribute("aria-pressed", "false");
    if (stopTimer) {
      window.clearTimeout(stopTimer);
      stopTimer = null;
    }
    var kept = chunks;
    var blob = new Blob(kept, { type: mime.split(";")[0] || "audio/webm" });
    chunks = [];
    recorder = null;
    if (discardRecord) {
      discardRecord = false;
      return;
    }
    var seconds = Date.now() - startedAt;
    if (seconds < MIN_RECORD_MS || blob.size < 512) {
      setState(handsFree ? "太短了，再说一句" : "太短了：按住按钮多说一会儿", "idle");
      return;
    }
    hfCooldownUntil = performance.now() + HF_COOLDOWN_MS;
    blobToBase64(blob).then(function (base64) {
      sendTurn({ audio: base64, mime: mime.split(";")[0] }, "voice");
    }).catch(function (error) {
      fail("录音处理失败：" + error.message);
    });
  }

  function sendText(from) {
    var box = from || (phoneInput && phoneInput.value.trim() ? phoneInput : inputEl);
    var text = (box.value || "").trim().slice(0, MAX_TEXT);
    if (!text || busy) {
      return;
    }
    box.value = "";
    if (inputEl !== box) {
      inputEl.value = "";
    }
    if (phoneInput && phoneInput !== box) {
      phoneInput.value = "";
    }
    if (window.JinyuScreen && window.JinyuScreen.scanNow) {
      window.JinyuScreen.scanNow();
    }
    sendTurn({ text: text }, "text");
  }

  talkBtn.addEventListener("pointerdown", function (event) {
    event.preventDefault();
    try {
      talkBtn.setPointerCapture(event.pointerId);
    } catch (error) {}
    if (handsFree) {
      // 按住说话优先：先退掉免提，避免两套录音同时抢麦克风
      hfStop(null);
    }
    startRecord(false);
  });

  ["pointerup", "pointercancel", "lostpointercapture"].forEach(function (name) {
    talkBtn.addEventListener(name, function () {
      if (recording) {
        stopRecord();
      }
    });
  });

  talkBtn.addEventListener("contextmenu", function (event) {
    event.preventDefault();
  });

  // 键盘等价操作：聚焦按钮后按住空格/回车录音，松开即发送
  talkBtn.addEventListener("keydown", function (event) {
    if (event.key !== " " && event.key !== "Enter") {
      return;
    }
    event.preventDefault();
    if (event.repeat || recording) {
      return;
    }
    if (handsFree) {
      hfStop(null);
    }
    startRecord(false);
  });

  ["keyup", "blur"].forEach(function (name) {
    talkBtn.addEventListener(name, function (event) {
      if (name === "keyup" && event.key !== " " && event.key !== "Enter") {
        return;
      }
      if (recording) {
        stopRecord();
      }
    });
  });

  replayBtn.addEventListener("click", function () {
    if (!lastUrls.length) {
      return;
    }
    stopPlayback();
    queue = lastUrls.slice();
    pump();
  });

  if (handsBtn) {
    handsBtn.addEventListener("click", function () {
      if (handsFree) {
        hfStop("免提已关闭");
      } else {
        hfStart();
      }
    });
  }

  // 页面切到后台就关掉免提，避免在用户看不到的时候一直开着麦克风
  document.addEventListener("visibilitychange", function () {
    if (document.hidden && handsFree) {
      hfStop("页面切到后台，免提已关闭");
    }
  });

  sendBtn.addEventListener("click", function () {
    sendText(inputEl);
  });

  if (phoneForm && phoneInput) {
    // 手机键盘的回车就是「发送」，走 form 提交最稳
    phoneForm.addEventListener("submit", function (event) {
      event.preventDefault();
      sendText(phoneInput);
    });
  }

  inputEl.addEventListener("keydown", function (event) {
    if (event.key === "Enter") {
      event.preventDefault();
      sendText(inputEl);
    }
  });

  // 多端同步：每几秒拉一次另一端说了什么，显示在页面底部
  var peerEl = document.getElementById("peer-line");
  var feedAfter = 0;
  var feedPrimed = false;
  var peerTimer = 0;

  function showPeer(items) {
    if (!peerEl) {
      return;
    }
    var others = items.filter(function (item) {
      return item.client !== CLIENT;
    });
    if (!others.length) {
      return;
    }
    var last = others[others.length - 1];
    peerEl.textContent = last.who === "user"
      ? "另一边的主人说「" + last.text + "」"
      : "瑾瑜在另一边：" + last.text;
    peerEl.hidden = false;
    window.clearTimeout(peerTimer);
    peerTimer = window.setTimeout(function () {
      peerEl.hidden = true;
    }, 9000);
  }

  function pollFeed() {
    if (document.hidden) {
      return;
    }
    resolveApi().then(function (base) {
      if (!base) {
        return null;
      }
      return fetch(base + "/call/feed?after=" + feedAfter, {
        credentials: "same-origin",
        cache: "no-store"
      }).then(function (resp) {
        return resp.ok ? resp.json() : null;
      });
    }).then(function (data) {
      if (!data) {
        return;
      }
      feedAfter = data.next || feedAfter;
      if (feedPrimed) {
        showPeer(data.items || []);
      }
      feedPrimed = true;
    }).catch(function () {});
  }

  window.setInterval(pollFeed, 2500);

  window.addEventListener("beforeunload", function () {
    if (vadTimer) {
      window.clearInterval(vadTimer);
      vadTimer = null;
    }
    if (audioCtx) {
      try { audioCtx.close(); } catch (error) {}
      audioCtx = null;
    }
    if (stream) {
      stream.getTracks().forEach(function (track) { track.stop(); });
      stream = null;
    }
  });

  resolveApi().then(function (base) {
    if (!base) {
      setState("通话服务未启动：请运行 python call_server.py", "error");
      return;
    }
    if (recording || busy) {
      return;
    }
    if (!health || !health.tts) {
      setState("可以录音，但没装 edge-tts：只会回文字", "idle");
    } else if (!health.asr) {
      setState("没配 RP_ASR_KEY：先打字试，配好后按住说话", "idle");
    }
  });
})();
