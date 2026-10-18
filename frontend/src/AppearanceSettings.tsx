import { useEffect, useState } from "react";

type ThemeId = "default" | "next" | "cathay" | "scsb" | "sinopac" | "yuanta";
type ColorMode = "light" | "dark" | "system";

const themes: { id: ThemeId; name: string; description: string; colors: [string, string, string] }[] = [
  { id: "default", name: "原始配色", description: "溫暖彤紅與柔和玉綠", colors: ["#bf405b", "#43b7ad", "#fff8f3"] },
  { id: "next", name: "配色 A", description: "活力黃與科技灰", colors: ["#FAEC32", "#989898", "#20242B"] },
  { id: "cathay", name: "配色 B", description: "沉穩金融綠", colors: ["#00834B", "#185C45", "#EAF5EE"] },
  { id: "scsb", name: "配色 C", description: "暖紅與橘金", colors: ["#C94735", "#EE9D36", "#FFF4E7"] },
  { id: "sinopac", name: "配色 D", description: "俐落金融紅", colors: ["#C51F32", "#842535", "#F9EDEF"] },
  { id: "yuanta", name: "配色 E", description: "深藍與科技藍", colors: ["#0067A8", "#004477", "#29A7D7"] },
];

const themeIds = new Set(themes.map((item) => item.id));
const modeIds = new Set<ColorMode>(["light", "dark", "system"]);
const themeKey = "tongling-theme-v1";
const modeKey = "tongling-color-mode-v1";

function savedTheme(): ThemeId {
  try {
    const value = localStorage.getItem(themeKey) as ThemeId;
    return themeIds.has(value) ? value : "default";
  } catch { return "default"; }
}

function savedMode(): ColorMode {
  try {
    const value = localStorage.getItem(modeKey) as ColorMode;
    return modeIds.has(value) ? value : "system";
  } catch { return "system"; }
}

function applyAppearance(theme: ThemeId, mode: ColorMode) {
  const scheme = mode === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : mode === "system" ? "light" : mode;
  const root = document.documentElement;
  root.dataset.theme = theme;
  root.dataset.colorMode = mode;
  root.dataset.colorScheme = scheme;
  root.style.colorScheme = scheme;
  const surface = getComputedStyle(root).getPropertyValue("--background").trim();
  document.querySelector<HTMLMetaElement>('meta[name="theme-color"]')?.setAttribute("content", surface || (scheme === "dark" ? "#171b22" : "#fff8f3"));
}

// This stays active while the appearance page is closed, including during Agent work.
export function initAppearanceRuntime() {
  applyAppearance(savedTheme(), savedMode());
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (savedMode() === "system") applyAppearance(savedTheme(), "system");
  });
  window.addEventListener("storage", (event) => {
    if (event.key === themeKey || event.key === modeKey) applyAppearance(savedTheme(), savedMode());
  });
}

export function AppearanceSettings() {
  const [theme, setTheme] = useState(savedTheme);
  const [mode, setMode] = useState(savedMode);

  useEffect(() => {
    applyAppearance(theme, mode);
    try {
      localStorage.setItem(themeKey, theme);
      localStorage.setItem(modeKey, mode);
    } catch { /* Appearance remains usable when browser storage is disabled. */ }
  }, [theme, mode]);

  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key === themeKey || event.key === modeKey) {
        setTheme(savedTheme());
        setMode(savedMode());
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  return <div className="appearance-settings">
    <span className="eyebrow">PERSONAL APPEARANCE</span>
    <h2>外觀與主題</h2>
    <p>選擇適合你的工作色彩。這項偏好只保存在目前瀏覽器，不影響其他組員或專案內容。</p>
    <fieldset className="appearance-modes">
      <legend>明暗模式</legend>
      <div className="appearance-mode-options">
        {([ ["light", "淺色"], ["dark", "深色"], ["system", "跟隨系統"] ] as const).map(([id, label]) =>
          <label key={id} className={mode === id ? "active" : ""}>
            <input type="radio" name="appearance-mode" value={id} checked={mode === id} onChange={() => setMode(id)} />{label}
          </label>
        )}
      </div>
    </fieldset>
    <fieldset className="appearance-themes">
      <legend>主題配色</legend>
      <div className="appearance-grid">
        {themes.map((item) => <label key={item.id} className={`appearance-card${theme === item.id ? " active" : ""}`}>
          <input type="radio" name="appearance-theme" value={item.id} checked={theme === item.id} onChange={() => setTheme(item.id)} aria-label={item.name} />
          <span className="appearance-card-top"><strong>{item.name}</strong><span className="appearance-check" aria-hidden="true">{theme === item.id ? "✓ 已套用" : "選擇"}</span></span>
          <span className="appearance-swatches" aria-hidden="true">{item.colors.map((color, index) => <i key={index} style={{ backgroundColor: color }} />)}</span>
          <span className="appearance-mini" aria-hidden="true" style={{ backgroundColor: item.colors[2] }}><i style={{ backgroundColor: item.colors[0] }} /><b style={{ backgroundColor: item.colors[1] }} /><em style={{ borderColor: item.colors[0] }} /></span>
          <small>{item.description}</small>
        </label>)}
      </div>
    </fieldset>
    <small className="appearance-note">選擇只改變操作介面的顏色，不會改動正式文件或圖表內容。</small>
  </div>;
}
