(function () {
  "use strict";

  const DATA = window.EGO_TOOL_DATA;
  if (!DATA || !Array.isArray(DATA.questions) || !Array.isArray(DATA.characters)) {
    document.body.textContent = "小工具数据加载失败，请重新安装数据包。";
    return;
  }

  const STORAGE_KEY = "sakura_ego_match_v1";
  const dimensions = DATA.dimensions;
  const characterById = Object.fromEntries(DATA.characters.map(function (character) {
    return [character.id, character];
  }));
  const questionById = Object.fromEntries(DATA.questions.map(function (question) {
    return [question.id, question];
  }));
  const dimensionDescriptions = {
    "灵活": "协调、适应与转向",
    "锻造": "自律、训练与打磨",
    "突破": "挑战、破局与越界",
    "预判": "解析、规划与预演",
    "掌控": "组织、支配与定调",
    "愉悦": "兴奋、直觉与快感",
    "定力": "秩序、冷静与稳定",
    "坚韧": "承受、恢复与坚持"
  };

  let answers = new Array(DATA.questions.length).fill(null);
  let questionIndex = 0;
  let currentRanking = [];
  let selectedCharacterId = null;
  let galleryReturnView = "home-view";
  let detailReturnView = "gallery-view";
  let toastTimer = null;
  let autoAdvanceTimer = null;
  let reviewMode = false;

  const views = Array.from(document.querySelectorAll(".view"));
  const homeView = document.getElementById("home-view");
  const testView = document.getElementById("test-view");
  const resultView = document.getElementById("result-view");
  const galleryView = document.getElementById("gallery-view");
  const detailView = document.getElementById("detail-view");
  const continueButton = document.getElementById("continue-button");
  const optionList = document.getElementById("option-list");
  const questionCard = document.getElementById("question-card");
  const testHint = document.getElementById("test-hint");
  const toast = document.getElementById("toast");
  const searchInput = document.getElementById("character-search");

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function setViewportHeight() {
    const height = window.visualViewport ? window.visualViewport.height : window.innerHeight;
    document.documentElement.style.setProperty("--viewport-height", Math.round(height) + "px");
  }

  function setView(target) {
    if (target !== testView) clearAutoAdvance();
    views.forEach(function (view) {
      view.classList.toggle("is-active", view === target);
    });
    window.scrollTo(0, 0);
  }

  function clearAutoAdvance() {
    if (autoAdvanceTimer === null) return;
    window.clearTimeout(autoAdvanceTimer);
    autoAdvanceTimer = null;
  }

  function showToast(message) {
    window.clearTimeout(toastTimer);
    toast.textContent = message;
    toast.classList.add("is-visible");
    toastTimer = window.setTimeout(function () {
      toast.classList.remove("is-visible");
    }, 1800);
  }

  function saveProgress() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        version: DATA.questionnaireVersion,
        answers: answers,
        questionIndex: questionIndex,
        updatedAt: Date.now()
      }));
    } catch (error) {
      showToast("本机存储不可用，本次进度只在当前页面保留");
    }
    updateHomeProgress();
  }

  function readProgress() {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
      if (!saved || saved.version !== DATA.questionnaireVersion || !Array.isArray(saved.answers)) return;
      answers = new Array(DATA.questions.length).fill(null).map(function (_, index) {
        const value = saved.answers[index];
        return /^[A-E]$/.test(value || "") ? value : null;
      });
      questionIndex = Math.max(0, Math.min(DATA.questions.length - 1, Number(saved.questionIndex) || 0));
    } catch (error) {
      answers = new Array(DATA.questions.length).fill(null);
      questionIndex = 0;
    }
  }

  function updateHomeProgress() {
    const count = answers.filter(Boolean).length;
    continueButton.classList.toggle("is-hidden", count === 0);
    continueButton.textContent = count === DATA.questions.length
      ? "查看上次测评结果"
      : "继续上次进度 · " + count + "/" + DATA.questions.length;
  }

  function beginNewTest() {
    if (answers.some(Boolean) && !window.confirm("开始新测评会清空当前保存的答案，确定继续吗？")) return;
    answers = new Array(DATA.questions.length).fill(null);
    questionIndex = 0;
    reviewMode = false;
    saveProgress();
    renderQuestion();
    setView(testView);
  }

  function continueTest() {
    if (answers.every(Boolean)) {
      renderResult();
      return;
    }
    const firstEmpty = answers.findIndex(function (answer) { return !answer; });
    questionIndex = firstEmpty >= 0 ? firstEmpty : questionIndex;
    reviewMode = false;
    renderQuestion();
    setView(testView);
  }

  function optionMarkup(question, selected) {
    return question.options.map(function (option) {
      const classes = ["option-button"];
      if (selected === option.key) classes.push("is-selected");
      if (option.isNone) classes.push("is-none");
      return "<button type=\"button\" class=\"" + classes.join(" ") + "\" data-option=\"" + option.key
        + "\" data-key=\"" + option.key + "\" role=\"radio\" aria-checked=\""
        + String(selected === option.key) + "\"><span>" + escapeHtml(option.text) + "</span></button>";
    }).join("");
  }

  function renderQuestion() {
    clearAutoAdvance();
    const question = DATA.questions[questionIndex];
    document.getElementById("progress-module").textContent = question.moduleLabel;
    document.getElementById("progress-count").textContent = (questionIndex + 1) + " / " + DATA.questions.length;
    document.getElementById("progress-bar").style.width = ((questionIndex + 1) / DATA.questions.length * 100).toFixed(2) + "%";
    document.getElementById("question-kicker").textContent = question.id + " · " + question.shortTitle;
    document.getElementById("question-title").textContent = question.text;
    optionList.innerHTML = optionMarkup(question, answers[questionIndex]);
    document.getElementById("next-button").innerHTML = questionIndex === DATA.questions.length - 1
      ? "生成结果 <span>→</span>"
      : "下一题 <span>→</span>";
    const previous = document.querySelector('[data-action="previous-question"]');
    previous.disabled = questionIndex === 0;
    previous.style.opacity = questionIndex === 0 ? ".45" : "1";
    testHint.classList.remove("is-warning");
    testHint.textContent = reviewMode
      ? "答案检查模式：选择后自动进入下一题。"
      : "选择后自动进入下一题，进度会保存在本机。";
  }

  function selectOption(key) {
    clearAutoAdvance();
    const selectedQuestionIndex = questionIndex;
    answers[questionIndex] = key;
    saveProgress();
    optionList.querySelectorAll(".option-button").forEach(function (button) {
      const active = button.getAttribute("data-option") === key;
      button.classList.toggle("is-selected", active);
      button.setAttribute("aria-checked", String(active));
    });
    testHint.classList.remove("is-warning");
    testHint.textContent = key === "E"
      ? "已选择“以上皆非”，即将进入下一题。"
      : "已记录，即将进入下一题。";
    autoAdvanceTimer = window.setTimeout(function () {
      autoAdvanceTimer = null;
      if (questionIndex !== selectedQuestionIndex || !testView.classList.contains("is-active")) return;
      moveQuestion(1);
    }, 240);
  }

  function moveQuestion(delta) {
    clearAutoAdvance();
    if (delta > 0 && !answers[questionIndex]) {
      testHint.textContent = "请先选择一个最接近你的答案。";
      testHint.classList.add("is-warning");
      questionCard.classList.remove("is-nudged");
      window.setTimeout(function () { questionCard.classList.add("is-nudged"); }, 10);
      return;
    }
    if (delta > 0 && questionIndex === DATA.questions.length - 1) {
      if (answers.every(Boolean)) renderResult();
      return;
    }
    questionIndex = Math.max(0, Math.min(DATA.questions.length - 1, questionIndex + delta));
    saveProgress();
    renderQuestion();
    window.scrollTo(0, 0);
  }

  function calculateScores() {
    const scores = Object.fromEntries(dimensions.map(function (dimension) { return [dimension, 0]; }));
    let noneCount = 0;
    DATA.questions.forEach(function (question, index) {
      const choice = answers[index];
      const option = question.options.find(function (item) { return item.key === choice; });
      if (!option) return;
      if (option.isNone) noneCount += 1;
      Object.keys(option.scores).forEach(function (dimension) {
        scores[dimension] += option.scores[dimension];
      });
    });
    return { scores: scores, noneCount: noneCount };
  }

  function standardizedDistance(left, right) {
    const sum = dimensions.reduce(function (total, dimension) {
      const deviation = DATA.stats[dimension].sd || 1;
      const delta = (left[dimension] - right[dimension]) / deviation;
      return total + delta * delta;
    }, 0);
    return Math.sqrt(sum / dimensions.length);
  }

  function similarityFromDistance(distance) {
    return Math.max(1, Math.min(100, Math.round(100 / (1 + 0.85 * distance))));
  }

  function rankCharacters(userScores) {
    return DATA.characters.map(function (character) {
      const distance = standardizedDistance(userScores, character.dimensions);
      return { character: character, distance: distance, similarity: similarityFromDistance(distance) };
    }).sort(function (left, right) {
      if (left.distance !== right.distance) return left.distance - right.distance;
      return left.character.nameZh.localeCompare(right.character.nameZh, "zh-CN");
    });
  }

  function characterColor(id) {
    let hash = 0;
    for (let index = 0; index < id.length; index += 1) hash = ((hash << 5) - hash) + id.charCodeAt(index);
    const hue = 198 + (Math.abs(hash) % 38);
    return "linear-gradient(145deg, hsl(" + hue + " 78% 61%), hsl(" + (hue + 18) + " 62% 31%))";
  }

  function characterInitial(name) {
    return Array.from(String(name).replace(/[·•・\s]/g, "")).slice(0, 2).join("");
  }

  function setCharacterMark(element, character) {
    element.textContent = characterInitial(character.nameZh);
    element.style.background = characterColor(character.id);
  }

  function typeBadges(character) {
    return [character.mbti, character.enneagram, "Tritype " + character.tritype].map(function (label) {
      return "<span>" + escapeHtml(label) + "</span>";
    }).join("");
  }

  function topDimensionSummary(userScores, character) {
    const strongest = dimensions.slice().sort(function (left, right) {
      const leftZ = (userScores[left] - DATA.stats[left].mean) / DATA.stats[left].sd;
      const rightZ = (userScores[right] - DATA.stats[right].mean) / DATA.stats[right].sd;
      return rightZ - leftZ;
    }).slice(0, 2);
    const closest = dimensions.slice().sort(function (left, right) {
      const leftGap = Math.abs(userScores[left] - character.dimensions[left]) / DATA.stats[left].sd;
      const rightGap = Math.abs(userScores[right] - character.dimensions[right]) / DATA.stats[right].sd;
      return leftGap - rightGap;
    }).slice(0, 2);
    return "你的突出轴是「" + strongest.join("」与「") + "」，而你和 " + character.nameZh
      + " 最同步的部分落在「" + closest.join("」与「") + "」。这说明你们未必每个选择都相同，但行动结构相当接近。";
  }

  function renderHiddenEnding(noneCount) {
    const holder = document.getElementById("hidden-ending");
    if (noneCount < DATA.hiddenEndingThreshold) {
      holder.innerHTML = "";
      return;
    }
    holder.innerHTML = "<article class=\"hidden-ending-card\"><span>HIDDEN ENDING · " + noneCount
      + "/36</span><h2>【绘心甚八／旁观者】</h2><p>你的自我被厚厚的防御机制包裹，你拒绝被现成选项定义。以下角色排名仅根据剩余有效维度给出参考，不必把它当作最终答案。</p></article>";
  }

  function renderDimensionGrid(container, scores) {
    container.innerHTML = dimensions.map(function (dimension) {
      const value = scores[dimension];
      return "<article class=\"dimension-item\"><header><b>" + dimension + "</b><strong>"
        + (value > 0 ? "+" : "") + value + "</strong></header><p>" + dimensionDescriptions[dimension] + "</p></article>";
    }).join("");
  }

  function renderRankingList() {
    document.getElementById("ranking-list").innerHTML = currentRanking.slice(0, 3).map(function (entry, index) {
      const character = entry.character;
      return "<button type=\"button\" class=\"ranking-item\" data-character-id=\"" + escapeHtml(character.id)
        + "\"><span class=\"ranking-number\">0" + (index + 1) + "</span><span class=\"character-mark\" style=\"background:"
        + characterColor(character.id) + "\">" + escapeHtml(characterInitial(character.nameZh))
        + "</span><span class=\"ranking-copy\"><b>" + escapeHtml(character.nameZh)
        + "</b><small class=\"name-ja\">" + escapeHtml(character.nameJa) + "</small><small>"
        + escapeHtml(character.mbti + " · " + character.enneagram + " · " + character.tritype)
        + "</small></span><span class=\"ranking-score\"><b>" + entry.similarity + "%</b><small>距离 "
        + entry.distance.toFixed(2) + "</small></span></button>";
    }).join("");
  }

  function normalizedRadarValue(scores, dimension) {
    const stat = DATA.stats[dimension];
    const z = (scores[dimension] - stat.mean) / (stat.sd || 1);
    return Math.max(7, Math.min(95, 50 + z * 18));
  }

  function drawRadar(canvas, primaryScores, secondaryScores) {
    if (!canvas || !primaryScores) return;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width) return;
    const size = Math.max(280, rect.width);
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(size * ratio);
    canvas.height = Math.round(size * ratio);
    const context = canvas.getContext("2d");
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, size, size);
    const center = size / 2;
    const radius = size * 0.31;
    const count = dimensions.length;
    const angleFor = function (index) { return -Math.PI / 2 + index * Math.PI * 2 / count; };

    context.lineWidth = 1;
    [0.2, 0.4, 0.6, 0.8, 1].forEach(function (ring) {
      context.beginPath();
      dimensions.forEach(function (_, index) {
        const angle = angleFor(index);
        const x = center + Math.cos(angle) * radius * ring;
        const y = center + Math.sin(angle) * radius * ring;
        if (index === 0) context.moveTo(x, y); else context.lineTo(x, y);
      });
      context.closePath();
      context.strokeStyle = ring === 1 ? "rgba(96,154,228,.34)" : "rgba(96,154,228,.14)";
      context.stroke();
    });
    dimensions.forEach(function (dimension, index) {
      const angle = angleFor(index);
      context.beginPath();
      context.moveTo(center, center);
      context.lineTo(center + Math.cos(angle) * radius, center + Math.sin(angle) * radius);
      context.strokeStyle = "rgba(96,154,228,.14)";
      context.stroke();
      const labelRadius = radius + size * 0.065;
      const x = center + Math.cos(angle) * labelRadius;
      const y = center + Math.sin(angle) * labelRadius;
      context.fillStyle = "#91a4c0";
      context.font = "700 " + Math.max(10, size * 0.025) + "px sans-serif";
      context.textAlign = Math.cos(angle) > 0.25 ? "left" : (Math.cos(angle) < -0.25 ? "right" : "center");
      context.textBaseline = Math.sin(angle) > 0.25 ? "top" : (Math.sin(angle) < -0.25 ? "bottom" : "middle");
      context.fillText(dimension, x, y);
    });

    function polygon(scores, stroke, fill, width) {
      context.beginPath();
      dimensions.forEach(function (dimension, index) {
        const angle = angleFor(index);
        const value = normalizedRadarValue(scores, dimension) / 100;
        const x = center + Math.cos(angle) * radius * value;
        const y = center + Math.sin(angle) * radius * value;
        if (index === 0) context.moveTo(x, y); else context.lineTo(x, y);
      });
      context.closePath();
      context.fillStyle = fill;
      context.fill();
      context.strokeStyle = stroke;
      context.lineWidth = width;
      context.stroke();
    }

    if (secondaryScores) polygon(secondaryScores, "rgba(53,197,255,.8)", "rgba(53,197,255,.08)", 1.5);
    polygon(primaryScores, "#ff4f94", "rgba(255,79,148,.16)", 2.4);
  }

  function roundedRectPath(context, x, y, width, height, radius) {
    const safeRadius = Math.min(radius, width / 2, height / 2);
    context.beginPath();
    context.moveTo(x + safeRadius, y);
    context.lineTo(x + width - safeRadius, y);
    context.quadraticCurveTo(x + width, y, x + width, y + safeRadius);
    context.lineTo(x + width, y + height - safeRadius);
    context.quadraticCurveTo(x + width, y + height, x + width - safeRadius, y + height);
    context.lineTo(x + safeRadius, y + height);
    context.quadraticCurveTo(x, y + height, x, y + height - safeRadius);
    context.lineTo(x, y + safeRadius);
    context.quadraticCurveTo(x, y, x + safeRadius, y);
    context.closePath();
  }

  function fillRoundedRect(context, x, y, width, height, radius, fill) {
    roundedRectPath(context, x, y, width, height, radius);
    context.fillStyle = fill;
    context.fill();
  }

  function drawShareRadar(context, userScores, characterScores, centerX, centerY, radius) {
    const count = dimensions.length;
    const angleFor = function (index) { return -Math.PI / 2 + index * Math.PI * 2 / count; };
    [0.25, 0.5, 0.75, 1].forEach(function (ring) {
      context.beginPath();
      dimensions.forEach(function (_, index) {
        const angle = angleFor(index);
        const x = centerX + Math.cos(angle) * radius * ring;
        const y = centerY + Math.sin(angle) * radius * ring;
        if (index === 0) context.moveTo(x, y); else context.lineTo(x, y);
      });
      context.closePath();
      context.strokeStyle = ring === 1 ? "rgba(91,181,255,.42)" : "rgba(91,181,255,.16)";
      context.lineWidth = 2;
      context.stroke();
    });
    dimensions.forEach(function (dimension, index) {
      const angle = angleFor(index);
      context.beginPath();
      context.moveTo(centerX, centerY);
      context.lineTo(centerX + Math.cos(angle) * radius, centerY + Math.sin(angle) * radius);
      context.strokeStyle = "rgba(91,181,255,.14)";
      context.lineWidth = 2;
      context.stroke();
      const labelRadius = radius + 45;
      const labelX = centerX + Math.cos(angle) * labelRadius;
      const labelY = centerY + Math.sin(angle) * labelRadius;
      context.fillStyle = "#a9bbd3";
      context.font = "700 24px sans-serif";
      context.textAlign = Math.cos(angle) > 0.25 ? "left" : (Math.cos(angle) < -0.25 ? "right" : "center");
      context.textBaseline = Math.sin(angle) > 0.25 ? "top" : (Math.sin(angle) < -0.25 ? "bottom" : "middle");
      context.fillText(dimension, labelX, labelY);
    });

    function polygon(scores, stroke, fill, width) {
      context.beginPath();
      dimensions.forEach(function (dimension, index) {
        const angle = angleFor(index);
        const value = normalizedRadarValue(scores, dimension) / 100;
        const x = centerX + Math.cos(angle) * radius * value;
        const y = centerY + Math.sin(angle) * radius * value;
        if (index === 0) context.moveTo(x, y); else context.lineTo(x, y);
      });
      context.closePath();
      context.fillStyle = fill;
      context.fill();
      context.strokeStyle = stroke;
      context.lineWidth = width;
      context.stroke();
    }

    polygon(characterScores, "#35c5ff", "rgba(53,197,255,.10)", 4);
    polygon(userScores, "#ff4f94", "rgba(255,79,148,.18)", 6);
    context.textBaseline = "alphabetic";
  }

  function createShareCardDataUrl() {
    if (!currentRanking.length) throw new Error("尚未生成测评结果");
    const calculated = calculateScores();
    const winner = currentRanking[0];
    const canvas = document.createElement("canvas");
    canvas.width = 900;
    canvas.height = 1200;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("当前设备无法生成分享图");

    const background = context.createLinearGradient(0, 0, 900, 1200);
    background.addColorStop(0, "#030710");
    background.addColorStop(0.58, "#071426");
    background.addColorStop(1, "#130c1b");
    context.fillStyle = background;
    context.fillRect(0, 0, 900, 1200);
    context.strokeStyle = "rgba(92,153,231,.07)";
    context.lineWidth = 1;
    for (let x = 0; x <= 900; x += 45) {
      context.beginPath(); context.moveTo(x, 0); context.lineTo(x, 1200); context.stroke();
    }
    for (let y = 0; y <= 1200; y += 45) {
      context.beginPath(); context.moveTo(0, y); context.lineTo(900, y); context.stroke();
    }

    context.fillStyle = "rgba(36,124,255,.15)";
    context.beginPath(); context.arc(790, 110, 180, 0, Math.PI * 2); context.fill();
    context.fillStyle = "rgba(255,79,148,.10)";
    context.beginPath(); context.arc(80, 1080, 160, 0, Math.PI * 2); context.fill();

    fillRoundedRect(context, 44, 36, 812, 1128, 42, "rgba(5,13,25,.78)");
    context.strokeStyle = "rgba(75,160,255,.32)";
    context.lineWidth = 2;
    roundedRectPath(context, 44, 36, 812, 1128, 42);
    context.stroke();

    context.textAlign = "left";
    context.fillStyle = "#ff70aa";
    context.font = "800 28px sans-serif";
    context.fillText("Sakura樱语", 82, 92);
    context.fillStyle = "#6d83a2";
    context.font = "700 18px sans-serif";
    context.fillText("BLUE LOCK · EGO TEST", 82, 124);
    context.fillStyle = "#35c5ff";
    context.fillRect(718, 82, 98, 4);

    context.textAlign = "center";
    context.fillStyle = "#8297b4";
    context.font = "700 23px sans-serif";
    context.fillText("我的 Ego 最接近", 450, 180);
    context.fillStyle = "#f6f9ff";
    context.font = "900 62px Georgia, 'Songti SC', serif";
    context.fillText(winner.character.nameZh, 450, 247);
    context.fillStyle = "#8da1bd";
    context.font = "500 22px sans-serif";
    context.fillText(winner.character.nameJa, 450, 282);
    context.fillStyle = "#a9d9ff";
    context.font = "700 21px sans-serif";
    context.fillText(winner.character.mbti + " · " + winner.character.enneagram + " · Tritype " + winner.character.tritype, 450, 325);

    fillRoundedRect(context, 338, 350, 224, 88, 44, "rgba(28,105,198,.20)");
    context.strokeStyle = "rgba(63,184,255,.48)";
    context.lineWidth = 2;
    roundedRectPath(context, 338, 350, 224, 88, 44);
    context.stroke();
    context.fillStyle = "#ffffff";
    context.font = "900 48px Georgia, serif";
    context.fillText(winner.similarity + "%", 450, 409);
    context.fillStyle = "#7890ad";
    context.font = "700 17px sans-serif";
    context.fillText("相似指数", 450, 462);

    drawShareRadar(context, calculated.scores, winner.character.dimensions, 450, 700, 150);
    context.fillStyle = "#ff4f94";
    context.fillRect(318, 924, 24, 6);
    context.fillStyle = "#9caec5";
    context.font = "600 18px sans-serif";
    context.textAlign = "left";
    context.fillText("你", 352, 931);
    context.fillStyle = "#35c5ff";
    context.fillRect(456, 924, 24, 6);
    context.fillStyle = "#9caec5";
    context.fillText(winner.character.nameZh, 490, 931);

    context.textAlign = "left";
    context.fillStyle = "#6f87a6";
    context.font = "800 18px sans-serif";
    context.fillText("TOP 3  最接近的角色", 82, 954);
    currentRanking.slice(0, 3).forEach(function (entry, index) {
      const y = 986 + index * 52;
      context.fillStyle = index === 0 ? "#ff70aa" : "#5fbaff";
      context.font = "900 20px Georgia, serif";
      context.fillText("0" + (index + 1), 82, y + 25);
      context.fillStyle = "#eef5ff";
      context.font = "800 24px sans-serif";
      context.fillText(entry.character.nameZh, 138, y + 25);
      context.textAlign = "right";
      context.fillStyle = "#62c4ff";
      context.font = "900 25px Georgia, serif";
      context.fillText(entry.similarity + "%", 810, y + 25);
      context.textAlign = "left";
    });

    context.textAlign = "center";
    context.fillStyle = "#60738f";
    context.font = "600 17px sans-serif";
    context.fillText("36题 · 8维行动驱力 · 33位角色样本", 450, 1152);
    return canvas.toDataURL("image/png");
  }

  function hostMiniToolApi(method) {
    const miniTool = window.xhs && window.xhs.miniTool;
    return miniTool && typeof miniTool[method] === "function" ? miniTool[method].bind(miniTool) : null;
  }

  function withBusyButton(button, label, action) {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = label;
    let pending;
    try {
      pending = action();
    } catch (error) {
      button.disabled = false;
      button.textContent = original;
      showToast("操作未完成，请重试或检查系统授权");
      return;
    }
    Promise.resolve(pending).catch(function () {
      showToast("操作未完成，请重试或检查系统授权");
    }).finally(function () {
      button.disabled = false;
      button.textContent = original;
    });
  }

  function postResultNote(button) {
    const postNote = hostMiniToolApi("postNote");
    if (!postNote) {
      showToast("请在小红书 App 真机中使用发布功能");
      return;
    }
    withBusyButton(button, "正在生成…", function () {
      const image = createShareCardDataUrl();
      const winner = currentRanking[0];
      const topThree = currentRanking.slice(0, 3).map(function (entry) {
        return entry.character.nameZh + " " + entry.similarity + "%";
      }).join("、");
      const characterTag = winner.character.nameZh.replace(/[·•・\s]/g, "");
      const noteTags = "#Sakura樱语 #蓝色监狱Ego角色测评 #蓝色监狱 #" + characterTag;
      return postNote({
        title: ("我的蓝锁Ego是" + winner.character.nameZh).slice(0, 20),
        content: "我在「Sakura樱语」完成了36题Ego角色测评。最接近的是"
          + winner.character.nameZh + "（相似指数 " + winner.similarity + "%），前三名：" + topThree
          + "。相似指数用于直观展示角色距离，不是概率，也不代表官方结论。\n\n" + noteTags,
        tags: noteTags,
        mediaInfo: { image_resources: [{ url: image }] }
      }).then(function () {
        showToast("已唤起发布页，可继续编辑或取消");
      });
    });
  }

  function saveResultImage(button) {
    const saveImage = hostMiniToolApi("saveImageToPhotosAlbum");
    if (!saveImage) {
      showToast("请在小红书 App 真机中使用保存功能");
      return;
    }
    withBusyButton(button, "正在生成…", function () {
      return saveImage({ filePath: createShareCardDataUrl() }).then(function () {
        showToast("结果卡已保存到相册");
      });
    });
  }

  function renderResult() {
    const calculated = calculateScores();
    currentRanking = rankCharacters(calculated.scores);
    const winner = currentRanking[0];
    renderHiddenEnding(calculated.noneCount);
    setCharacterMark(document.getElementById("winner-mark"), winner.character);
    document.getElementById("result-heading").textContent = winner.character.nameZh;
    document.getElementById("winner-name-ja").textContent = winner.character.nameJa;
    document.getElementById("winner-types").innerHTML = typeBadges(winner.character);
    document.getElementById("winner-similarity").textContent = winner.similarity;
    document.getElementById("winner-summary").textContent = topDimensionSummary(calculated.scores, winner.character);
    document.getElementById("legend-character-name").textContent = winner.character.nameZh;
    renderDimensionGrid(document.getElementById("dimension-grid"), calculated.scores);
    renderRankingList();
    setView(resultView);
    reviewMode = false;
    window.requestAnimationFrame(function () {
      drawRadar(document.getElementById("result-radar"), calculated.scores, winner.character.dimensions);
    });
  }

  function galleryCard(character) {
    return "<button type=\"button\" class=\"character-card\" data-character-id=\"" + escapeHtml(character.id)
      + "\"><span class=\"character-mark\" style=\"background:" + characterColor(character.id) + "\">"
      + escapeHtml(characterInitial(character.nameZh)) + "</span><h2>" + escapeHtml(character.nameZh)
      + "</h2><p class=\"character-card-ja\">" + escapeHtml(character.nameJa)
      + "</p><div class=\"mini-types\"><span>" + escapeHtml(character.mbti) + "</span><span>"
      + escapeHtml(character.enneagram) + "</span><span>" + escapeHtml(character.tritype)
      + "</span></div><footer><span>36 题心路</span><span>以上皆非 " + character.noneCount + "</span></footer></button>";
  }

  function renderGallery(query) {
    const normalized = String(query || "").trim().toLocaleLowerCase("zh-CN");
    const filtered = DATA.characters.filter(function (character) {
      const haystack = [character.nameZh, character.nameJa, character.id, character.mbti, character.enneagram, character.tritype].join(" ").toLocaleLowerCase("zh-CN");
      return !normalized || haystack.indexOf(normalized) >= 0;
    });
    document.getElementById("character-grid").innerHTML = filtered.map(galleryCard).join("");
    document.getElementById("gallery-empty").classList.toggle("is-hidden", filtered.length > 0);
  }

  function openGallery(returnView) {
    galleryReturnView = returnView || (resultView.classList.contains("is-active") ? "result-view" : "home-view");
    searchInput.value = "";
    renderGallery("");
    setView(galleryView);
  }

  function journeyMarkup(character) {
    return character.answers.map(function (answer) {
      const question = questionById[answer.questionId];
      const option = question.options.find(function (item) { return item.key === answer.choice; });
      return "<details class=\"journey-item\"><summary><div class=\"journey-question\"><b>" + question.id
        + "</b><span>" + escapeHtml(question.shortTitle) + "</span></div><div class=\"journey-choice\">选择 "
        + answer.choice + " · " + escapeHtml(option.label) + "</div></summary><div class=\"journey-body\"><div class=\"chosen-option\"><b>"
        + answer.choice + "</b>　" + escapeHtml(option.text)
        + "</div><div class=\"monologue-card original-card\"><label>角色原文</label><p>"
        + escapeHtml(answer.monologue)
        + "</p></div><div class=\"monologue-card translation-card\"><label>中文翻译</label><p>"
        + escapeHtml(answer.monologueZh) + "</p></div></div></details>";
    }).join("");
  }

  function openCharacter(characterId, returnView) {
    const character = characterById[characterId];
    if (!character) return;
    selectedCharacterId = characterId;
    detailReturnView = returnView || "gallery-view";
    setCharacterMark(document.getElementById("detail-mark"), character);
    document.getElementById("detail-name").textContent = character.nameZh;
    document.getElementById("detail-name-ja").textContent = character.nameJa;
    document.getElementById("detail-types").innerHTML = typeBadges(character);
    document.getElementById("detail-none-count").textContent = "以上皆非 " + character.noneCount + " 题";
    document.getElementById("detail-index").textContent = String(DATA.characters.indexOf(character) + 1).padStart(2, "0");
    renderDimensionGrid(document.getElementById("detail-dimensions"), character.dimensions);
    document.getElementById("journey-list").innerHTML = journeyMarkup(character);
    setView(detailView);
    window.requestAnimationFrame(function () {
      drawRadar(document.getElementById("detail-radar"), character.dimensions, null);
    });
  }

  function restartTest() {
    if (!window.confirm("确定清空当前答案并重新测评吗？")) return;
    answers = new Array(DATA.questions.length).fill(null);
    questionIndex = 0;
    currentRanking = [];
    reviewMode = false;
    saveProgress();
    renderQuestion();
    setView(testView);
  }

  function reviewAnswers() {
    questionIndex = 0;
    reviewMode = true;
    renderQuestion();
    setView(testView);
  }

  function activeViewId() {
    const active = views.find(function (view) { return view.classList.contains("is-active"); });
    return active ? active.id : "home-view";
  }

  document.addEventListener("click", function (event) {
    const option = event.target.closest("[data-option]");
    if (option) {
      selectOption(option.getAttribute("data-option"));
      return;
    }
    const characterButton = event.target.closest("[data-character-id]");
    if (characterButton) {
      const returnView = resultView.classList.contains("is-active") ? "result-view" : "gallery-view";
      openCharacter(characterButton.getAttribute("data-character-id"), returnView);
      return;
    }
    const actionButton = event.target.closest("[data-action]");
    if (!actionButton) return;
    const action = actionButton.getAttribute("data-action");
    if (action === "go-home") { setView(homeView); updateHomeProgress(); }
    else if (action === "start-test") beginNewTest();
    else if (action === "continue-test") continueTest();
    else if (action === "open-gallery") openGallery(activeViewId());
    else if (action === "test-back") {
      if (questionIndex > 0) moveQuestion(-1); else { setView(homeView); updateHomeProgress(); }
    }
    else if (action === "save-exit") { saveProgress(); setView(homeView); showToast("进度已保存在本机"); }
    else if (action === "previous-question") moveQuestion(-1);
    else if (action === "next-question") moveQuestion(1);
    else if (action === "restart-test") restartTest();
    else if (action === "post-note") postResultNote(actionButton);
    else if (action === "save-result-image") saveResultImage(actionButton);
    else if (action === "review-answers") reviewAnswers();
    else if (action === "open-winner" && currentRanking.length) openCharacter(currentRanking[0].character.id, "result-view");
    else if (action === "gallery-back") setView(document.getElementById(galleryReturnView) || homeView);
    else if (action === "detail-back") setView(document.getElementById(detailReturnView) || galleryView);
  });

  searchInput.addEventListener("input", function () { renderGallery(searchInput.value); });
  window.addEventListener("resize", function () {
    setViewportHeight();
    if (resultView.classList.contains("is-active") && currentRanking.length) {
      const calculated = calculateScores();
      drawRadar(document.getElementById("result-radar"), calculated.scores, currentRanking[0].character.dimensions);
    }
    if (detailView.classList.contains("is-active") && selectedCharacterId) {
      drawRadar(document.getElementById("detail-radar"), characterById[selectedCharacterId].dimensions, null);
    }
  });
  if (window.visualViewport) window.visualViewport.addEventListener("resize", setViewportHeight);

  setViewportHeight();
  readProgress();
  updateHomeProgress();
  renderGallery("");
})();
