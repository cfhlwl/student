(function () {
  'use strict';

  function csrfToken(form) {
    return form.querySelector('input[name="csrfmiddlewaretoken"]')?.value || '';
  }

  function issueText(issue) {
    var location = issue.row ? '第 ' + issue.row + ' 行：' : '';
    return location + (issue.filename ? issue.filename + '：' : '') + issue.message;
  }

  function renderIssues(root, key, issues) {
    var box = root.querySelector('[data-issues="' + key + '"]');
    if (!box) return;
    var list = box.querySelector('ul');
    list.textContent = '';
    (issues || []).forEach(function (issue) {
      var item = document.createElement('li');
      item.textContent = issueText(issue);
      list.appendChild(item);
    });
    box.hidden = !(issues || []).length;
  }

  function renderResult(root, payload) {
    var result = root.querySelector('#preflight-result');
    var summary = payload.summary || {};
    Object.keys(summary).forEach(function (key) {
      var node = root.querySelector('[data-summary="' + key + '"]');
      if (node) node.textContent = String(summary[key]);
    });
    renderIssues(root, 'blocking_errors', payload.blocking_errors);
    renderIssues(root, 'warnings', payload.warnings);
    result.hidden = false;
    updateContinue(root, payload);
  }

  function updateContinue(root, payload) {
    var button = root.querySelector('[data-preflight-continue]');
    var acknowledged = Boolean(root.querySelector('[data-warning-ack]')?.checked);
    var hasErrors = (payload.blocking_errors || []).length > 0;
    var hasWarnings = (payload.warnings || []).length > 0;
    button.disabled = hasErrors || (hasWarnings && !acknowledged);
    button.dataset.preflightReady = button.disabled ? 'false' : 'true';
  }

  function setFileCount(form, fieldName) {
    var input = form.querySelector('[name="' + fieldName + '"]');
    var target = form.querySelector('[data-file-count="' + fieldName + '"]');
    if (!input || !target) return;
    var count = input.files ? input.files.length : 0;
    target.textContent = count ? ('已选择 ' + count + ' 个文件') : '';
  }

  function mount(root) {
    var form = root.querySelector('#preflight-form');
    if (!form) return;
    var payload = null;
    ['roster', 'photos'].forEach(function (fieldName) {
      var input = form.querySelector('[name="' + fieldName + '"]');
      input?.addEventListener('change', function () {
        setFileCount(form, fieldName);
      });
    });
    root.querySelector('[data-warning-ack]')?.addEventListener('change', function () {
      if (payload) updateContinue(root, payload);
    });
    form.addEventListener('submit', async function (event) {
      event.preventDefault();
      var roster = form.querySelector('[name="roster"]')?.files?.[0];
      if (!roster) return;
      var endpoint = root.dataset.preflightEndpoint;
      if (!endpoint) {
        renderResult(root, { summary: { student_count: 0, photo_count: 0, matched_count: 0, missing_count: 0, manual_count: 0 }, blocking_errors: [{ message: '尚未配置预检查接口', row: null, filename: '' }], warnings: [] });
        return;
      }
      var body = new FormData(form);
      var button = form.querySelector('button[type="submit"]');
      button.disabled = true;
      try {
        var response = await fetch(endpoint, { method: 'POST', body: body, credentials: 'same-origin', headers: { 'X-CSRFToken': csrfToken(form), 'X-Requested-With': 'XMLHttpRequest' }, cache: 'no-store' });
        var data = await response.json();
        if (!response.ok) throw new Error(data.error || '预检查失败，请修正文件后重试');
        payload = data;
        renderResult(root, data);
      } catch (error) {
        renderResult(root, { summary: { student_count: 0, photo_count: 0, matched_count: 0, missing_count: 0, manual_count: 0 }, blocking_errors: [{ message: error.message || '预检查失败', row: null, filename: '' }], warnings: [] });
      } finally {
        button.disabled = false;
      }
    });
  }

  window.StudentCollectorPreflight = { mount: mount, renderResult: renderResult };
  document.addEventListener('DOMContentLoaded', function () {
    var root = document.getElementById('preflight-app');
    if (root) mount(root);
  });
}());
