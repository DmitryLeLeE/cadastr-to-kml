// Ранняя установка темы до отрисовки страницы (без inline-скриптов из-за CSP).
try {
  const saved = localStorage.getItem("cadastr-theme");
  if (saved === "dark" || saved === "light") {
    document.documentElement.dataset.theme = saved;
  }
} catch (error) {
  // localStorage может быть недоступен в приватном режиме — не критично.
}
