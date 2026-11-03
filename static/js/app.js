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
  const progressText = document.getElementById('upload-progress-text');
  const retryButton = document.getElementById('upload-retry');
  const submitButton = document.getElementById('submit-button');
  if (submitForm && progress && submitButton) {
    const placeholder = '00000000-0000-0000-0000-000000000000';
    const storageKey = 'student-collector-upload-session-v3';
    const sleep = function (ms) { return new Promise(function (resolve) { window.setTimeout(resolve, ms); }); };
    function sha256Pure(buffer) {
      const bytes = new Uint8Array(buffer), words = new Uint32Array(64);
      const K = [0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2];
      const bit = function (x, n) { return (x >>> n) | (x << (32 - n)); }, rotr = function (x, n) { return bit(x, n) >>> 0; };
      const paddedLength = ((bytes.length + 9 + 63) >> 6) << 6, padded = new Uint8Array(paddedLength);
      padded.set(bytes); padded[bytes.length] = 0x80;
      const view = new DataView(padded.buffer); view.setUint32(paddedLength - 4, bytes.length * 8, false);
      let h0 = 0x6a09e667, h1 = 0xbb67ae85, h2 = 0x3c6ef372, h3 = 0xa54ff53a, h4 = 0x510e527f, h5 = 0x9b05688c, h6 = 0x1f83d9ab, h7 = 0x5be0cd19;
      for (let offset = 0; offset < padded.length; offset += 64) {
        for (let i = 0; i < 16; i++) words[i] = view.getUint32(offset + i * 4, false);
        for (let i = 16; i < 64; i++) { const s0 = rotr(words[i - 15], 7) ^ rotr(words[i - 15], 18) ^ (words[i - 15] >>> 3); const s1 = rotr(words[i - 2], 17) ^ rotr(words[i - 2], 19) ^ (words[i - 2] >>> 10); words[i] = (words[i - 16] + s0 + words[i - 7] + s1) >>> 0; }
        let a=h0,b=h1,c=h2,d=h3,e=h4,f=h5,g=h6,h=h7;
        for (let i = 0; i < 64; i++) { const S1 = rotr(e,6)^rotr(e,11)^rotr(e,25), ch = (e&f)^((~e)&g), t1 = (h+S1+ch+K[i]+words[i])>>>0, S0 = rotr(a,2)^rotr(a,13)^rotr(a,22), maj = (a&b)^(a&c)^(b&c), t2 = (S0+maj)>>>0; h=g;g=f;f=e;e=(d+t1)>>>0;d=c;c=b;b=a;a=(t1+t2)>>>0; }
        h0=(h0+a)>>>0;h1=(h1+b)>>>0;h2=(h2+c)>>>0;h3=(h3+d)>>>0;h4=(h4+e)>>>0;h5=(h5+f)>>>0;h6=(h6+g)>>>0;h7=(h7+h)>>>0;
      }
      return [h0,h1,h2,h3,h4,h5,h6,h7].map(function (v) { return v.toString(16).padStart(8, '0'); }).join('');
    }
    async function fileFingerprint(file) {
      if (!file) throw new Error('请先选择 Excel 名单');
      const buffer = await file.arrayBuffer();
      if (window.crypto && window.crypto.subtle) {
        const digest = await window.crypto.subtle.digest('SHA-256', buffer);
        return Array.from(new Uint8Array(digest)).map(function (value) { return value.toString(16).padStart(2, '0'); }).join('');
      }
      return sha256Pure(buffer);
    }
    async function textFingerprint(text) {
      const buffer = new TextEncoder().encode(text).buffer;
      if (window.crypto && window.crypto.subtle) {
        const digest = await window.crypto.subtle.digest('SHA-256', buffer);
        return Array.from(new Uint8Array(digest)).map(function (value) { return value.toString(16).padStart(2, '0'); }).join('');
      }
      return sha256Pure(buffer);
    }
    function randomNonce() {
      const bytes = new Uint8Array(32);
      if (window.crypto && window.crypto.getRandomValues) {
        window.crypto.getRandomValues(bytes);
      } else {
        for (let index = 0; index < bytes.length; index++) bytes[index] = Math.floor(Math.random() * 256);
      }
      return Array.from(bytes).map(function (value) { return value.toString(16).padStart(2, '0'); }).join('');
    }
    async function buildPhotoManifest(files) {
      const manifest = [];
      for (const file of files) manifest.push({name: file.name, size: file.size, sha256: await fileFingerprint(file)});
      return manifest;
    }
    async function buildDocumentManifest(files) {
      const manifest = [];
      for (const file of files) manifest.push({name: file.name, size: file.size, sha256: await fileFingerprint(file)});
      return manifest;
    }
    function persist(state) { window.__studentUploadState = state; sessionStorage.setItem(storageKey, JSON.stringify(state)); }
    function clearPersisted() { window.__studentUploadState = null; sessionStorage.removeItem(storageKey); }
    function urlFor(template, id) { return template.replace(placeholder, id); }
    function xhrJson(url, data, csrf, onProgress, method) {
      return new Promise(function (resolve, reject) {
        const xhr = new XMLHttpRequest(); xhr.open(method || 'POST', url); xhr.setRequestHeader('X-CSRFToken', csrf); xhr.timeout = 180000;
        if (onProgress) xhr.upload.onprogress = function (event) { if (event.lengthComputable) onProgress(event.loaded, event.total); };
        xhr.onload = function () { let body = {}; try { body = JSON.parse(xhr.responseText || '{}'); } catch (error) {} if (xhr.status >= 200 && xhr.status < 300) resolve(body); else reject(new Error(body.error || '上传失败，请重试')); };
        xhr.onerror = function () { reject(new Error('网络连接中断，请重试失败部分')); }; xhr.ontimeout = function () { reject(new Error('上传请求超时，请重试失败部分')); };
        xhr.send(data);
      });
    }
    function setProgress(percent, text) { progress.style.display = 'block'; progress.querySelector('span').style.width = Math.max(0, Math.min(100, percent)) + '%'; if (progressText) progressText.textContent = text; }
    function bytesText(value) { return (value / 1024 / 1024).toFixed(1) + ' MB'; }
    async function retryRequest(makeRequest, onProgress) { let lastError; for (let attempt = 1; attempt <= 3; attempt++) { try { return await makeRequest(onProgress); } catch (error) { lastError = error; if (attempt < 3) await sleep(attempt * 700); } } throw lastError; }
    function compareUploads(items, uploads) { return uploads.every(function (row, index) { const item = items[index]; return item && row.name === item.name && Number(row.size) === item.size && row.sha256 === item.sha256; }); }
    async function readStatus(state, photoManifest, documentManifest, rosterFingerprint, csrf) {
      const statusData = new FormData(); statusData.append('upload_token', state.token); statusData.append('manifest_digest', state.manifestDigest || ''); statusData.append('roster_fingerprint', rosterFingerprint);
      const status = await xhrJson(urlFor(submitForm.dataset.uploadStatus, state.id), statusData, csrf, null, 'POST');
      if (status.status === 'finalizing') throw new Error('服务器正在完成本次提交，请稍后刷新并重新选择文件');
      if (status.photo_manifest_digest !== state.manifestDigest || JSON.stringify(status.photo_manifest || []) !== JSON.stringify(photoManifest) || JSON.stringify(status.document_manifest || []) !== JSON.stringify(documentManifest)) throw new Error('Excel/照片/文档清单或内容已变化，请重新选择后提交');
      const batches = (status.batches || []).slice().sort(function (a, b) { return a.kind.localeCompare(b.kind) || a.sequence - b.sequence; });
      const photos = [], documents = [];
      batches.forEach(function (batch) { (batch.kind === 'photo' ? photos : documents).push.apply(batch.kind === 'photo' ? photos : documents, batch.uploads || []); });
      if (!compareUploads(photoManifest, photos) || !compareUploads(documentManifest, documents)) throw new Error('已上传文件与当前选择不一致，请重新提交');
      state.photoIndex = photos.length; state.documentIndex = documents.length;
      state.photoBatchNo = (batches.filter(function (b) { return b.kind === 'photo'; }).length); state.documentBatchNo = (batches.filter(function (b) { return b.kind === 'document'; }).length);
      state.failedBatch = null; persist(state);
      return status;
    }
    async function uploadInBatches() {
      const photoInput = submitForm.querySelector('[name=photos]'), rosterInput = submitForm.querySelector('[name=roster]'), documentInput = submitForm.querySelector('[name=documents]');
      const batchSize = Number(submitForm.dataset.photoBatchSize || 20), documentBatchSize = 10, batchBytes = Number(submitForm.dataset.photoBatchMb || 32) * 1024 * 1024, csrf = submitForm.querySelector('[name=csrfmiddlewaretoken]').value;
      const files = Array.from(photoInput?.files || []), documents = Array.from(documentInput?.files || []);
      if (!rosterInput?.files[0]) throw new Error('请先选择 Excel 名单');
      const photoManifest = await buildPhotoManifest(files), documentManifest = await buildDocumentManifest(documents), rosterFingerprint = await fileFingerprint(rosterInput.files[0]);
      const inputKey = JSON.stringify({rosterFingerprint: rosterFingerprint, photoManifest: photoManifest, documentManifest: documentManifest});
      let state = window.__studentUploadState;
      try { if (!state) state = JSON.parse(sessionStorage.getItem(storageKey) || 'null'); } catch (error) { state = null; }
      if (state && (state.inputKey !== inputKey || !state.idempotencyKey)) { clearPersisted(); state = null; }
      if (!state) {
        state = {id: null, token: '', photoIndex: 0, documentIndex: 0, photoBatchNo: 0, documentBatchNo: 0, inputKey: inputKey, idempotencyKey: randomNonce(), manifestDigest: '', rosterFingerprint: rosterFingerprint, failedBatch: null};
        persist(state);
      }
      submitButton.disabled = true; if (retryButton) retryButton.hidden = true; setProgress(2, '正在校验文件内容…');
      if (state.id) {
        const status = await readStatus(state, photoManifest, documentManifest, rosterFingerprint, csrf);
        if (status.completed_submission_id && status.result_url) { window.location.href = status.result_url; return; }
      } else {
        const startData = new FormData(); startData.append('roster', rosterInput.files[0], rosterInput.files[0].name); startData.append('expected_photo_count', String(files.length)); startData.append('expected_document_count', String(documents.length)); startData.append('photo_manifest', JSON.stringify(photoManifest)); startData.append('document_manifest', JSON.stringify(documentManifest)); startData.append('roster_fingerprint', rosterFingerprint); startData.append('idempotency_key', state.idempotencyKey); startData.append('note', submitForm.querySelector('[name=note]')?.value || '');
        const started = await retryRequest(function (onProgress) { return xhrJson(submitForm.dataset.uploadStart, startData, csrf, function (loaded, total) { setProgress(Math.round((loaded / total) * 5), '正在建立上传会话… ' + bytesText(loaded)); }); });
        state.id = started.upload_id; state.token = started.upload_token; state.manifestDigest = started.photo_manifest_digest; state.rosterFingerprint = rosterFingerprint; persist(state);
      }
      const totalBytes = (rosterInput.files[0]?.size || 0) + files.reduce(function (n, f) { return n + f.size; }, 0) + documents.reduce(function (n, f) { return n + f.size; }, 0);
      let confirmedBytes = (rosterInput.files[0]?.size || 0) + files.slice(0, state.photoIndex).reduce(function (n, f) { return n + f.size; }, 0) + documents.slice(0, state.documentIndex).reduce(function (n, f) { return n + f.size; }, 0);
      let confirmedCount = state.photoIndex + state.documentIndex;
      const uploadFileBatches = async function (items, fieldName, endpoint, indexKey, batchKey, maxCount, kind) {
        while (state[indexKey] < items.length) {
          const start = state[indexKey]; let end = start, size = 0;
          while (end < items.length && end < start + maxCount && (end === start || size + items[end].size <= batchBytes)) { size += items[end].size; end += 1; }
          const batchNo = state[batchKey]; state.failedBatch = {kind: kind, index: start, sequence: batchNo}; persist(state);
          const makeData = function () { const data = new FormData(); data.append('batch_no', String(batchNo)); data.append('upload_token', state.token); data.append('manifest_digest', state.manifestDigest || ''); data.append('roster_fingerprint', state.rosterFingerprint || ''); items.slice(start, end).forEach(function (file) { data.append(fieldName, file, file.name); }); return data; };
          const body = await retryRequest(function () { return xhrJson(urlFor(endpoint, state.id), makeData(), csrf, function (loaded, total) { const percent = totalBytes ? ((confirmedBytes + loaded) / totalBytes) * 100 : 0; setProgress(Math.min(99, percent), '第 ' + (batchNo + 1) + ' 批：已传输 ' + bytesText(confirmedBytes + loaded) + '/' + bytesText(totalBytes) + ' · 服务器已确认照片 ' + state.photoIndex + '/' + files.length + ' · 成功 ' + confirmedCount + '，待上传 ' + (files.length + documents.length - confirmedCount) + '，失败 0'); }); });
          if (!body || Number(body.accepted) !== end - start) throw new Error('服务器确认数量与本批文件不一致');
          state[indexKey] = end; state[batchKey] = batchNo + 1; state.failedBatch = null; confirmedBytes += size; confirmedCount += end - start; persist(state);
          setProgress(Math.min(99, totalBytes ? (confirmedBytes / totalBytes) * 100 : 99), '已传输 ' + bytesText(confirmedBytes) + '/' + bytesText(totalBytes) + ' · 服务器已确认照片 ' + state.photoIndex + '/' + files.length + ' · 成功 ' + confirmedCount + '，待上传 ' + (files.length + documents.length - confirmedCount) + '，失败 0');
        }
      };
      await uploadFileBatches(files, 'photos', submitForm.dataset.uploadBatch, 'photoIndex', 'photoBatchNo', batchSize, 'photo');
      await uploadFileBatches(documents, 'documents', submitForm.dataset.uploadDocumentBatch, 'documentIndex', 'documentBatchNo', documentBatchSize, 'document');
      setProgress(99, '正在最终化，请勿关闭页面…');
      const completeData = new FormData(); completeData.append('upload_token', state.token); completeData.append('manifest_digest', state.manifestDigest || ''); completeData.append('roster_fingerprint', state.rosterFingerprint || '');
      const completed = await retryRequest(function () { return xhrJson(urlFor(submitForm.dataset.uploadComplete, state.id), completeData, csrf); });
      clearPersisted(); setProgress(100, '全部提交成功'); window.location.href = completed.result_url;
    }
    function handleUploadError(error) { submitButton.disabled = false; submitButton.textContent = '继续上传 →'; if (retryButton) retryButton.hidden = !sessionStorage.getItem(storageKey); if (progressText) progressText.textContent = error.message; window.alert(error.message); }
    submitForm.addEventListener('submit', function (event) {
      const photoInput = submitForm.querySelector('[name=photos]'), documentInput = submitForm.querySelector('[name=documents]');
      const batchSize = Number(submitForm.dataset.photoBatchSize || 20), batchBytes = Number(submitForm.dataset.photoBatchMb || 32) * 1024 * 1024;
      const totalPhotoBytes = Array.from(photoInput?.files || []).reduce(function (total, file) { return total + file.size; }, 0);
      const totalDocumentBytes = Array.from(documentInput?.files || []).reduce(function (total, file) { return total + file.size; }, 0);
      const rosterBytes = submitForm.querySelector('[name=roster]')?.files[0]?.size || 0;
      const shouldBatch = photoInput && (photoInput.files.length > batchSize || totalPhotoBytes > batchBytes || totalPhotoBytes + totalDocumentBytes + rosterBytes > batchBytes || photoInput.files.length || (documentInput && documentInput.files.length) || sessionStorage.getItem(storageKey));
      if (shouldBatch) { event.preventDefault(); uploadInBatches().catch(handleUploadError); return; }
      progress.style.display = 'block'; submitButton.disabled = true; submitButton.textContent = '正在上传…';
    });
    if (retryButton) retryButton.addEventListener('click', function () { uploadInBatches().catch(handleUploadError); });
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

