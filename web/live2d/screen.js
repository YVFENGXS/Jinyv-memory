(function () {
  "use strict";

  var DEFAULT_API = "http://127.0.0.1:8790";
  var MAX_SIDE = 1280;
  var JPEG_QUALITY = 0.86;
  // 摄像头画面一直在轻微变化，间隔和差异阈值都比屏幕宽一点，省调用
  var SCAN = {
    screen: { interval: 4000, threshold: 5 },
    camera: { interval: 6000, threshold: 10 }
  };
  var MAX_QUESTION = 200;
  var LABEL = { screen: "屏幕", camera: "摄像头" };

  var screenBtn = document.getElementById("view-screen");
  var cameraBtn = document.getElementById("view-camera");
  var autoBtn = document.getElementById("screen-auto");
  var stateEl = document.getElementById("screen-state");
  var inputEl = document.getElementById("screen-question");
  var sendBtn = document.getElementById("screen-send");
  var readEl = document.getElementById("screen-read");
  var answerEl = document.getElementById("screen-answer");
  var previewEl = document.getElementById("view-preview");
  var videoEl = document.getElementById("view-video");

  if (!screenBtn || !cameraBtn || !stateEl) {
    return;
  }

  var stream = null;
  var MODE = "";
  var timer = null;
  var inFlight = false;
  var pendingScan = false;
  var capturing = false;
  var autoScan = true;
  var lastSig = null;
  var lastSentAt = 0;
  var apiBase = null;
  var apiTried = false;

  // 读屏、摄像头、语音用同一个会话键，这样通话时能拿到刚看的那一帧
  function sessionId() {
    try {
      var got = window.localStorage.getItem("rp.stage.session");
      if (got) {
        return got;
      }
      got = window.localStorage.getItem("rp.screen.session") ||
        window.localStorage.getItem("rp.voice.session");
      if (got) {
        window.localStorage.setItem("rp.stage.session", got);
        return got;
      }
      var made = "stage-" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
      window.localStorage.setItem("rp.stage.session", made);
      return made;
    } catch (error) {
      return "stage-local";
    }
  }

  var SESSION = sessionId();

  function setState(text, state) {
    stateEl.textContent = text;
    stateEl.dataset.state = state || "idle";
  }

  function clock() {
    var now = new Date();
    return ("0" + now.getHours()).slice(-2) + ":" + ("0" + now.getMinutes()).slice(-2) +
      ":" + ("0" + now.getSeconds()).slice(-2);
  }

  function setButtons() {
    window.RP_VIEW_MODE = MODE;
    screenBtn.setAttribute("aria-pressed", MODE === "screen" ? "true" : "false");
    cameraBtn.setAttribute("aria-pressed", MODE === "camera" ? "true" : "false");
    screenBtn.textContent = MODE === "screen" ? "停止屏幕" : "屏幕";
    cameraBtn.textContent = MODE === "camera" ? "关闭摄像头" : "摄像头";
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
      return fetch(base + "/health", { cache: "no-store" })
        .then(function (resp) {
          if (!resp.ok) {
            throw new Error("HTTP " + resp.status);
          }
          return resp.json();
        })
        .then(function (info) {
          apiBase = base;
          if (!info.vision) {
            setState("服务已启动，但没配视觉密钥（RP_GLM_KEY / RP_FRONT_KEY）", "error");
          }
          return base;
        })
        .catch(function () {
          return tryNext(at + 1);
        });
    }

    return tryNext(0).then(function (base) {
      if (!base) {
        setState("通话服务未启动：请运行 python call_server.py", "error");
      }
      return base;
    });
  }

  function postJson(base, path, body) {
    return fetch(base + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (resp) {
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

  function signatureOf(canvas) {
    var small = document.createElement("canvas");
    small.width = 32;
    small.height = 18;
    var ctx = small.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(canvas, 0, 0, 32, 18);
    var data = ctx.getImageData(0, 0, 32, 18).data;
    var out = [];
    for (var i = 0; i < data.length; i += 4) {
      out.push((data[i] * 0.299 + data[i + 1] * 0.587 + data[i + 2] * 0.114) / 8);
    }
    return out;
  }

  function diffOf(a, b) {
    if (!a || !b || a.length !== b.length) {
      return 999;
    }
    var total = 0;
    for (var i = 0; i < a.length; i += 1) {
      total += Math.abs(a[i] - b[i]);
    }
    return total / a.length;
  }

  function capture() {
    if (!videoEl || !videoEl.videoWidth || !videoEl.videoHeight) {
      return Promise.resolve(null);
    }
    var width = videoEl.videoWidth;
    var height = videoEl.videoHeight;
    var scale = Math.min(1, MAX_SIDE / Math.max(width, height));
    var canvas = document.createElement("canvas");
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    var ctx = canvas.getContext("2d");
    ctx.drawImage(videoEl, 0, 0, canvas.width, canvas.height);
    var sig = signatureOf(canvas);

    return new Promise(function (resolve) {
      canvas.toBlob(function (blob) {
        if (!blob) {
          resolve(null);
          return;
        }
        var reader = new FileReader();
        reader.onload = function () {
          var raw = String(reader.result || "");
          var base64 = raw.indexOf(",") >= 0 ? raw.split(",")[1] : raw;
          resolve({ image: base64, signature: sig, width: canvas.width, height: canvas.height });
        };
        reader.onerror = function () { resolve(null); };
        reader.readAsDataURL(blob);
      }, "image/jpeg", JPEG_QUALITY);
    });
  }

  function renderReading(reading) {
    readEl.hidden = false;
    readEl.dataset.error = "false";
    readEl.textContent = "";
    if (!reading) {
      return;
    }
    var lines = [];
    if (reading.mode === "camera") {
      if (reading.scene) {
        lines.push("环境：" + reading.scene);
      }
      if (reading.person) {
        lines.push("画面里的人：" + reading.person);
      }
      if (reading.action) {
        lines.push("动作 / 表情：" + reading.action);
      }
      if (reading.text) {
        lines.push("画面文字：" + String(reading.text).slice(0, 320));
      }
    } else {
      if (reading.app) {
        lines.push("窗口：" + reading.app);
      }
      if (reading.scene) {
        lines.push("画面：" + reading.scene);
      }
      if (reading.activity) {
        lines.push("在做什么：" + reading.activity);
      }
      if (reading.error) {
        lines.push("报错：" + reading.error);
      }
      if (reading.files && reading.files.length) {
        lines.push("文件：" + reading.files.join("；"));
      }
      if (reading.text) {
        lines.push("文字：" + String(reading.text).slice(0, 320));
      }
    }
    if (reading.sensitive) {
      lines.push("提醒：" + reading.sensitive);
    }
    readEl.textContent = lines.join("\n") ||
      (reading.mode === "camera" ? "这一帧没看清人。" : "这一帧没读到文字。");
  }

  function renderAnswer(answer, mode) {
    answerEl.hidden = false;
    var note = mode && mode !== "real" ? "\n（未接模型，这是本地判读结果）" : "";
    answerEl.textContent = String(answer || "").trim() + note;
  }

  function fail(message) {
    setState(message, "error");
    answerEl.hidden = false;
    answerEl.textContent = message;
  }

  function releaseFrame() {
    inFlight = false;
    if (pendingScan) {
      // 说话时补的一帧如果撞上正在跑的调用，等它结束再补，别把最新的画面丢掉
      pendingScan = false;
      scan(true);
    }
  }

  function sendFrame(shot) {
    if (inFlight || !shot) {
      return Promise.resolve();
    }
    inFlight = true;
    var mode = MODE;
    setState("正在看" + LABEL[mode] + "…", "loading");
    return resolveApi().then(function (base) {
      if (!base) {
        throw new Error("通话服务未启动：请运行 python call_server.py");
      }
      return postJson(base, "/vision/frame", {
        session: SESSION,
        mode: mode,
        image: shot.image
      });
    }).then(function (data) {
      renderReading(data.reading);
      setState("已打开" + LABEL[mode] + " · " + shot.width + "×" + shot.height +
        " · 最近看 " + clock(), "ready");
    }).catch(function (error) {
      fail("看画面失败：" + error.message);
    }).then(function () {
      releaseFrame();
    });
  }

  function scan(force) {
    if (!stream || !MODE) {
      return Promise.resolve(false);
    }
    if (!force && !autoScan) {
      return Promise.resolve(false);
    }
    if (inFlight || capturing) {
      if (force) {
        pendingScan = true;
      }
      return Promise.resolve(false);
    }
    capturing = true;
    return capture().then(function (shot) {
      capturing = false;
      if (!shot) {
        return false;
      }
      // 画面没变化也至少每分钟补一帧，状态里的「最近看」不会停在旧时间
      var heartbeat = Date.now() - lastSentAt >= 60000;
      if (!force && !heartbeat && lastSig &&
          diffOf(lastSig, shot.signature) < SCAN[MODE].threshold) {
        return false;
      }
      lastSig = shot.signature;
      lastSentAt = Date.now();
      return sendFrame(shot).then(function () {
        return true;
      });
    }).catch(function () {
      capturing = false;
      return false;
    });
  }

  function stopTimer() {
    if (timer) {
      window.clearInterval(timer);
      timer = null;
    }
  }

  function restartTimer() {
    stopTimer();
    if (!MODE) {
      return;
    }
    timer = window.setInterval(function () { scan(false); }, SCAN[MODE].interval);
  }

  function stopView(silent) {
    stopTimer();
    if (stream) {
      stream.getTracks().forEach(function (track) { track.stop(); });
      stream = null;
    }
    if (videoEl) {
      videoEl.srcObject = null;
    }
    if (previewEl) {
      previewEl.hidden = true;
      previewEl.dataset.mode = "";
    }
    MODE = "";
    lastSig = null;
    lastSentAt = 0;
    setButtons();
    if (!silent) {
      setState("未打开", "idle");
    }
  }

  function startView(mode) {
    if (MODE === mode) {
      stopView();
      return;
    }
    stopView(true);
    resolveApi();
    var request;
    if (mode === "camera") {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        fail("当前浏览器打不开摄像头（需要 localhost 或 HTTPS）");
        return;
      }
      request = navigator.mediaDevices.getUserMedia({
        video: {
          width: { ideal: 1280 },
          height: { ideal: 720 },
          frameRate: { ideal: 15, max: 24 }
        },
        audio: false
      });
    } else {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
        fail("当前浏览器不支持共享屏幕（需要 localhost 或 HTTPS）");
        return;
      }
      request = navigator.mediaDevices.getDisplayMedia({
        video: { frameRate: { ideal: 5, max: 10 } },
        audio: false
      });
    }

    request.then(function (got) {
      stream = got;
      MODE = mode;
      videoEl.srcObject = stream;
      previewEl.hidden = false;
      previewEl.dataset.mode = mode;
      var track = stream.getVideoTracks()[0];
      if (track) {
        track.addEventListener("ended", function () { stopView(); });
      }
      setButtons();
      setState("已打开" + LABEL[mode] + " · 等待画面", "loading");

      return new Promise(function (resolve) {
        if (videoEl.readyState >= 1) {
          resolve();
          return;
        }
        videoEl.onloadedmetadata = function () { resolve(); };
        videoEl.play().catch(function () {});
      });
    }).then(function () {
      return videoEl.play().catch(function () {});
    }).then(function () {
      lastSig = null;
      lastSentAt = 0;
      scan(true);
      restartTimer();
    }).catch(function (error) {
      stopView();
      if (error && error.name === "NotAllowedError") {
        setState("已取消" + LABEL[mode], "idle");
      } else if (error && error.name === "NotFoundError") {
        fail("没找到摄像头：检查设备是否插好，或换个浏览器试试");
      } else {
        fail("打开" + LABEL[mode] + "失败：" + (error && error.message ? error.message : error));
      }
    });
  }

  function ask() {
    var question = inputEl.value.trim();
    if (!question || inFlight) {
      return;
    }
    inputEl.value = "";
    inFlight = true;
    var mode = MODE || "screen";
    setState("正在看" + LABEL[mode] + "…", "loading");

    var ready = stream ? capture() : Promise.resolve(null);
    ready.then(function (shot) {
      if (shot) {
        lastSig = shot.signature;
        lastSentAt = Date.now();
      }
      return resolveApi().then(function (base) {
        if (!base) {
          throw new Error("通话服务未启动：请运行 python call_server.py");
        }
        var body = { session: SESSION, question: question, mode: mode };
        if (shot) {
          body.image = shot.image;
        }
        return postJson(base, "/vision/ask", body);
      });
    }).then(function (data) {
      renderReading(data.reading);
      renderAnswer(data.answer, data.mode);
      if (data.stage && window.JinyuStage) {
        try {
          window.JinyuStage.apply(data.stage);
        } catch (error) {
          console.warn("[screen] stage apply failed:", error);
        }
      }
      setState((MODE ? "已打开" + LABEL[MODE] : "未打开") + " · 已回答 " + clock(), "ready");
    }).catch(function (error) {
      fail("提问失败：" + error.message);
    }).then(function () {
      releaseFrame();
    });
  }

  screenBtn.addEventListener("click", function () {
    startView("screen");
  });

  cameraBtn.addEventListener("click", function () {
    startView("camera");
  });

  autoBtn.addEventListener("click", function () {
    autoScan = !autoScan;
    autoBtn.setAttribute("aria-pressed", autoScan ? "true" : "false");
    if (autoScan && stream) {
      scan(true);
    }
  });

  sendBtn.addEventListener("click", ask);

  inputEl.addEventListener("keydown", function (event) {
    if (event.key === "Enter") {
      event.preventDefault();
      ask();
    }
  });

  window.addEventListener("beforeunload", function () {
    if (stream) {
      stream.getTracks().forEach(function (track) { track.stop(); });
    }
  });

  // 切回这个页面 / 窗口获得焦点时立刻补一帧，避免后台被浏览器降频后画面停在旧状态
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) {
      scan(true);
    }
  });
  window.addEventListener("focus", function () { scan(true); });

  window.JinyuScreen = {
    scanNow: function () { scan(true); },
    mode: function () { return MODE; }
  };

  setButtons();
  resolveApi();
})();
