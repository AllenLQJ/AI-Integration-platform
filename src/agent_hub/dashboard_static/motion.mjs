// Motion helpers for the dashboard. Every effect collapses to an instant change when the
// viewer asks for reduced motion, and none of them is needed to read the page.

export function prefersReducedMotion(media = globalThis.matchMedia) {
  try { return Boolean(media?.('(prefers-reduced-motion: reduce)').matches); } catch { return false; }
}

/** Ease-out cubic: fast start, gentle landing. */
export const easeOut = t => 1 - (1 - Math.min(1, Math.max(0, t))) ** 3;

/** The value a count-up shows `elapsed` ms into a `duration` ms run from `from` to `to`. */
export function tween(from, to, elapsed, duration) {
  if (duration <= 0 || elapsed >= duration) return to;
  return from + (to - from) * easeOut(elapsed / duration);
}

/**
 * Count `el` up to the number in `text` (e.g. "65%", "1,240", "12"), keeping the suffix.
 * Non-numeric text is shown as is. Returns a function that stops the animation.
 */
export function countUp(el, text, {duration = 700, raf = globalThis.requestAnimationFrame, now = () => performance.now()} = {}) {
  const match = /^(\D*)(\d+(?:\.\d+)?)(.*)$/.exec(String(text));
  if (!match || !raf || prefersReducedMotion()) { el.textContent = String(text); return () => {}; }
  const [, prefix, number, suffix] = match, target = Number(number), decimals = (number.split('.')[1] || '').length;
  const start = now();
  let stopped = false;
  const frame = () => {
    if (stopped) return;
    const value = tween(0, target, now() - start, duration);
    el.textContent = prefix + value.toFixed(decimals) + suffix;
    if (value !== target) raf(frame);
  };
  el.textContent = prefix + (0).toFixed(decimals) + suffix;
  raf(frame);
  return () => { stopped = true; el.textContent = String(text); };
}

/** Give `nodes` the entrance animation, each a step after the previous one. */
export function stagger(nodes, offset = 0) {
  [...nodes].forEach((node, index) => {
    node.style?.setProperty?.('--i', String(offset + index));
    node.classList?.add('enter');
  });
}

/** Run `update` inside a view transition where the browser supports it. */
export function viewTransition(update, doc = globalThis.document) {
  if (doc?.startViewTransition && !prefersReducedMotion()) {
    try { return doc.startViewTransition(update); } catch { /* fall through to a plain update */ }
  }
  update();
  return null;
}
