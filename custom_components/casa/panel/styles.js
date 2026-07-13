// Casa admin panel — design system. One stylesheet adopted by the app shell's
// shadow root; every view renders plain DOM styled by these classes. Tokens map
// to HA theme CSS vars so light/dark themes work without extra rules.

const CSS = `
:host {
  --casa-primary: var(--primary-color, #03a9f4);
  --casa-on-primary: var(--text-primary-color, #fff);
  --casa-bg: var(--primary-background-color, #fafafa);
  --casa-card-bg: var(--card-background-color, #fff);
  --casa-bg-2: var(--secondary-background-color, #f5f5f5);
  --casa-divider: var(--divider-color, #e0e0e0);
  --casa-text: var(--primary-text-color, #212121);
  --casa-text-2: var(--secondary-text-color, #727272);
  --casa-error: var(--error-color, #db4437);
  --casa-warning: var(--warning-color, #f4b400);
  --casa-success: var(--success-color, #0f9d58);
  --casa-header-bg: var(--app-header-background-color, var(--primary-color, #03a9f4));
  --casa-header-fg: var(--app-header-text-color, #fff);
  --casa-radius: 12px;
  --casa-radius-sm: 8px;
  --casa-shadow: 0 1px 3px rgba(0, 0, 0, 0.12);
  --casa-shadow-pop: 0 4px 16px rgba(0, 0, 0, 0.22);

  display: block;
  height: 100%;
  background: var(--casa-bg);
  color: var(--casa-text);
  font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif);
  font-size: 14px;
}

* { box-sizing: border-box; }
.spacer { flex: 1; }
.mono { font-family: SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
.muted { color: var(--casa-text-2); }
[hidden] { display: none !important; }

/* ---------- App header ---------- */
.app-header {
  display: flex; align-items: center; gap: 12px;
  height: 64px; padding: 0 16px;
  background: var(--casa-header-bg); color: var(--casa-header-fg);
}
.app-header__icon-tile {
  width: 40px; height: 40px; border-radius: 10px;
  background: rgba(255, 255, 255, 0.15);
  display: flex; align-items: center; justify-content: center;
  --mdc-icon-size: 24px;
}
.app-header__titles { display: flex; flex-direction: column; line-height: 1.25; min-width: 0; }
.app-header__title { font-size: 17px; font-weight: 600; white-space: nowrap; }
.app-header__subtitle { font-size: 12px; opacity: 0.8; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.app-header .btn--icon { color: var(--casa-header-fg); }

/* ---------- Main layout ---------- */
#casa-main { height: calc(100% - 64px); overflow-y: auto; }
.page { padding: 16px 24px 32px; max-width: 1400px; margin: 0 auto; }
.page--flush { padding: 0; max-width: none; height: 100%; }

/* ---------- Cards ---------- */
.card {
  background: var(--casa-card-bg);
  border: 1px solid var(--casa-divider);
  border-radius: var(--casa-radius);
  box-shadow: var(--casa-shadow);
}
.card--clickable { cursor: pointer; transition: box-shadow 0.15s, border-color 0.15s; }
.card--clickable:hover { box-shadow: 0 3px 10px rgba(0, 0, 0, 0.16); border-color: var(--casa-primary); }
.card__body { padding: 14px 16px; }
.card__footer {
  padding: 10px 16px; border-top: 1px solid var(--casa-divider);
  display: flex; align-items: center; gap: 8px;
}
.card--danger { border-color: var(--casa-error); }

/* Big option card (provision entry / deploy method cards, ESPHome "Create new project" style) */
.option-card {
  display: flex; align-items: center; gap: 14px; width: 100%; text-align: left;
  padding: 18px 16px; margin: 0 0 12px 0;
  background: var(--casa-card-bg); color: var(--casa-text);
  border: 1px solid var(--casa-divider); border-radius: var(--casa-radius);
  cursor: pointer; font: inherit; transition: border-color 0.15s, box-shadow 0.15s;
}
.option-card:hover { border-color: var(--casa-primary); box-shadow: var(--casa-shadow); }
.option-card__text { flex: 1; min-width: 0; }
.option-card__title { font-size: 15px; font-weight: 600; margin-bottom: 2px; }
.option-card__desc { font-size: 13px; color: var(--casa-text-2); }
.option-card ha-icon.chevron { color: var(--casa-text-2); }
.option-card[disabled] { opacity: 0.5; cursor: not-allowed; }

/* ---------- Chips & badges ---------- */
.chip {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 10px; border-radius: 999px;
  font-size: 12px; font-weight: 500; line-height: 18px;
  background: var(--casa-bg-2); color: var(--casa-text);
  white-space: nowrap;
}
.chip--app { background: color-mix(in srgb, var(--casa-primary) 14%, transparent); color: var(--casa-primary); }
.chip--ok { background: color-mix(in srgb, var(--casa-success) 14%, transparent); color: var(--casa-success); }
.chip--warn { background: color-mix(in srgb, var(--casa-warning) 20%, transparent); color: var(--casa-warning); }
.chip--error { background: color-mix(in srgb, var(--casa-error) 14%, transparent); color: var(--casa-error); }
.chip--neutral { background: var(--casa-bg-2); color: var(--casa-text-2); }

.badge {
  display: inline-block; padding: 1px 8px; border-radius: 999px;
  font-size: 11px; font-weight: 600; line-height: 16px; vertical-align: middle;
}
.badge--orphan { background: var(--casa-error); color: #fff; }
.badge--stale { background: var(--casa-warning); color: #fff; }
.badge--pending { background: var(--casa-primary); color: #fff; }
.badge--count {
  min-width: 16px; height: 16px; padding: 0 4px; border-radius: 8px;
  background: var(--casa-primary); color: #fff;
  font-size: 10px; line-height: 16px; text-align: center;
}

/* ---------- Status dot ---------- */
.status-dot {
  display: inline-block; width: 10px; height: 10px; border-radius: 50%;
  background: var(--casa-text-2);
}
.status-dot--online { background: var(--casa-success); }
.status-dot--offline { background: var(--casa-error); }
.status-dot--warn { background: var(--casa-warning); }

/* ---------- Buttons ---------- */
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  height: 36px; padding: 0 16px; border-radius: var(--casa-radius-sm);
  border: none; background: none; color: var(--casa-text);
  font: inherit; font-size: 13px; font-weight: 500; cursor: pointer;
  white-space: nowrap; transition: background 0.12s, opacity 0.12s;
}
.btn ha-icon { --mdc-icon-size: 18px; }
.btn:disabled { opacity: 0.45; cursor: not-allowed; }
.btn--primary { background: var(--casa-primary); color: var(--casa-on-primary); }
.btn--primary:hover:not(:disabled) { filter: brightness(1.08); }
.btn--outlined { border: 1px solid var(--casa-divider); background: var(--casa-card-bg); }
.btn--outlined:hover:not(:disabled) { background: var(--casa-bg-2); }
.btn--text { color: var(--casa-primary); padding: 0 10px; }
.btn--text:hover:not(:disabled) { background: color-mix(in srgb, var(--casa-primary) 8%, transparent); }
.btn--danger { background: var(--casa-error); color: #fff; }
.btn--danger-outlined { border: 1px solid var(--casa-error); color: var(--casa-error); background: transparent; }
.btn--icon {
  width: 36px; height: 36px; padding: 0; border-radius: 50%;
  color: inherit; background: none;
}
.btn--icon:hover:not(:disabled) { background: rgba(127, 127, 127, 0.15); }
.btn--icon.danger { color: var(--casa-error); }

/* ---------- Inputs ---------- */
.input, .select, .textarea {
  width: 100%; padding: 9px 12px;
  border: 1px solid var(--casa-divider); border-radius: var(--casa-radius-sm);
  background: var(--casa-card-bg); color: var(--casa-text);
  font: inherit; font-size: 13px;
}
.input:focus, .select:focus, .textarea:focus {
  outline: none; border-color: var(--casa-primary);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--casa-primary) 25%, transparent);
}
.input:disabled, .select:disabled, .textarea:disabled { opacity: 0.55; background: var(--casa-bg-2); }
.textarea { font-family: SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; min-height: 120px; resize: vertical; }

.field { margin-bottom: 14px; }
.field > label { display: block; font-size: 13px; font-weight: 500; margin-bottom: 5px; }
.field__help { font-size: 12px; color: var(--casa-text-2); margin-top: 4px; }
.field--error .input, .field--error .select { border-color: var(--casa-error); }
.field__error { font-size: 12px; color: var(--casa-error); margin-top: 4px; }
.field-row { display: flex; gap: 8px; align-items: center; }
.field-row .input { flex: 1; }

.toggle { display: flex; align-items: center; gap: 8px; font-size: 13px; cursor: pointer; margin-bottom: 10px; }
input[type="checkbox"], input[type="radio"] { accent-color: var(--casa-primary); width: 16px; height: 16px; }

.search-field { position: relative; min-width: 220px; flex: 0 1 340px; }
.search-field ha-icon {
  position: absolute; left: 12px; top: 50%; transform: translateY(-50%);
  color: var(--casa-text-2); --mdc-icon-size: 18px; pointer-events: none;
}
.search-field .input { border-radius: 24px; padding-left: 38px; }

/* ---------- Segmented control ---------- */
.segmented {
  display: inline-flex; border: 1px solid var(--casa-divider);
  border-radius: var(--casa-radius-sm); overflow: hidden; background: var(--casa-card-bg);
}
.segmented__btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  height: 34px; padding: 0 12px; border: none; background: none;
  color: var(--casa-text-2); font: inherit; font-size: 13px; cursor: pointer;
}
.segmented__btn + .segmented__btn { border-left: 1px solid var(--casa-divider); }
.segmented__btn--active {
  background: color-mix(in srgb, var(--casa-primary) 12%, transparent);
  color: var(--casa-primary);
}
.segmented__btn ha-icon { --mdc-icon-size: 18px; }

/* ---------- Tabs ---------- */
.tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--casa-divider); margin-bottom: 16px; }
.tab {
  padding: 10px 16px; border: none; background: none; cursor: pointer;
  font: inherit; font-size: 14px; font-weight: 500; color: var(--casa-text-2);
  border-bottom: 2px solid transparent; margin-bottom: -1px;
}
.tab--active { color: var(--casa-primary); border-bottom-color: var(--casa-primary); }

/* Step tabs (provision flow) — progress indicators, backward-clickable only */
.tabs--steps .tab { display: inline-flex; align-items: center; gap: 8px; }
.tabs--steps .tab:disabled { opacity: 0.45; cursor: default; }
.tabs--steps .tab--active:disabled { opacity: 1; cursor: default; }
.tab--done { color: var(--casa-text); }
.step-dot {
  width: 20px; height: 20px; border-radius: 50%; flex: none;
  display: inline-flex; align-items: center; justify-content: center;
  font-size: 11px; font-weight: 600;
  background: var(--casa-bg-2); color: var(--casa-text-2);
}
.tab--active .step-dot { background: var(--casa-primary); color: var(--casa-on-primary); }
.tab--done .step-dot { background: color-mix(in srgb, var(--casa-success) 18%, transparent); color: var(--casa-success); }

/* ---------- Table ---------- */
.table { width: 100%; border-collapse: collapse; font-size: 13px; }
.table th {
  text-align: left; padding: 10px 12px;
  font-size: 11px; font-weight: 600; letter-spacing: 0.6px; text-transform: uppercase;
  color: var(--casa-text-2); border-bottom: 1px solid var(--casa-divider);
  white-space: nowrap; user-select: none;
}
.table th.sortable { cursor: pointer; }
.table th.sortable:hover { color: var(--casa-text); }
.table th .sort-arrow { --mdc-icon-size: 14px; vertical-align: middle; transition: transform 0.15s; }
.table th .sort-arrow.desc { transform: rotate(180deg); }
.table td { padding: 10px 12px; border-bottom: 1px solid var(--casa-divider); vertical-align: middle; }
.table tbody tr:hover { background: var(--casa-bg-2); }
.table tbody tr.row--clickable { cursor: pointer; }
.table td.col-check, .table th.col-check { width: 34px; padding-right: 0; }
.table td.col-actions { white-space: nowrap; text-align: right; }
.table .row-icons { display: inline-flex; gap: 2px; }

/* ---------- List toolbar / meta / footer ---------- */
.list-toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }
.list-meta {
  display: flex; align-items: center; gap: 14px;
  font-size: 13px; color: var(--casa-text-2); margin-bottom: 10px; min-height: 36px;
}
.bulk-bar {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 6px 12px; margin-bottom: 10px;
  background: color-mix(in srgb, var(--casa-primary) 8%, transparent);
  border-radius: var(--casa-radius-sm); font-size: 13px;
}
.list-footer {
  display: flex; align-items: center; gap: 14px; justify-content: flex-end;
  padding: 12px 4px; font-size: 13px; color: var(--casa-text-2);
}
.list-footer .select { width: auto; padding: 4px 8px; }
.page-btn { width: 32px; height: 32px; }

/* ---------- Two-pane split (sessions view) ---------- */
.split { display: flex; gap: 18px; align-items: flex-start; }
.split__nav { width: 250px; flex: none; }
.split__main { flex: 1; min-width: 0; }
@media (max-width: 700px) {
  .split { flex-direction: column; }
  .split__nav { width: 100%; }
}

/* ---------- Card grid (device cards view / profile picker) ---------- */
.grid-cards {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 14px;
}

/* ---------- Popover / menu ---------- */
.popover {
  position: fixed; z-index: 10001; min-width: 200px; max-width: 340px;
  background: var(--casa-card-bg); color: var(--casa-text);
  border: 1px solid var(--casa-divider); border-radius: var(--casa-radius-sm);
  box-shadow: var(--casa-shadow-pop); padding: 6px 0; font-size: 13px;
}
.popover__section { padding: 8px 14px 4px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--casa-text-2); }
.popover .toggle { padding: 5px 14px; margin: 0; }
.popover .toggle:hover { background: var(--casa-bg-2); }
.menu__item {
  display: flex; align-items: center; gap: 10px; width: 100%;
  height: 36px; padding: 0 14px; border: none; background: none;
  color: var(--casa-text); font: inherit; font-size: 13px; cursor: pointer; text-align: left;
}
.menu__item:hover:not(:disabled) { background: var(--casa-bg-2); }
.menu__item ha-icon { --mdc-icon-size: 18px; color: var(--casa-text-2); }
.menu__item--danger, .menu__item--danger ha-icon { color: var(--casa-error); }
.menu__item:disabled { opacity: 0.45; cursor: not-allowed; }
.menu__divider { height: 1px; background: var(--casa-divider); margin: 6px 0; }

/* ---------- Modal ---------- */
.modal-overlay {
  position: fixed; inset: 0; z-index: 10002;
  background: rgba(0, 0, 0, 0.45);
  display: flex; align-items: flex-start; justify-content: center;
  padding: 8vh 16px 16px; overflow-y: auto;
}
.modal {
  background: var(--casa-card-bg); color: var(--casa-text);
  border-radius: var(--casa-radius); box-shadow: var(--casa-shadow-pop);
  width: 100%; max-width: 440px; overflow: hidden;
}
.modal--wide { max-width: 720px; }
.modal__header {
  display: flex; align-items: center; gap: 10px;
  padding: 12px 16px; background: var(--casa-header-bg); color: var(--casa-header-fg);
  font-size: 15px; font-weight: 600;
}
.modal__header .btn--icon { color: var(--casa-header-fg); width: 30px; height: 30px; }
.modal__body { padding: 18px; }
.modal__footer {
  display: flex; align-items: center; justify-content: flex-end; gap: 8px;
  padding: 12px 18px; border-top: 1px solid var(--casa-divider);
}

/* ---------- Toast ---------- */
.toast {
  position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
  z-index: 10003; max-width: 520px;
  background: var(--casa-text); color: var(--casa-bg);
  padding: 10px 18px; border-radius: var(--casa-radius-sm);
  box-shadow: var(--casa-shadow-pop); font-size: 13px;
  animation: casa-toast-in 0.18s ease-out;
}
.toast--error { background: var(--casa-error); color: #fff; }
@keyframes casa-toast-in { from { opacity: 0; transform: translate(-50%, 8px); } }

/* ---------- Empty state ---------- */
.empty-state {
  display: flex; flex-direction: column; align-items: center; gap: 10px;
  padding: 56px 16px; color: var(--casa-text-2); text-align: center;
}
.empty-state ha-icon { --mdc-icon-size: 48px; opacity: 0.6; }

/* ---------- Error bar ---------- */
.errbar {
  background: color-mix(in srgb, var(--casa-error) 12%, transparent);
  color: var(--casa-error); border: 1px solid var(--casa-error);
  border-radius: var(--casa-radius-sm); padding: 10px 14px;
  font-size: 13px; margin-bottom: 14px;
}

/* ---------- Editor (three-pane) ---------- */
.editor { display: flex; height: 100%; min-height: 0; }
.editor__nav {
  width: 270px; flex: none; border-right: 1px solid var(--casa-divider);
  background: var(--casa-card-bg); overflow-y: auto; padding: 14px 12px;
}
.editor__nav-hint { font-size: 12px; color: var(--casa-text-2); font-style: italic; margin: 2px 4px 14px; }
.editor__nav-group { font-size: 12px; font-weight: 600; color: var(--casa-text-2); margin: 14px 4px 6px; text-transform: uppercase; letter-spacing: 0.5px; }
.nav-item {
  display: flex; align-items: center; gap: 10px; width: 100%;
  padding: 10px 12px; margin-bottom: 6px;
  border: 1px solid var(--casa-divider); border-radius: var(--casa-radius-sm);
  background: var(--casa-card-bg); color: var(--casa-text);
  font: inherit; font-size: 13px; font-weight: 500; cursor: pointer; text-align: left;
}
.nav-item:hover { border-color: var(--casa-primary); }
.nav-item--active {
  border-color: var(--casa-primary);
  background: color-mix(in srgb, var(--casa-primary) 10%, transparent);
  color: var(--casa-primary);
}
.nav-item--danger { color: var(--casa-error); }
.nav-item--danger ha-icon { color: var(--casa-error); }
.nav-item ha-icon { --mdc-icon-size: 18px; color: var(--casa-text-2); }
.nav-item--active ha-icon { color: var(--casa-primary); }
.nav-item .badge { margin-left: auto; }

.editor__form { flex: 1; min-width: 0; overflow-y: auto; padding: 22px 28px; }
.editor__form h2 { margin: 0 0 6px; font-size: 20px; }
.editor__form-desc { color: var(--casa-text-2); font-size: 13px; margin: 0 0 18px; }
.editor__form hr { border: none; border-top: 1px solid var(--casa-divider); margin: 18px 0; }
.section-card { margin-bottom: 16px; }
.section-card h5 { margin: 0 0 10px; font-size: 13px; font-weight: 600; }

.editor__code {
  width: 420px; flex: none; border-left: 1px solid var(--casa-divider);
  display: flex; flex-direction: column; min-height: 0; background: var(--casa-card-bg);
}
.editor__code-header {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; border-bottom: 1px solid var(--casa-divider);
  font-size: 12px; color: var(--casa-text-2);
}
.editor__code-body { flex: 1; overflow: auto; padding: 10px 0; }
.code-line { display: flex; font-family: SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; line-height: 1.55; }
.code-line__num {
  flex: none; width: 44px; padding-right: 12px; text-align: right;
  color: var(--casa-text-2); opacity: 0.6; user-select: none;
}
.code-line__txt { white-space: pre; color: var(--casa-text); }
.code-key { color: var(--casa-primary); }
.code-str { color: var(--casa-success); }
.code-num { color: var(--casa-warning); }
.code-placeholder { color: var(--casa-text-2); font-style: italic; }

.editor__bar {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 16px; border-bottom: 1px solid var(--casa-divider);
  background: var(--casa-card-bg); font-size: 14px;
}
.editor__bar .title { font-weight: 600; }
.editor__footer {
  display: flex; align-items: center; justify-content: flex-end; gap: 10px;
  padding: 10px 16px; border-top: 1px solid var(--casa-divider);
  background: var(--casa-card-bg);
}

.danger-zone { border: 1px solid var(--casa-error); border-radius: var(--casa-radius); padding: 14px 16px; }
.danger-zone h5 { color: var(--casa-error); margin: 0 0 8px; font-size: 13px; }

/* ---------- Narrow widths ---------- */
@media (max-width: 900px) {
  .page { padding: 12px; }
  .editor__code { display: none; }
  .editor__nav { width: 220px; }
}
@media (max-width: 640px) {
  .editor { flex-direction: column; }
  .editor__nav { width: 100%; border-right: none; border-bottom: 1px solid var(--casa-divider); }
}
`;

export const cssText = CSS;

export function makeSheet() {
  try {
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(CSS);
    return sheet;
  } catch (_e) {
    return null; // caller falls back to a <style> node with cssText
  }
}
