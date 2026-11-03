(function () {
  "use strict";

  var MODEL_URL = "models/jinyu/大肥鱼.model3.json";
  var MODEL_DIR = "models/jinyu/";
  var MOBILE_MAX_WIDTH = 860;
  var FADE_SECONDS = 0.35;

  var EMOTION_NAMES = [
    "脸红", "黑脸", "爱心眼", "圈圈眼", "屑屑眼",
    "流泪", "流冷汗", "＞＜", "兔牙", "前倾", "加载"
  ];

  var MOTION_LABELS = {
    "默认手切举手": "举手",
    "举手切默认手": "收手",
    "竖手指切出来动画": "竖手指",
    "竖手指切回默认": "收手指",
    "狗尾草切换出来": "狗尾草",
    "狗尾草切回默认手": "收狗尾草",
    "脱捕": "脱捕（13s）"
  };

  // AI 触发的动作只保持一小会儿，到点自动播对应的「收回」动作，避免举手/竖手指一直定格。
  // 手动点动作按钮不受影响；表情仍由 AI 的 [[收起:..]] / [[重置]] 控制。
  var AI_MOTION_HOLD_MS = 6000;
  var MOTION_RESTORE = {
    "举手": "收手",
    "竖手指": "收手指",
    "狗尾草": "收狗尾草"
  };

  var stageEl = document.getElementById("stage");
  var roomBtn = document.getElementById("room-toggle");
  var canvasEl = document.getElementById("live2d");
  var statusEl = document.getElementById("status");
  var statusTextEl = document.getElementById("status-text");
  var metaEl = document.getElementById("meta");
  var loaderEl = document.getElementById("loader");
  var panelEl = document.getElementById("panel");
  var panelOpenBtn = document.getElementById("panel-open");
  var panelCloseBtn = document.getElementById("panel-close");
  var emotionBox = document.getElementById("expr-emotion");
  var outfitBox = document.getElementById("expr-outfit");
  var motionBox = document.getElementById("motions");
  var resetBtn = document.getElementById("reset");
  var idleBtn = document.getElementById("idle");

  var app = null;
  var model = null;
  var activeMotion = null;
  var stageTimer = null;        // AI 动作的自动收回计时器
  var stageMotion = "";         // 计时器对应的 AI 动作名
  var heldMotion = "";          // 当前还举着/竖着的手势（会保持到收回）
  var stageReportTimer = null;  // 上报舞台状态的节流计时器

  // AI 自己控制的头部姿态：转头 / 抬头低头 / 歪头 / 点头
  var headTarget = { x: 0, y: 0, z: 0 };   // 目标值，-1..1；x 正=向右，y 正=抬头，z 正=向右歪
  var headNow = { x: 0, y: 0, z: 0 };      // 平滑后的实际值
  var headTimer = null;                    // 保持多久后自己回正
  var nodState = null;                     // { count, startedAt, duration }
  var gazeWeightNow = 1;                   // 鼠标视线的权重，有头姿时平滑降下来
  var expressionButtons = [];
  var motionButtons = [];
  var presets = [];            // 表情/换装的参数定义（从 exp3 读入）
  var layers = [];             // 当前叠加的层，按点击顺序合并
  var gazeTarget = { x: 0, y: 0 };
  var gazeValue = { x: 0, y: 0 };
  var paramIndexCache = {};
  var managedParams = {};
  var lastTick = 0;

  function setStatus(text, state) {
    statusTextEl.textContent = text;
    statusEl.dataset.state = state || "loading";
    // 顶部通话条跟着模型状态走：接通中 / 通话中 / 连接异常
    var callStatusEl = document.getElementById("call-status");
    if (callStatusEl) {
      callStatusEl.textContent =
        state === "error" ? "连接异常" : state === "loading" ? "接通中" : "通话中";
    }
  }

  function panelIsOpen() {
    return panelEl.dataset.open === "true";
  }

  function setPanelOpen(open) {
    panelEl.dataset.open = open ? "true" : "false";
    panelOpenBtn.hidden = open;
    if (stageEl) {
      // 面板打开时，右上角的「我」小窗让到面板左边
      stageEl.dataset.panel = open ? "true" : "false";
    }
    layout();
  }

  function layout() {
    if (!model || !app) {
      return;
    }

    var width = app.screen.width;
    var height = app.screen.height;
    var mobile = window.innerWidth <= MOBILE_MAX_WIDTH;
    // 顶部通话条、底部通话控制栏都浮在画面上：模型头顶别钻进顶栏，
    // 脚踩在画面底部（下半身被底部控制栏轻轻遮住，像真的在视频里）
    var topInset = 72;
    var rightInset = !mobile && panelIsOpen() ? 348 : 0;
    var bottomInset = mobile && panelIsOpen() ? Math.min(height * 0.52, 440) + 96 : 14;

    // 面板打开时再多留一点空隙，模型和面板不贴在一起
    var availableWidth = Math.max(width - rightInset - (rightInset ? 88 : 32), 120);
    var availableHeight = Math.max(height - topInset - bottomInset - 8, 120);

    var sourceWidth = model.internalModel.width;
    var sourceHeight = model.internalModel.height;
    var scale = Math.min(availableWidth / sourceWidth, availableHeight / sourceHeight);

    model.scale.set(scale);
    model.anchor.set(0.5, 1);
    model.position.set((width - rightInset) / 2, height - bottomInset);
  }

  function clamp(value, low, high) {
    return Math.min(high, Math.max(low, value));
  }

  // 这套模型没有 ParamAngleX，转头用的是自定义 AngleX/Y，指针位置直接换算成转角
  function followPointer() {
    window.addEventListener("pointermove", function (event) {
      gazeTarget.x = clamp((event.clientX / window.innerWidth) * 2 - 1, -1, 1);
      gazeTarget.y = clamp((event.clientY / window.innerHeight) * 2 - 1, -1, 1);
    }, { passive: true });
  }

  function paramIndex(core, id) {
    if (paramIndexCache[id] !== undefined) {
      return paramIndexCache[id];
    }
    var ids = core._parameterIds ||
      (core._model && core._model.parameters && core._model.parameters.ids) || [];
    for (var i = 0; i < ids.length; i += 1) {
      if (drawableName(ids[i]) === id) {
        paramIndexCache[id] = i;
        return i;
      }
    }
    paramIndexCache[id] = -1;
    return -1;
  }

  function paramLimit(core, id) {
    var index = paramIndex(core, id);
    if (index < 0) {
      return 0;
    }
    return Math.max(Math.abs(core.getParameterMaximumValue(index)),
      Math.abs(core.getParameterMinimumValue(index)));
  }

  function paramDefault(core, id) {
    var index = paramIndex(core, id);
    if (index < 0) {
      return 0;
    }
    return typeof core.getParameterDefaultValue === "function"
      ? core.getParameterDefaultValue(index)
      : 0;
  }

  // 表情/换装都是同一套开关参数（key*/Clothes*/Movement*/LeanInput），
  // 多选时逐层合并：不同参数叠加，同一参数后点的覆盖先点的。
  // 必须在 afterMotionUpdate 写（早于 physics），前倾这类要靠物理算的才动得起来。
  function applyLayers() {
    if (!model) {
      return;
    }
    var core = model.internalModel.coreModel;
    var now = performance.now();
    var dt = lastTick ? Math.min(0.08, Math.max(0, (now - lastTick) / 1000)) : 0.016;
    lastTick = now;

    layers.forEach(function (layer) {
      var step = dt / FADE_SECONDS;
      layer.weight = clamp(layer.weight + (layer.closing ? -step : step), 0, 1);
    });
    layers = layers.filter(function (layer) {
      return !(layer.closing && layer.weight <= 0);
    });

    var ease = 1 - Math.exp(-dt * 7);
    gazeValue.x += (gazeTarget.x - gazeValue.x) * ease;
    gazeValue.y += (gazeTarget.y - gazeValue.y) * ease;

    var merged = {};
    layers.forEach(function (layer) {
      layer.params.forEach(function (param) {
        var base = paramDefault(core, param.id);
        var current = merged[param.id];
        if (current === undefined) {
          current = base;
        }
        var target = base + param.value;
        merged[param.id] = current * (1 - layer.weight) + target * layer.weight;
      });
    });

    // 已经没有层控制的参数复位到默认值，避免前倾这类状态卡住
    Object.keys(managedParams).forEach(function (id) {
      if (merged[id] === undefined) {
        var index = paramIndex(core, id);
        if (index >= 0) {
          core.setParameterValueByIndex(index, paramDefault(core, id));
        }
        delete managedParams[id];
      }
    });
    Object.keys(merged).forEach(function (id) {
      var index = paramIndex(core, id);
      if (index >= 0) {
        core.setParameterValueByIndex(index, merged[id]);
        managedParams[id] = true;
      }
    });

    // 有头姿时把鼠标视线让位，否则鼠标一动就把头拉回中间
    var nodOffset = nodWave(now);
    var headActive = nodOffset !== 0 || !!headTimer ||
      Math.abs(headTarget.x) + Math.abs(headTarget.y) + Math.abs(headTarget.z) > 0.01;
    // 权重也做平滑，回正那一刻头才不会突然被鼠标拽一下
    gazeWeightNow += ((headActive ? 0.18 : 1) - gazeWeightNow) * ease;

    var headEase = 1 - Math.exp(-dt * 5);
    headNow.x += (headTarget.x - headNow.x) * headEase;
    headNow.y += (headTarget.y - headNow.y) * headEase;
    headNow.z += (headTarget.z - headNow.z) * headEase;

    setAngle(core, "AngleX", clamp(gazeValue.x * gazeWeightNow + headNow.x, -1, 1), 0.85);
    // 垂直方向这套模型是反的：正值低头、负值抬头，所以取负号，鼠标在上才抬头
    setAngle(core, "AngleY",
      -clamp(gazeValue.y * gazeWeightNow + headNow.y + nodOffset, -1, 1), 0.45);
    // 歪头（Z 轴倾斜）；模型没有这个参数就自动跳过
    setAngle(core, "AngleZ", clamp(headNow.z, -1, 1), 0.5);
  }

  // 点头：一段短促的上下摆动，只往下点，不改变 AI 设定的其他头姿
  function nodWave(now) {
    if (!nodState) {
      return 0;
    }
    var elapsed = (now - nodState.startedAt) / 1000;
    if (elapsed >= nodState.duration) {
      nodState = null;
      return 0;
    }
    var wave = Math.sin(Math.PI * 2 * nodState.count * elapsed / nodState.duration);
    return -Math.abs(wave) * 0.5;
  }

  function clearHeadTimer() {
    if (headTimer) {
      window.clearTimeout(headTimer);
      headTimer = null;
    }
  }

  function releaseHead() {
    headTarget.x = 0;
    headTarget.y = 0;
    headTarget.z = 0;
    nodState = null;
    clearHeadTimer();
  }

  // 执行服务端下发的一组头姿：{turn, tilt, pitch, nod, hold}
  function applyHead(spec) {
    if (!spec) {
      return;
    }
    clearHeadTimer();
    if (spec.turn) {
      headTarget.x = spec.turn === "left" ? -0.75 : spec.turn === "right" ? 0.75 : 0;
    }
    if (spec.tilt) {
      headTarget.z = spec.tilt === "left" ? -0.55 : spec.tilt === "right" ? 0.55 : 0;
    }
    if (spec.pitch) {
      headTarget.y = spec.pitch === "up" ? 0.45 : spec.pitch === "down" ? -0.5 : 0;
    }
    var count = Math.min(5, Math.max(0, Math.round(Number(spec.nod) || 0)));
    if (count > 0) {
      nodState = {
        count: count,
        startedAt: performance.now(),
        duration: count * 0.55 + 0.25
      };
    }
    var hold = Number(spec.hold);
    if (!window.isFinite(hold) || hold <= 0) {
      var moved = Math.abs(headTarget.x) + Math.abs(headTarget.y) + Math.abs(headTarget.z);
      hold = moved > 0.01 ? 8000 : 0;   // 默认保持 8 秒，然后自然回正
    }
    if (hold > 0) {
      headTimer = window.setTimeout(function () {
        headTimer = null;
        releaseHead();
        reportStage();
      }, hold);
    }
    reportStage();
  }


  function setAngle(core, id, direction, ratio) {
    var index = paramIndex(core, id);
    if (index < 0) {
      return;
    }
    var limit = paramLimit(core, id);
    core.setParameterValueByIndex(index, direction * limit * ratio);
  }

  function installParameterLayer(target) {
    // afterMotionUpdate 在动作之后、physics 之前触发：
    // 参数既能盖住动作，又能喂给物理（前倾就是靠物理驱动的）
    target.internalModel.on("afterMotionUpdate", applyLayers);
    lastTick = performance.now();
  }

  function loadPresets(definitions) {
    var loads = definitions.map(function (definition) {
      var url = MODEL_DIR + definition.File;
      return fetch(url)
        .then(function (resp) {
          if (!resp.ok) {
            throw new Error("HTTP " + resp.status);
          }
          return resp.json();
        })
        .then(function (data) {
          var params = (data && data.Parameters) || [];
          return {
            name: definition.Name,
            params: params.map(function (item) {
              return { id: item.Id, value: Number(item.Value) };
            })
          };
        })
        .catch(function (error) {
          console.warn("[live2d] expression load failed:", definition.Name, error);
          return null;
        });
    });
    return Promise.all(loads).then(function (list) {
      presets = list.filter(Boolean);
      return presets;
    });
  }

  function layerActive(name) {
    return layers.some(function (layer) {
      return layer.name === name && !layer.closing;
    });
  }

  function togglePreset(name) {
    var existing = layers.filter(function (layer) {
      return layer.name === name;
    })[0];
    if (existing) {
      existing.closing = !existing.closing;
      syncExpressionButtons();
      reportStage();
      return;
    }
    var preset = presets.filter(function (item) {
      return item.name === name;
    })[0];
    if (!preset) {
      console.warn("[live2d] expression not loaded yet:", name);
      return;
    }
    layers.push({ name: name, params: preset.params, weight: 0, closing: false });
    syncExpressionButtons();
    reportStage();
  }

  // 模型自带的鼻梁黑色圆角图标（ArtMesh246/682），不是五官，加载后隐藏
  var NOSE_BADGE_DRAWABLES = ["ArtMesh246", "ArtMesh682"];

  function drawableName(id) {
    if (!id) {
      return "";
    }
    try {
      if (typeof id.getString === "function") {
        var text = id.getString();
        if (typeof text === "string") {
          return text;
        }
        if (text && typeof text.s === "string") {
          return text.s;
        }
      }
    } catch (error) {}
    try {
      if (typeof id.s === "string") {
        return id.s;
      }
    } catch (error) {}
    return String(id);
  }

  function hideNoseBadge(target) {
    var core = target && target.internalModel && target.internalModel.coreModel;
    if (!core || typeof core.getDrawableOpacity !== "function") {
      return [];
    }
    var hidden = [];
    var count = core.getDrawableCount();
    for (var i = 0; i < count; i += 1) {
      if (NOSE_BADGE_DRAWABLES.indexOf(drawableName(core.getDrawableId(i))) >= 0) {
        hidden.push(i);
      }
    }
    if (!hidden.length) {
      return [];
    }
    var original = core.getDrawableOpacity.bind(core);
    core.getDrawableOpacity = function (index) {
      return hidden.indexOf(index) >= 0 ? 0 : original(index);
    };
    return hidden;
  }

  function labelForMotion(file) {
    var base = String(file || "").split("/").pop().replace(/\.motion3\.json$/, "");
    return MOTION_LABELS[base] || base;
  }

  function makeChip(label, onClick) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "chip";
    button.textContent = label;
    button.setAttribute("aria-pressed", "false");
    button.addEventListener("click", onClick);
    return button;
  }

  function syncExpressionButtons() {
    expressionButtons.forEach(function (entry) {
      entry.button.setAttribute("aria-pressed", layerActive(entry.name) ? "true" : "false");
    });
  }

  function syncMotionButtons() {
    motionButtons.forEach(function (entry) {
      var key = entry.group + ":" + entry.index;
      entry.button.setAttribute("aria-pressed", key === activeMotion ? "true" : "false");
    });
  }

  function resetExpression() {
    layers = [];
    releaseHead();
    syncExpressionButtons();
    reportStage();
  }

  function backToIdle() {
    var restore = MOTION_RESTORE[stageMotion];
    clearStageTimer();
    if (!model) {
      return;
    }
    var manager = model.internalModel.motionManager;
    if (!manager) {
      return;
    }
    resetExpression();
    if (typeof manager.stopAllMotions === "function") {
      manager.stopAllMotions();
    }
    activeMotion = null;
    heldMotion = "";
    syncMotionButtons();
    manager.startRandomMotion("Idle", 1);
    if (restore) {
      playMotionByName(restore);   // 先把举手/竖手指收回去，再让 Idle 接管
    }
    reportStage();
  }

  function findLayer(name) {
    return layers.filter(function (layer) {
      return layer.name === name;
    })[0];
  }

  function showPreset(name) {
    var existing = findLayer(name);
    if (existing) {
      existing.closing = false;
      reportStage();
      return;
    }
    var preset = presets.filter(function (item) {
      return item.name === name;
    })[0];
    if (!preset) {
      console.warn("[live2d] stage expression not loaded:", name);
      return;
    }
    layers.push({ name: name, params: preset.params, weight: 0, closing: false });
    reportStage();
  }

  function hidePreset(name) {
    var existing = findLayer(name);
    if (existing) {
      existing.closing = true;
      reportStage();
    }
  }

  function playMotionByName(label) {
    var manager = model.internalModel.motionManager;
    var definitions = (manager && manager.definitions) || {};
    var groups = Object.keys(definitions).filter(function (group) {
      return group !== "Idle";
    });
    for (var g = 0; g < groups.length; g += 1) {
      var list = definitions[groups[g]] || [];
      for (var i = 0; i < list.length; i += 1) {
        if (labelForMotion(list[i].File) === label) {
          model.motion(groups[g], i, 2);
          activeMotion = groups[g] + ":" + i;
          heldMotion = MOTION_RESTORE[label] ? label : "";
          syncMotionButtons();
          reportStage();
          return true;
        }
      }
    }
    console.warn("[live2d] stage motion not found:", label);
    return false;
  }

  function clearStageTimer() {
    if (stageTimer) {
      window.clearTimeout(stageTimer);
      stageTimer = null;
    }
    stageMotion = "";
  }

  function scheduleMotionRestore(label) {
    clearStageTimer();
    var back = MOTION_RESTORE[label];
    if (!back || AI_MOTION_HOLD_MS <= 0) {
      return;                       // 脱捕这类自己会结束的动作不用收
    }
    stageMotion = label;
    stageTimer = window.setTimeout(function () {
      stageTimer = null;
      if (stageMotion !== label) {
        return;                     // 期间被别的动作/待机顶掉了
      }
      stageMotion = "";
      playMotionByName(back);
    }, AI_MOTION_HOLD_MS);
  }

  // 服务器 AI 在回答里带 stage，前端按它驱动模型；面板手动操作照旧有效
  function applyStage(stage) {
    if (!stage || !model) {
      return;
    }
    if (stage.reset || stage.idle) {
      clearStageTimer();
    }
    if (stage.reset) {
      resetExpression();
    }
    (stage.off || []).forEach(hidePreset);
    (stage.expr || []).forEach(showPreset);
    if (stage.idle) {
      backToIdle();
      return;
    }
    if (stage.motion) {
      if (playMotionByName(stage.motion)) {
        scheduleMotionRestore(stage.motion);
      }
    }
    if (stage.head) {
      applyHead(stage.head);
    }
    syncExpressionButtons();
    reportStage();
  }

  // ---- 让模型知道自己现在的样子：把实际表情/动作上报给通话服务 ----
  // 和 voice.js / screen.js 同一个固定会话键，表情上报才能落到同一场通话里
  function stageSessionId() {
    return "stage-shared";
  }

  function motionLabelOf(key) {
    if (!key || !model) {
      return "";
    }
    var parts = String(key).split(":");
    var definitions = (model.internalModel.motionManager || {}).definitions || {};
    var list = definitions[parts[0]] || [];
    var item = list[Number(parts[1])];
    return item ? labelForMotion(item.File) : "";
  }

  // 当前头姿的一句话描述，随舞台状态一起上报，让模型知道自己头朝哪
  function headText() {
    var parts = [];
    if (headTarget.x > 0.15) {
      parts.push("头向右转");
    } else if (headTarget.x < -0.15) {
      parts.push("头向左转");
    }
    if (headTarget.y > 0.15) {
      parts.push("头微微抬起");
    } else if (headTarget.y < -0.15) {
      parts.push("头低着");
    }
    if (headTarget.z > 0.15) {
      parts.push("头向右歪");
    } else if (headTarget.z < -0.15) {
      parts.push("头向左歪");
    }
    if (nodState) {
      parts.push("正在点头");
    }
    return parts.join("、") || "头正对着你";
  }

  function currentStageState() {
    var expressions = layers.filter(function (layer) {
      return !layer.closing;
    }).map(function (layer) {
      return layer.name;
    });
    var motion = "";
    if (heldMotion) {
      motion = heldMotion + "（保持中）";
    } else if (stageMotion) {
      motion = stageMotion;
    } else if (activeMotion) {
      motion = motionLabelOf(activeMotion);
    }
    return {
      session: stageSessionId(),
      expressions: expressions,
      motion: motion,
      head: headText()
    };
  }

  function reportStage() {
    if (stageReportTimer) {
      return;
    }
    stageReportTimer = window.setTimeout(function () {
      stageReportTimer = null;
      var base = window.RP_API_BASE ||
        (window.location.origin && window.location.origin !== "null"
          ? window.location.origin : "http://127.0.0.1:8790");
      try {
        fetch(base + "/stage/state", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(currentStageState())
        }).catch(function () {});
      } catch (error) {}
    }, 150);
  }

  window.JinyuStage = {
    apply: applyStage,
    reset: resetExpression,
    idle: backToIdle
  };

  function bindExpressionButton(button, name) {
    button.addEventListener("click", function () {
      if (!model) {
        return;
      }
      togglePreset(name);
    });
  }

  function buildUi() {
    var settings = model.internalModel.settings;
    var expressions =
      settings.expressions ||
      (settings.fileReferences && settings.fileReferences.Expressions) ||
      [];
    var manager = model.internalModel.motionManager;
    var definitions = (manager && manager.definitions) || {};

    expressionButtons = [];
    expressions.forEach(function (definition) {
      var name = typeof definition === "string" ? definition : definition.Name;
      var isEmotion = EMOTION_NAMES.indexOf(name) !== -1;
      var button = makeChip(name, function () {});
      bindExpressionButton(button, name);
      expressionButtons.push({ name: name, button: button });
      (isEmotion ? emotionBox : outfitBox).appendChild(button);
    });

    motionButtons = [];
    Object.keys(definitions).forEach(function (group) {
      if (group === "Idle") {
        return;
      }
      definitions[group].forEach(function (definition, index) {
        var label = labelForMotion(definition.File);
        var button = makeChip(label, function () {
          if (!model) {
            return;
          }
          // 动作不重置表情：举手时也能保持脸红/爱心眼
          clearStageTimer();          // 手动操作优先，撤掉 AI 的自动收回计时
          model.motion(group, index, 2);
          activeMotion = group + ":" + index;
          heldMotion = MOTION_RESTORE[label] ? label : "";
          syncMotionButtons();
          reportStage();
        });
        motionButtons.push({ group: group, index: index, button: button });
        motionBox.appendChild(button);
      });
    });

    if (manager && typeof manager.on === "function") {
      manager.on("motionStart", function (group, index) {
        activeMotion = group + ":" + index;
        syncMotionButtons();
        reportStage();
      });
      manager.on("motionFinish", function () {
        activeMotion = null;
        syncMotionButtons();
        reportStage();
      });
    }

    loadPresets(expressions);

    var blink = (settings.groups || []).some(function (group) {
      return group.Name === "EyeBlink" && group.Ids && group.Ids.length;
    });
    var lipSync = (settings.groups || []).some(function (group) {
      return group.Name === "LipSync" && group.Ids && group.Ids.length;
    });
    var motionCount = motionButtons.length;

    metaEl.textContent =
      expressions.length + " 个表情 · " + motionCount + " 个动作" +
      (blink ? " · 眨眼" : "") + (lipSync ? " · LipSync" : "");
    reportStage();
  }

  function hideLoader() {
    window.requestAnimationFrame(function () {
      window.setTimeout(function () {
        loaderEl.dataset.hidden = "true";
      }, 450);
    });
  }

  function boot() {
    app = new PIXI.Application({
      view: canvasEl,
      resizeTo: window,
      backgroundAlpha: 0,
      antialias: true,
      autoDensity: true,
      resolution: Math.min(window.devicePixelRatio || 1, 2)
    });

    app.renderer.on("resize", layout);
    window.addEventListener("resize", layout);

    setPanelOpen(window.innerWidth > MOBILE_MAX_WIDTH);

    PIXI.live2d.Live2DModel.from(MODEL_URL, {
      autoInteract: false,
      autoUpdate: true
    })
      .then(function (loadedModel) {
        model = loadedModel;
        app.stage.addChild(model);
        var hiddenBadge = hideNoseBadge(model);
        layout();
        installParameterLayer(model);
        buildUi();
        followPointer();

        var manager = model.internalModel.motionManager;
        manager.startRandomMotion("Idle", 1);

        var settings = model.internalModel.settings;
        var expressions =
          settings.expressions ||
          (settings.fileReferences && settings.fileReferences.Expressions) ||
          [];
        setStatus("已就绪 · 移动鼠标，她会看向你", "ready");
        console.info("[live2d] model ready", {
          size: model.internalModel.width + "x" + model.internalModel.height,
          expressions: expressions.length,
          motions: Object.keys(manager.definitions || {}),
          groups: settings.groups,
          noseBadgeHidden: hiddenBadge.length
        });
        hideLoader();
      })
      .catch(function (error) {
        console.error("[live2d] load failed", error);
        setStatus("加载失败：" + (error && error.message ? error.message : error), "error");
        metaEl.textContent = "模型未加载";
        loaderEl.dataset.hidden = "true";
      });
  }

  resetBtn.addEventListener("click", function () {
    if (model) {
      resetExpression();
    }
  });

  idleBtn.addEventListener("click", function () {
    if (model) {
      backToIdle();
    }
  });

  panelCloseBtn.addEventListener("click", function () {
    setPanelOpen(false);
  });

  panelOpenBtn.addEventListener("click", function () {
    setPanelOpen(true);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && panelIsOpen()) {
      setPanelOpen(false);
    }
  });

  // ---- 视频通话外观：白天 / 夜晚背景 + 通话计时 ----
  var ROOM_KEY = "rp.stage.room";
  var callStartedAt = Date.now();

  function readSavedRoom() {
    try {
      var got = window.localStorage.getItem(ROOM_KEY);
      return got === "day" || got === "night" ? got : "";
    } catch (error) {
      return "";
    }
  }

  function applyRoom(room) {
    if (!stageEl || !roomBtn) {
      return;
    }
    stageEl.dataset.room = room;
    roomBtn.textContent = room === "day" ? "白天" : "夜晚";
    roomBtn.setAttribute("aria-pressed", room === "night" ? "true" : "false");
  }

  function autoRoom() {
    var hour = new Date().getHours();
    return hour >= 6 && hour < 18 ? "day" : "night";   // 6:00-18:00 白天，其余夜晚
  }

  function formatElapsed(ms) {
    var total = Math.max(0, Math.floor(ms / 1000));
    var h = Math.floor(total / 3600);
    var m = Math.floor((total % 3600) / 60);
    var s = total % 60;
    var pad = function (n) { return ("0" + n).slice(-2); };
    return h > 0 ? h + ":" + pad(m) + ":" + pad(s) : pad(m) + ":" + pad(s);
  }

  applyRoom(readSavedRoom() || autoRoom());
  if (roomBtn) {
    roomBtn.addEventListener("click", function () {
      var next = stageEl.dataset.room === "day" ? "night" : "day";
      applyRoom(next);
      try {
        window.localStorage.setItem(ROOM_KEY, next);
      } catch (error) {}
    });
  }
  // 没手动选过就一直跟着本地时间走
  window.setInterval(function () {
    if (!readSavedRoom()) {
      applyRoom(autoRoom());
    }
  }, 600000);

  function tickCallTimer() {
    var el = document.getElementById("call-timer");
    if (el) {
      el.textContent = formatElapsed(Date.now() - callStartedAt);
    }
  }
  tickCallTimer();
  window.setInterval(tickCallTimer, 1000);

  boot();
})();
