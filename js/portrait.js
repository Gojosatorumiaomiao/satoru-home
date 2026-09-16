(() => {
  const video = document.getElementById('satoru-video');
  const button = document.getElementById('motion-toggle');
  if (!video || !button) return;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  let desired = !reduced.matches;
  let visible = true;
  video.muted = true;
  const sync = () => { button.textContent = video.paused ? '播放动画' : '暂停动画'; };
  const update = () => {
    if (desired && visible && !document.hidden) video.play().catch(sync);
    else video.pause();
    sync();
  };
  button.addEventListener('click', () => { desired = video.paused; update(); });
  video.addEventListener('play', sync);
  video.addEventListener('pause', sync);
  video.addEventListener('error', () => { button.hidden = true; video.controls = true; });
  reduced.addEventListener('change', () => { desired = !reduced.matches; update(); });
  document.addEventListener('visibilitychange', update);
  if ('IntersectionObserver' in window) new IntersectionObserver(entries => {
    visible = entries[0].isIntersecting; update();
  }).observe(video);
  button.hidden = false;
  video.controls = false;
  update();
})();
