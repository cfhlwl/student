(function () {
  "use strict";

  function setFeedback(message, isError) {
    document.querySelectorAll("[data-copy-feedback]").forEach(function (node) {
      node.textContent = message;
      node.classList.toggle("is-error", Boolean(isError));
    });
  }

  function legacyCopy(text) {
    var input = document.createElement("textarea");
    input.value = text;
    input.setAttribute("readonly", "");
    input.style.position = "fixed";
    input.style.opacity = "0";
    document.body.appendChild(input);
    input.select();
    var copied = false;
    try {
      copied = document.execCommand("copy");
    } catch (error) {
      copied = false;
    }
    input.remove();
    return copied;
  }

  function copyText(text) {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      return navigator.clipboard.writeText(text);
    }
    return legacyCopy(text) ? Promise.resolve() : Promise.reject(new Error("clipboard-unavailable"));
  }

  function setupCopyButtons() {
    document.querySelectorAll("[data-copy-target]").forEach(function (button) {
      button.addEventListener("click", function () {
        var target = document.getElementById(button.getAttribute("data-copy-target"));
        if (!target) return;
        button.disabled = true;
        copyText(target.textContent.trim()).then(function () {
          setFeedback("已复制，可粘贴保存。", false);
          button.textContent = "已复制";
          window.setTimeout(function () {
            button.textContent = button.getAttribute("data-copy-label") || "复制";
          }, 1800);
        }).catch(function () {
          setFeedback("浏览器未允许复制，请手动选中并复制。", true);
        }).finally(function () {
          button.disabled = false;
        });
      });
    });
  }

  function setupResultSummaryAndFilters() {
    var rows = Array.prototype.slice.call(document.querySelectorAll("[data-result-row]"));
    if (!rows.length) return;
    var counts = { total: rows.length, matched: 0, missing: 0, ambiguous: 0 };
    rows.forEach(function (row) {
      var status = row.getAttribute("data-match-status");
      if (Object.prototype.hasOwnProperty.call(counts, status)) counts[status] += 1;
    });
    Object.keys(counts).forEach(function (key) {
      var node = document.querySelector('[data-summary="' + key + '"]');
      if (node) node.textContent = String(counts[key]);
    });

    var empty = document.querySelector("[data-result-filter-empty]");
    document.querySelectorAll("[data-result-filter]").forEach(function (button) {
      button.addEventListener("click", function () {
        var filter = button.getAttribute("data-result-filter");
        var visible = 0;
        document.querySelectorAll("[data-result-filter]").forEach(function (item) {
          var active = item === button;
          item.classList.toggle("is-active", active);
          item.setAttribute("aria-pressed", active ? "true" : "false");
        });
        rows.forEach(function (row) {
          var show = filter === "all" || row.getAttribute("data-match-status") === filter;
          row.hidden = !show;
          if (show) visible += 1;
        });
        if (empty) empty.hidden = visible !== 0;
      });
    });
  }

  function setupSupplementReviewSummary() {
    document.querySelectorAll("[data-supplement-review]").forEach(function (card) {
      var missing = card.querySelector('[data-supplement-missing]');
      var exceptions = card.querySelector('[data-supplement-exceptions]');
      var studentStatuses = card.querySelectorAll("[data-student-match-status]");
      var photoRows = card.querySelectorAll("[data-supplement-photo]");
      if (missing) {
        var missingCount = 0;
        studentStatuses.forEach(function (node) {
          if (node.getAttribute("data-student-match-status") !== "matched") missingCount += 1;
        });
        missing.textContent = String(missingCount);
      }
      if (exceptions) {
        var exceptionCount = 0;
        photoRows.forEach(function (row) {
          if (row.getAttribute("data-match-status") !== "matched") exceptionCount += 1;
        });
        exceptions.textContent = String(exceptionCount);
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    setupCopyButtons();
    setupResultSummaryAndFilters();
    setupSupplementReviewSummary();
  });
}());
