// Appearance: follow the system, or force light or dark. The choice is a per-viewer convenience
// kept in localStorage; if storage is unavailable the page simply follows the system.

export const THEMES = ['system', 'light', 'dark'];
export const THEME_LABELS = {system: '跟随系统', light: '浅色', dark: '深色'};
/** Outline icons in the same style as the panel toggles: [tag, attributes] per shape. */
export const THEME_GLYPHS = {
  system: [['circle', {cx: 10, cy: 10, r: 6.5, class: 'icon-line'}], ['path', {d: 'M10 3.5a6.5 6.5 0 0 1 0 13z', class: 'icon-fill'}]],
  light: [['circle', {cx: 10, cy: 10, r: 3.2, class: 'icon-line'}],
    ['path', {d: 'M10 2.5v2M10 15.5v2M2.5 10h2M15.5 10h2M4.7 4.7l1.4 1.4M13.9 13.9l1.4 1.4M4.7 15.3l1.4-1.4M13.9 6.1l1.4-1.4', class: 'icon-line'}]],
  dark: [['path', {d: 'M15.8 12.4A6.5 6.5 0 0 1 7.6 4.2a6.5 6.5 0 1 0 8.2 8.2z', class: 'icon-line'}]],
};
const KEY = 'agent-hub:theme';

/** The next choice when the toggle is pressed: system → light → dark → system. */
export const nextTheme = theme => THEMES[(Math.max(0, THEMES.indexOf(theme)) + 1) % THEMES.length];

export function readTheme(storage = globalThis.localStorage) {
  try {
    const value = storage?.getItem(KEY);
    return THEMES.includes(value) ? value : 'system';
  } catch {
    return 'system';
  }
}

export function saveTheme(theme, storage = globalThis.localStorage) {
  try {
    if (theme === 'system') storage?.removeItem(KEY);
    else storage?.setItem(KEY, theme);
  } catch { /* the choice just will not be remembered */ }
}

/** `system` removes the override, so the stylesheet's prefers-color-scheme rules decide. */
export function applyTheme(theme, root = globalThis.document?.documentElement) {
  if (!root) return;
  if (theme === 'light' || theme === 'dark') root.setAttribute('data-theme', theme);
  else root.removeAttribute('data-theme');
}

/** Panel layout preferences (e.g. a collapsed sidebar), stored the same forgiving way. */
export function readFlag(name, storage = globalThis.localStorage) {
  try { return storage?.getItem(`agent-hub:${name}`) === '1'; } catch { return false; }
}

export function saveFlag(name, on, storage = globalThis.localStorage) {
  try {
    if (on) storage?.setItem(`agent-hub:${name}`, '1');
    else storage?.removeItem(`agent-hub:${name}`);
  } catch { /* not remembered */ }
}
