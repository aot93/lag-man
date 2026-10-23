// Theme selection (docs/brand.md, "Layout and themes"): Auto follows the system setting,
// Light and Dark override it through data-theme on the root element. Shared by the
// generator control page and the analysis app.

const KEY = "lag-man.theme";
const THEMES = ["system", "light", "dark"];
const LABELS = { system: "Auto", light: "Light", dark: "Dark" };

export function loadTheme() {
  try {
    const v = localStorage.getItem(KEY);
    return THEMES.includes(v) ? v : "system";
  } catch {
    return "system";
  }
}

export function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
  try {
    if (theme === "system") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, theme);
  } catch {
    // Storage may be unavailable (private mode); the theme still applies for this visit.
  }
}

// Fills `container` with an Auto / Light / Dark segmented control.
export function themeControl(container) {
  let current = loadTheme();
  applyTheme(current);
  container.classList.add("seg", "theme-seg");
  container.setAttribute("role", "radiogroup");
  container.setAttribute("aria-label", "Theme");
  const buttons = THEMES.map((t) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "btn small";
    b.value = t;
    b.textContent = LABELS[t];
    b.setAttribute("role", "radio");
    b.addEventListener("click", () => { current = t; applyTheme(t); sync(); });
    return b;
  });
  function sync() {
    for (const b of buttons) {
      const on = b.value === current;
      b.classList.toggle("on", on);
      b.setAttribute("aria-checked", String(on));
    }
  }
  container.replaceChildren(...buttons);
  sync();
}
