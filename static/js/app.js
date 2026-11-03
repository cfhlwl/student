document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('input[type=file]').forEach(function (input) {
    input.addEventListener('change', function () {
      const target = document.querySelector('[data-file-count="' + input.name + '"]');
      if (!target) return;
      if (!input.files.length) {
        target.textContent = '';
      } else if (input.multiple) {
        target.textContent = '已选择 ' + input.files.length + ' 个文件';
      } else {
        target.textContent = '已选择：' + input.files[0].name;
      }
    });
  });
  const submitForm = document.getElementById('submit-form');
  const progress = document.getElementById('upload-progress');
  const submitButton = document.getElementById('submit-button');
  if (submitForm && progress && submitButton) {
    submitForm.addEventListener('submit', function () {
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

