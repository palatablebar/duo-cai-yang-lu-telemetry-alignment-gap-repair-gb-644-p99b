/* 缺口页：替测候选核对与批准。未批准不改网格；批准后整页刷新显示留痕。 */
(function () {
  "use strict";

  function $(id) {
    return document.getElementById(id);
  }

  function setHint(text, kind) {
    const node = $("sub-hint");
    if (window.Gridlab) {
      window.Gridlab.setHint(node, text, kind);
    }
  }

  function setStatus(text, kind) {
    if (window.Gridlab) {
      window.Gridlab.setStatus(text, kind);
    }
  }

  async function getJSON(url) {
    const response = await fetch(url);
    const body = await response.json().catch(function () {
      return {};
    });
    if (!response.ok) {
      throw new Error(body.message || body.error || "HTTP " + response.status);
    }
    return body;
  }

  function renderChecks(candidate) {
    const table = $("sub-check-table");
    const body = $("sub-check-body");
    body.textContent = "";
    candidate.checks.forEach(function (item, index) {
      const tr = document.createElement("tr");
      tr.innerHTML =
        '<th class="gutter">' + (index + 1) + "</th>" +
        "<td>" + item.name + "</td>" +
        '<td><span class="tag ' + (item.ok ? "ok" : "bad") + '">' +
        (item.ok ? "通过" : "不通过") + "</span></td>" +
        "<td>" + item.detail + "</td>";
      body.appendChild(tr);
    });
    if (candidate.uncovered_buckets.length) {
      const tr = document.createElement("tr");
      tr.innerHTML =
        '<th class="gutter">·</th><td>未覆盖缺口桶</th>' +
        '<td><span class="tag bad">不通过</span></td><td class="mono">' +
        candidate.uncovered_buckets.join("，") + "</td>";
      body.appendChild(tr);
    }
    table.hidden = false;
    const approve = $("sub-approve");
    approve.disabled = !candidate.eligible;
    setHint(
      candidate.eligible
        ? "核对通过，可批准替测 " + candidate.donor_buckets.length + " 桶"
        : "核对未通过：" + candidate.failures.join("、"),
      candidate.eligible ? "ok" : "bad"
    );
  }

  async function loadDonors() {
    const target = $("sub-target");
    const donorSelect = $("sub-donor");
    donorSelect.textContent = "";
    $("sub-check-table").hidden = true;
    $("sub-approve").disabled = true;
    if (!target) {
      return;
    }
    // 枚举接口给出全部候选邻表及各自核对结论。
    const all = await fetch("/api/substitute/candidate-list", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sensor_id: target.value })
    }).then(function (r) {
      return r.json();
    });
    all.candidates.forEach(function (item) {
      const option = document.createElement("option");
      option.value = item.donor_sensor_id;
      option.textContent =
        item.donor_sensor_id + (item.eligible ? "（可替测）" : "（" + item.failures.join("/") + "）");
      donorSelect.appendChild(option);
    });
  }

  function bind() {
    const target = $("sub-target");
    const donorSelect = $("sub-donor");
    const checkBtn = $("sub-check");
    const approveBtn = $("sub-approve");
    if (!target || !donorSelect || !checkBtn || !approveBtn) {
      return;
    }

    target.addEventListener("change", loadDonors);
    loadDonors();

    checkBtn.addEventListener("click", async function () {
      setHint("核对中", "");
      try {
        const candidate = await getJSON(
          "/api/substitute/candidates?" +
            "sensor_id=" + encodeURIComponent(target.value) +
            "&donor_sensor_id=" + encodeURIComponent(donorSelect.value)
        );
        renderChecks(candidate);
      } catch (error) {
        setHint("核对失败：" + error.message, "bad");
      }
    });

    approveBtn.addEventListener("click", async function () {
      if (approveBtn.disabled) {
        return;
      }
      approveBtn.disabled = true;
      setHint("批准提交中", "");
      try {
        const result = await window.Gridlab.postJSON("/api/substitute/approve", {
          sensor_id: target.value,
          donor_sensor_id: donorSelect.value
        });
        const word = result.reused ? "决定已存在（幂等返回）" : "已批准 " + result.applied + " 桶";
        setStatus("替测 " + result.sensor_id + " ← " + result.donor_sensor_id + "：" + word, "ok");
        window.setTimeout(function () {
          window.location.reload();
        }, 400);
      } catch (error) {
        setHint("批准失败，缺口已保留：" + error.message, "bad");
        setStatus("替测批准失败", "bad");
        approveBtn.disabled = false;
      }
    });
  }

  document.addEventListener("DOMContentLoaded", bind);
})();
