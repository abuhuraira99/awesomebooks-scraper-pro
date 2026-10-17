// Applied before first paint so the saved theme never flashes.
try {
  const theme = localStorage.getItem("abp-theme");
  if (theme) document.documentElement.dataset.theme = theme;
} catch (e) {
  /* storage unavailable */
}
