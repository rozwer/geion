(() => {
  const API_BASE = "/api";
  const POLL_INTERVAL_MS = 1500;
  const SYSTEM_POLL_MS = 10000;

  const form = document.getElementById("scrape-form");
  const emailInput = document.getElementById("email");
  const passwordInput = document.getElementById("password");
  const excludeInput = document.getElementById("exclude");
  const submitButton = document.getElementById("submit-btn");
  const resetButton = document.getElementById("reset-btn");
  const feedback = document.getElementById("feedback");
  const queueInfo = document.getElementById("queue-info");
  const resultPanel = document.getElementById("result-panel");
  const statusTags = document.getElementById("status-tags");
  const resultSummary = document.getElementById("result-summary");
  const jsonOutput = document.getElementById("json-output");
  const systemStatus = document.getElementById("system-status");

  const downloadBands = document.getElementById("download-bands");
  const downloadBandRank = document.getElementById("download-band-rank");
  const downloadMemberRank = document.getElementById("download-member-rank");

  let activeJobId = null;
  let activePoll = null;
  let lastResult = null;

  function setFeedback(message, mode = "info") {
    feedback.textContent = message;
    feedback.classList.remove("is-error", "is-success");
    if (mode === "error") {
      feedback.classList.add("is-error");
    } else if (mode === "success") {
      feedback.classList.add("is-success");
    }
  }

  function updateQueueInfo(message) {
    queueInfo.textContent = message || "";
  }

  function toggleForm(disabled) {
    emailInput.disabled = disabled;
    passwordInput.disabled = disabled;
    excludeInput.disabled = disabled;
    submitButton.disabled = disabled;
    resetButton.disabled = !disabled && !lastResult;
  }

  function clearResult() {
    lastResult = null;
    resultPanel.hidden = true;
    statusTags.innerHTML = "";
    resultSummary.innerHTML = "";
    jsonOutput.textContent = "";
    downloadBands.disabled = true;
    downloadBandRank.disabled = true;
    downloadMemberRank.disabled = true;
  }

  function formatDateLabel(value) {
    if (!value) {
      return "-";
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return value;
    }
    return date.toLocaleString("ja-JP", { hour12: false });
  }

  async function extractError(response) {
    try {
      const data = await response.json();
      if (data && typeof data === "object") {
        if (data.detail) {
          return Array.isArray(data.detail) ? data.detail.join(", ") : String(data.detail);
        }
        if (data.error) {
          return String(data.error);
        }
      }
    } catch (err) {
      console.warn("Failed to parse error payload", err);
    }
    const text = await response.text();
    return text || "不明なエラーが発生しました";
  }

  function buildSummary(result, snapshot) {
    if (!result || !result.meta) {
      return "";
    }
    const lines = [
      `<div><strong>バンド数</strong><span>${result.meta.bandCount ?? "-"}</span></div>`,
      `<div><strong>生成時刻</strong><span>${formatDateLabel(result.meta.generatedAt)}</span></div>`,
      `<div><strong>除外ニックネーム</strong><span>${result.meta.excludedNickname || "-"}</span></div>`,
    ];
    if (snapshot.startedAt) {
      lines.push(`<div><strong>処理開始</strong><span>${formatDateLabel(snapshot.startedAt)}</span></div>`);
    }
    if (snapshot.finishedAt) {
      lines.push(`<div><strong>処理終了</strong><span>${formatDateLabel(snapshot.finishedAt)}</span></div>`);
    }
    if (snapshot.durationSeconds != null) {
      lines.push(`<div><strong>処理時間</strong><span>${snapshot.durationSeconds} 秒</span></div>`);
    }
    return lines.join("");
  }

  function renderTags(jobId, snapshot) {
    statusTags.innerHTML = "";
    const status = snapshot.status || "-";
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = `状態: ${status}`;
    statusTags.appendChild(tag);

    if (jobId) {
      const idTag = document.createElement("span");
      idTag.className = "tag";
      idTag.textContent = `ジョブID: ${jobId}`;
      statusTags.appendChild(idTag);
    }

    if (snapshot.assignedWorker) {
      const workerTag = document.createElement("span");
      workerTag.className = "tag";
      workerTag.textContent = `ワーカー: ${snapshot.assignedWorker}`;
      statusTags.appendChild(workerTag);
    }
  }

  function updateDownloads(result) {
    const hasBands = Array.isArray(result?.bands) && result.bands.length > 0;
    const hasBandRank = Array.isArray(result?.bandRank) && result.bandRank.length > 0;
    const hasMemberRank = Array.isArray(result?.memberRank) && result.memberRank.length > 0;

    downloadBands.disabled = !hasBands;
    downloadBandRank.disabled = !hasBandRank;
    downloadMemberRank.disabled = !hasMemberRank;
  }

  function exportCsv(filename, records, columns) {
    if (!Array.isArray(records) || records.length === 0) {
      setFeedback("CSV に出力できるデータがありません。", "error");
      return;
    }

    const header = columns.map((col) => col.label).join(",");
    const rows = records.map((record) =>
      columns
        .map((col) => {
          const value = typeof col.getter === "function" ? col.getter(record) : record[col.key];
          const text = value == null ? "" : String(value);
          if (/[",\n]/.test(text)) {
            return `"${text.replace(/"/g, '""')}"`;
          }
          return text;
        })
        .join(",")
    );

    const csv = [header, ...rows].join("\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(link.href);
    setFeedback(`${filename} をダウンロードしました。`, "success");
  }

  function buildFilename(prefix, meta) {
    const stamp = meta?.generatedAt || new Date().toISOString();
    return `${prefix}_${stamp.replace(/[:T-]/g, "").replace(/\..+/, "")}.csv`;
  }

  function handleResult(result, snapshot) {
    lastResult = result;
    renderTags(activeJobId, snapshot);
    resultSummary.innerHTML = buildSummary(result, snapshot);
    jsonOutput.textContent = JSON.stringify(result, null, 2);
    updateDownloads(result);
    resultPanel.hidden = false;
    setFeedback("スクレイピングが完了しました。", "success");
    toggleForm(false);
  }

  async function pollJob(jobId) {
    activeJobId = jobId;
    let keepPolling = true;

    while (keepPolling) {
      try {
        const response = await fetch(`${API_BASE}/jobs/${jobId}`);
        if (!response.ok) {
          throw new Error(await extractError(response));
        }
        const snapshot = await response.json();
        const status = snapshot.status;

        if (status === "queued") {
          updateQueueInfo(`キュー待ち: 現在 ${snapshot.queueSize} 件`);
          setFeedback("キューで待機しています...", "info");
        } else if (status === "running") {
          updateQueueInfo(`処理中 (残りキュー: ${snapshot.queueSize})`);
          setFeedback("スクレイピングを実行中です...", "info");
        } else if (status === "succeeded") {
          updateQueueInfo("処理が完了しました。");
          handleResult(snapshot.result, snapshot);
          keepPolling = false;
        } else if (status === "failed" || status === "cancelled") {
          updateQueueInfo("処理が中断されました。");
          setFeedback(snapshot.error || "処理に失敗しました。", "error");
          toggleForm(false);
          keepPolling = false;
        } else {
          updateQueueInfo(`状態: ${status}`);
        }

        if (keepPolling) {
          await new Promise((resolve) => {
            activePoll = window.setTimeout(resolve, POLL_INTERVAL_MS);
          });
        }
      } catch (error) {
        console.error(error);
        setFeedback(error.message || "ステータスの取得に失敗しました。", "error");
        toggleForm(false);
        keepPolling = false;
      }
    }
    activeJobId = null;
  }

  async function refreshSystemStatus() {
    try {
      const response = await fetch(`${API_BASE}/system`);
      if (!response.ok) {
        throw new Error("system status error");
      }
      const stats = await response.json();
      systemStatus.textContent = `キュー: ${stats.queueSize} / 稼働中: ${stats.running} / 完了: ${stats.completed} / 最大同時: ${stats.maxConcurrency}`;
    } catch (error) {
      systemStatus.textContent = "ステータス取得中...";
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearTimeout(activePoll);
    setFeedback("キューに登録しています...", "info");
    updateQueueInfo("");
    clearResult();
    toggleForm(true);

    const payload = {
      email: emailInput.value.trim(),
      password: passwordInput.value,
      excludeNickname: excludeInput.value.trim(),
    };

    try {
      const response = await fetch(`${API_BASE}/scrape`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        throw new Error(await extractError(response));
      }
      const data = await response.json();
      updateQueueInfo(`キュー待ち: 現在 ${data.queueSize} 件`);
      await pollJob(data.jobId);
    } catch (error) {
      console.error(error);
      setFeedback(error.message || "ジョブの登録に失敗しました。", "error");
      toggleForm(false);
    }
  });

  resetButton.addEventListener("click", () => {
    clearTimeout(activePoll);
    setFeedback("", "info");
    updateQueueInfo("");
    clearResult();
    form.reset();
    toggleForm(false);
  });

  downloadBands.addEventListener("click", () => {
    if (!lastResult) {
      return;
    }
    const columns = [
      { label: "バンド名", getter: (row) => row.bandName || "" },
      { label: "メンバー", getter: (row) => (Array.isArray(row.members) ? row.members.join(" / ") : "") },
      { label: "曲一覧", getter: (row) => row.songs || "" },
      { label: "曲数", getter: (row) => row.songCount ?? "" },
    ];
    exportCsv(buildFilename("bands", lastResult.meta), lastResult.bands || [], columns);
  });

  downloadBandRank.addEventListener("click", () => {
    if (!lastResult) {
      return;
    }
    const columns = [
      { label: "バンド名", getter: (row) => row.bandName || "" },
      { label: "参加回数", getter: (row) => row.appearanceCount ?? "" },
      { label: "曲数合計", getter: (row) => row.songCount ?? "" },
    ];
    exportCsv(buildFilename("band_rank", lastResult.meta), lastResult.bandRank || [], columns);
  });

  downloadMemberRank.addEventListener("click", () => {
    if (!lastResult) {
      return;
    }
    const columns = [
      { label: "メンバー", getter: (row) => row.memberName || "" },
      { label: "参加回数", getter: (row) => row.appearanceCount ?? "" },
    ];
    exportCsv(buildFilename("member_rank", lastResult.meta), lastResult.memberRank || [], columns);
  });

  window.addEventListener("beforeunload", () => {
    clearTimeout(activePoll);
  });

  refreshSystemStatus();
  window.setInterval(refreshSystemStatus, SYSTEM_POLL_MS);
})();
