document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('input[type=file]').forEach(function (input) {
    input.addEventListener('change', function () {
      window.__studentUploadState = null;
      const target = document.querySelector('[data-file-count="' + input.name + '"]');
      const hint = input.closest('.upload-field')?.querySelector('.file-hint');
      const defaultHint = input.name === 'roster' ? '点击选择 .xlsx 文件' : input.name === 'photos' ? '点击或拖入照片' : '点击选择文档';
      if (!target) return;
      if (!input.files.length) {
        target.textContent = '';
        if (hint) hint.textContent = defaultHint;
      } else if (input.multiple) {
        target.textContent = '已选择 ' + input.files.length + ' 个文件';
        if (hint) hint.textContent = '文件已加入，可继续调整选择';
      } else {
        target.textContent = '已选择：' + input.files[0].name;
        if (hint) hint.textContent = 'Excel 文件已选择';
      }
    });
  });
  const submitForm = document.getElementById('submit-form');
  const progress = document.getElementById('upload-progress');
  const submitButton = document.getElementById('submit-button');
  if (submitForm && progress && submitButton) {
    async function fileFingerprint(file) {
      const metadata = file.name + '\\0' + file.size + '\\0' + file.lastModified;
      if (window.crypto && window.crypto.subtle) {
        const digest = await window.crypto.subtle.digest('SHA-256', await file.arrayBuffer());
        return Array.from(new Uint8Array(digest)).map(function (value) { return value.toString(16).padStart(2, '0'); }).join('');
      }
      return 'meta:' + metadata;
    }
    async function buildPhotoManifest(files) {
      const manifest = [];
      for (const file of files) {
        const fingerprint = await fileFingerprint(file);
        manifest.push({name: file.name, size: file.size, sha256: fingerprint.indexOf('meta:') === 0 ? '' : fingerprint, fingerprint: fingerprint});
      }
      return manifest;
    }
    function buildDocumentManifest(files) {
      return files.map(function (file) { return {name: file.name, size: file.size, lastModified: file.lastModified}; });
    }
    async function uploadInBatches() {
      const photoInput = submitForm.querySelector('[name=photos]');
      const rosterInput = submitForm.querySelector('[name=roster]');
      const documentInput = submitForm.querySelector('[name=documents]');
      const batchSize = Number(submitForm.dataset.photoBatchSize || 20);
      const documentBatchSize = 10;
      const batchBytes = Number(submitForm.dataset.photoBatchMb || 32) * 1024 * 1024;
      const csrf = submitForm.querySelector('[name=csrfmiddlewaretoken]').value;
      const files = Array.from(photoInput?.files || []);
      const documents = Array.from(documentInput?.files || []);
      const photoManifest = await buildPhotoManifest(files);
      const rosterFingerprint = await fileFingerprint(rosterInput.files[0]);
      const documentManifest = buildDocumentManifest(documents);
      const inputKey = JSON.stringify({rosterFingerprint: rosterFingerprint, photoManifest: photoManifest, documentManifest: documentManifest});
      let state = window.__studentUploadState;
      if (state && state.inputKey !== inputKey) {
        state = window.__studentUploadState = null;
      }
      const jsonRequest = async function (url, data) {
        const response = await fetch(url, {method: 'POST', headers: {'X-CSRFToken': csrf}, body: data});
        const body = await response.json().catch(function () { return {}; });
        if (!response.ok) throw new Error(body.error || '上传失败，请重试');
        return body;
      };
      if (!state) {
        const startData = new FormData();
        startData.append('roster', rosterInput.files[0], rosterInput.files[0].name);
        startData.append('expected_photo_count', String(files.length));
        startData.append('expected_document_count', String(documents.length));
        startData.append('photo_manifest', JSON.stringify(photoManifest));
        startData.append('roster_fingerprint', rosterFingerprint);
        startData.append('note', submitForm.querySelector('[name=note]')?.value || '');
        const started = await jsonRequest(submitForm.dataset.uploadStart, startData);
        state = window.__studentUploadState = {id: started.upload_id, token: started.upload_token, photoIndex: 0, documentIndex: 0, photoBatchNo: 0, documentBatchNo: 0, inputKey: inputKey, manifestDigest: started.photo_manifest_digest, rosterFingerprint: rosterFingerprint};
      }
      submitButton.disabled = true;
      progress.style.display = 'block';
      const total = files.length + documents.length || 1;
      let completedCount = state.photoIndex + state.documentIndex;
      const uploadFileBatches = async function (items, fieldName, endpoint, indexKey, batchKey, maxCount) {
        while (state[indexKey] < items.length) {
          const start = state[indexKey];
          let end = start;
          let size = 0;
          while (end < items.length && end < start + maxCount && (end === start || size + items[end].size <= batchBytes)) {
            size += items[end].size;
            end += 1;
          }
          const batchData = new FormData();
          batchData.append('batch_no', String(state[batchKey]));
          batchData.append('upload_token', state.token);
          batchData.append('manifest_digest', state.manifestDigest || '');
          batchData.append('roster_fingerprint', state.rosterFingerprint || '');
          items.slice(start, end).forEach(function (file) { batchData.append(fieldName, file, file.name); });
          await jsonRequest(endpoint.replace('00000000-0000-0000-0000-000000000000', state.id), batchData);
          state[indexKey] = end;
          state[batchKey] += 1;
          completedCount += end - start;
          progress.querySelector('span').style.width = Math.min(95, Math.round((completedCount / total) * 95)) + '%';
        }
      };
      await uploadFileBatches(files, 'photos', submitForm.dataset.uploadBatch, 'photoIndex', 'photoBatchNo', batchSize);
      await uploadFileBatches(documents, 'documents', submitForm.dataset.uploadDocumentBatch, 'documentIndex', 'documentBatchNo', documentBatchSize);
      const completeData = new FormData();
      completeData.append('upload_token', state.token);
      completeData.append('manifest_digest', state.manifestDigest || '');
      completeData.append('roster_fingerprint', state.rosterFingerprint || '');
      const completed = await jsonRequest(submitForm.dataset.uploadComplete.replace('00000000-0000-0000-0000-000000000000', state.id), completeData);
      window.__studentUploadState = null;
      window.location.href = completed.result_url;
    }
    submitForm.addEventListener('submit', function (event) {
      const photoInput = submitForm.querySelector('[name=photos]');
      const documentInput = submitForm.querySelector('[name=documents]');
      const batchSize = Number(submitForm.dataset.photoBatchSize || 20);
      const batchBytes = Number(submitForm.dataset.photoBatchMb || 32) * 1024 * 1024;
      const totalPhotoBytes = Array.from(photoInput?.files || []).reduce(function (total, file) { return total + file.size; }, 0);
      const totalDocumentBytes = Array.from(documentInput?.files || []).reduce(function (total, file) { return total + file.size; }, 0);
      const rosterBytes = submitForm.querySelector('[name=roster]')?.files[0]?.size || 0;
      if (photoInput && (photoInput.files.length > batchSize || totalPhotoBytes > batchBytes || (documentInput && documentInput.files.length) || totalPhotoBytes + totalDocumentBytes + rosterBytes > batchBytes)) {
        event.preventDefault();
        uploadInBatches().catch(function (error) {
          submitButton.disabled = false;
          submitButton.textContent = '继续上传 →';
          window.alert(error.message);
        });
        return;
      }
      progress.style.display = 'block';
      submitButton.disabled = true;
      submitButton.textContent = '正在上传…';
      let width = 8;
      const timer = setInterval(function () { width = Math.min(width + 7, 92); progress.querySelector('span').style.width = width + '%'; }, 180);
      window.addEventListener('beforeunload', function () { clearInterval(timer); });
    });
  }
  const mode = document.querySelector('[name=mode]');
  const template = document.querySelector('[name=template]');
  const male = document.querySelector('[name=male_code]');
  const female = document.querySelector('[name=female_code]');
  const separator = document.querySelector('[name=separator]');
  const preview = document.getElementById('filename-preview');
  function renderPreview() {
    if (!preview || !mode) return;
    let text = template.value || '{姓名}';
    if (mode.value === 'name') text = '{姓名}';
    if (mode.value === 'gender_no') text = '{姓名}' + (separator.value || '') + '{性别编号}';
    const values = {'{姓名}':'张三','{性别编号}':male.value || '01','{学部}':'高中部','{班级}':'1','{就读类型}':'走读'};
    Object.keys(values).forEach(function (key) { text = text.replaceAll(key, values[key]); });
    preview.textContent = text + '.jpg';
  }
  [mode, template, male, female, separator].filter(Boolean).forEach(function (el) { el.addEventListener('input', renderPreview); el.addEventListener('change', renderPreview); });
  renderPreview();
  const photoModal = document.getElementById('photo-modal');
  const photoModalImage = document.getElementById('photo-modal-image');
  const photoModalCaption = document.getElementById('photo-modal-caption');
  const photoGuideOpen = document.getElementById('photo-guide-open');
  const firstPhotoChoice = document.querySelector('.photo-guide-choice');
  if (photoGuideOpen && firstPhotoChoice) {
    photoGuideOpen.addEventListener('click', function () { firstPhotoChoice.click(); });
  }
  document.querySelectorAll('.photo-zoom-trigger').forEach(function (trigger) {
    trigger.addEventListener('click', function () {
      photoModalImage.src = trigger.dataset.photo;
      photoModalImage.alt = trigger.dataset.caption || '照片示例大图';
      photoModalCaption.textContent = trigger.dataset.caption || '';
      photoModal.hidden = false;
      document.body.style.overflow = 'hidden';
    });
  });
  document.querySelectorAll('[data-photo-close]').forEach(function (close) {
    close.addEventListener('click', function () {
      photoModal.hidden = true;
      photoModalImage.src = '';
      document.body.style.overflow = '';
    });
  });
});

