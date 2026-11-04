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

  async function checkInBatches(endpoint, form, roster, photos) {
    if (photos.length > 300 || photos.reduce(function (total, file) { return total + file.size; }, 0) > 300 * 20 * 1024 * 1024) throw new Error('照片数量或总大小超过限制');
    var batches = [], maxCount = 20, maxBytes = 32 * 1024 * 1024;
    for (var index = 0; index < photos.length;) {
      var end = index, size = 0;
      while (end < photos.length && end < index + maxCount && (end === index || size + photos[end].size + roster.size <= maxBytes)) { size += photos[end].size; end += 1; }
      if (end === index) throw new Error('单张照片与 Excel 合计超过单批预检查限制');
      batches.push(photos.slice(index, end)); index = end;
    }
    if (!batches.length) batches.push([]);
    var aggregate = {summary: {student_count: 0, photo_count: 0, matched_count: 0, missing_count: 0, manual_count: 0}, records: [], photos: [], blocking_errors: [], warnings: []};
    for (var batchIndex = 0; batchIndex < batches.length; batchIndex += 1) {
      var body = new FormData(); body.append('roster', roster, roster.name);
      batches[batchIndex].forEach(function (file) { body.append('photos', file, file.name); });
      var response = await fetch(endpoint, {method: 'POST', body: body, credentials: 'same-origin', headers: {'X-CSRFToken': csrfToken(form), 'X-Requested-With': 'XMLHttpRequest'}, cache: 'no-store'});
      var data; try { data = await response.json(); } catch (error) { throw new Error('预检查返回无效结果，请重试'); }
      if (!response.ok) throw new Error(data.error || '预检查失败，请重试');
      if (!aggregate.records.length) { aggregate.records = data.records || []; aggregate.summary.student_count = Number((data.summary || {}).student_count || 0); }
      aggregate.photos.push.apply(aggregate.photos, data.photos || []);
      aggregate.blocking_errors.push.apply(aggregate.blocking_errors, data.blocking_errors || []);
      aggregate.warnings.push.apply(aggregate.warnings, data.warnings || []);
    }
    aggregate.summary.photo_count = aggregate.photos.length;
    aggregate.summary.matched_count = aggregate.photos.filter(function (photo) { return photo.status === 'matched'; }).length;
    aggregate.summary.manual_count = aggregate.photos.filter(function (photo) { return photo.status === 'manual'; }).length;
    var matchedNames = aggregate.photos.filter(function (photo) { return photo.status === 'matched'; }).map(function (photo) { return photo.filename.replace(/\.[^.]+$/, '').trim().toLocaleLowerCase(); });
    aggregate.summary.missing_count = aggregate.records.filter(function (record) { return matchedNames.indexOf(String(record.name || '').trim().toLocaleLowerCase()) < 0; }).length;
    return aggregate;
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
    root.querySelector('[data-preflight-continue]')?.addEventListener('click', function () {
      var target = root.dataset.submitUrl || '/submit/';
      window.location.assign(target + (target.indexOf('?') >= 0 ? '&' : '?') + 'from=preflight');
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
      var button = form.querySelector('button[type="submit"]');
      button.disabled = true;
      try {
        var data = await checkInBatches(endpoint, form, roster, Array.from(form.querySelector('[name="photos"]')?.files || []));
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
