(() => {
  'use strict';
  const video = document.getElementById('camera-preview');
  const canvas = document.getElementById('camera-canvas');
  const start = document.getElementById('camera-start');
  const stop = document.getElementById('camera-stop');
  const status = document.getElementById('camera-status');
  const code = document.getElementById('id_code');
  const form = document.getElementById('scan-form');
  let stream = null;
  let frame = null;
  let starting = false;
  let lastScan = 0;
  function stopCamera() {
    if (frame !== null) cancelAnimationFrame(frame);
    frame = null;
    if (stream) stream.getTracks().forEach(track => track.stop());
    stream = null;
    video.srcObject = null;
    video.hidden = true;
    stop.hidden = true;
    start.disabled = false;
  }
  function scan(now) {
    if (!stream) return;
    if (video.readyState >= 2 && now - lastScan >= 250) {
      lastScan = now;
      const scale = Math.min(1, 960 / Math.max(video.videoWidth, 1));
      canvas.width = Math.round(video.videoWidth * scale);
      canvas.height = Math.round(video.videoHeight * scale);
      if (canvas.width && canvas.height) {
        const context = canvas.getContext('2d', {willReadFrequently: true});
        context.drawImage(video, 0, 0, canvas.width, canvas.height);
        const image = context.getImageData(0, 0, canvas.width, canvas.height);
        const result = window.jsQR(image.data, image.width, image.height, {inversionAttempts: 'dontInvert'});
        if (result && result.data.startsWith('GYM1:') && result.data.length <= 800) {
          code.value = result.data;
          stopCamera();
          status.textContent = 'Card captured. Confirm the branch and action, then check access and record.';
          document.getElementById('scan-submit').focus();
          return;
        }
      }
    }
    frame = requestAnimationFrame(scan);
  }
  start.addEventListener('click', async () => {
    if (stream || starting) return;
    if (!navigator.mediaDevices?.getUserMedia || !window.jsQR) {
      status.textContent = 'Camera scanning is unavailable here. Use HTTPS or localhost, or paste/scan the code into the form.';
      return;
    }
    starting = true;
    start.disabled = true;
    try {
      stream = await navigator.mediaDevices.getUserMedia({video: {facingMode: {ideal: 'environment'}}, audio: false});
      if (document.hidden) { stopCamera(); return; }
      video.srcObject = stream;
      video.hidden = false;
      stop.hidden = false;
      await video.play();
      status.textContent = 'Point the camera at a member card. Nothing is recorded until you submit the form.';
      frame = requestAnimationFrame(scan);
    } catch (_) {
      stopCamera();
      status.textContent = 'Could not open the camera. Check camera permissions or use a USB scanner/paste the code.';
    } finally {
      starting = false;
    }
  });
  stop.addEventListener('click', () => {
    stopCamera();
    status.textContent = 'Camera stopped.';
  });
  // USB scanners often send Enter; staff still confirms branch/action explicitly.
  code.addEventListener('keydown', event => {
    if (event.key === 'Enter') { event.preventDefault(); document.getElementById('scan-submit').focus(); }
  });
  form.addEventListener('submit', () => { stopCamera(); });
  window.addEventListener('pagehide', stopCamera);
  document.addEventListener('visibilitychange', () => { if (document.hidden) stopCamera(); });
})();
