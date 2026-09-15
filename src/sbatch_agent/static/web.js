/* Progressive enhancement only. Domain validation and submit stay on server. */
"use strict";
document.documentElement.classList.add("enhanced");
function updateResourcePolicy(control) {
  const mode = control.querySelector("[data-policy-mode]").value;
  control.querySelectorAll("[data-policy-panel]").forEach(function (panel) {
    panel.hidden = panel.dataset.policyPanel !== mode;
    panel.querySelectorAll("input").forEach(function (input) { input.disabled = panel.hidden; });
  });
}
function markEdited(target) {
  const prepared = target.closest(".prepared-task");
  if (!prepared) return;
  prepared.querySelectorAll(".submit-job").forEach(function (button) { button.disabled = true; });
  if (!prepared.querySelector(".preview-needs-update")) {
    const hint = document.createElement("p");
    hint.className = "hint preview-needs-update";
    hint.textContent = "配置已修改，请更新预览后再确认。";
    prepared.querySelector(".submit-bar")?.prepend(hint);
  }
}
document.addEventListener("change", function (event) {
  const policy = event.target.closest("[data-resource-policy]");
  if (policy) updateResourcePolicy(policy);
  if (policy && event.target.closest("#smart-prepare-form")) clearPreparation();
  markEdited(event.target);
});
document.addEventListener("input", function (event) {
  markEdited(event.target);
  if (event.target.matches("[data-policy-input]") && event.target.closest("#smart-prepare-form")) clearPreparation();
});
function updateAnalyzeButton() {
  const button = document.getElementById("analyze-prepare");
  if (!button) return;
  const folder = document.querySelector('#working-folder input[name="folder_path"]')?.value;
  const manual = document.getElementById("smart-project")?.value.trim();
  button.disabled = !(folder || manual) || !document.getElementById("smart-intent").value.trim() ||
    document.getElementById("smart-prepare-form").classList.contains("htmx-request");
}
function clearPreparation() {
  document.getElementById("smart-prepare-result")?.replaceChildren();
}
document.addEventListener("input", function (event) {
  if (event.target.id === "smart-project") {
    const input = document.querySelector('#working-folder input[name="folder_path"]');
    if (input) input.value = "";
    document.querySelector(".chosen-folder")?.remove();
    document.getElementById("working-folder")?.removeAttribute("data-folder-confirmed");
    const choose = document.getElementById("choose-folder");
    if (choose) choose.textContent = "📁 选择文件夹";
  }
  if (["smart-project", "smart-intent"].includes(event.target.id)) {
    clearPreparation();
    updateAnalyzeButton();
  }
});
document.addEventListener("click", function (event) {
  if (event.target.closest("[data-folder-cancel]")) {
    document.getElementById("folder-picker")?.replaceChildren();
    document.getElementById("choose-folder")?.focus();
  }
});
updateAnalyzeButton();
document.addEventListener("click", function (event) {
  const button = event.target.closest("[data-tree-action]");
  if (!button) return;
  const tree = button.closest("[data-file-tree]");
  tree.querySelectorAll("[data-tree-dir]").forEach(function (directory) {
    directory.open = button.dataset.treeAction === "relevant" &&
      (directory.dataset.relevant === "true" || directory.parentElement.dataset.treeNode === ".");
  });
});

document.addEventListener("htmx:beforeRequest", function (event) {
  const target = event.detail.target;
  if (target && ["folder-picker", "working-folder"].includes(target.id) &&
      document.getElementById("smart-prepare-form")?.classList.contains("htmx-request")) {
    event.preventDefault();
    return;
  }
  if (target) target.setAttribute("aria-busy", "true");
  // Never leave an old confirmation actionable during an edit/new analysis.
  if (target && target.id === "smart-prepare-result") {
    target.querySelectorAll(".submit-job").forEach(function (button) { button.disabled = true; });
  }
});
document.addEventListener("htmx:beforeSwap", function (event) {
  // Render the server's sanitized partial errors, retaining their HTTP status.
  // No client evaluation, error-body logging or automatic retry.
  const xhr = event.detail.xhr;
  if (xhr.status >= 400 && xhr.getResponseHeader("Content-Type")?.startsWith("text/html")) {
    event.detail.shouldSwap = true;
  }
});
document.addEventListener("htmx:afterRequest", function (event) {
  if (event.detail.target) event.detail.target.removeAttribute("aria-busy");
  updateAnalyzeButton();
});
document.addEventListener("htmx:afterSwap", function (event) {
  const target = event.detail.target;
  if (target?.id === "working-folder" && document.querySelector("#working-folder[data-folder-confirmed]")) {
    document.getElementById("folder-picker")?.replaceChildren();
    const manual = document.getElementById("smart-project");
    if (manual) manual.value = "";
    clearPreparation();
    updateAnalyzeButton();
    document.getElementById("smart-intent")?.focus();
  }
  if (target?.id === "folder-picker") target.querySelector(".folder-browser")?.focus({preventScroll: true});
  const focus = target?.querySelector('[role="alert"], .prepared-task');
  if (focus) focus.focus({preventScroll: true});
  if (target && ["smart-prepare-result", "project-scan-result"].includes(target.id)) {
    target.scrollIntoView({behavior: "instant", block: "start"});
  }
});
function transportFailure(event) {
  const target = event.detail.target;
  if (!target) return;
  target.removeAttribute("aria-busy");
  const message = document.createElement("p");
  message.className = "alert alert-danger";
  message.setAttribute("role", "alert");
  message.textContent = "连接未完成，请重新打开准备结果核对。仍可手动配置；不会自动提交或重试。";
  target.prepend(message);
}
document.addEventListener("htmx:sendError", transportFailure);
document.addEventListener("htmx:timeout", transportFailure);
document.addEventListener("submit", function (event) {
  const loading = event.submitter?.dataset.loadingLabel ? event.submitter : null;
  if (loading) {
    if (loading.dataset.submitting) { event.preventDefault(); return; }
    loading.dataset.submitting = "true";
    const spinner = loading.querySelector("[data-loading-spinner]");
    const label = loading.querySelector("[data-loading-text]");
    if (spinner) spinner.hidden = false;
    if (label) label.textContent = loading.dataset.loadingLabel;
    loading.disabled = true;
  }
  // Regular POST/redirect for real submission; no fetch/HTMX interception.
  const button = event.submitter;
  if (button?.classList.contains("submit-job")) {
    if (button.dataset.submitting) { event.preventDefault(); return; }
    button.dataset.submitting = "true";
    button.textContent = "正在提交…";
    button.setAttribute("aria-disabled", "true");
  }
});
