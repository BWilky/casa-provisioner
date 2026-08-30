// Casa panel version — MUST match CASA_VERSION in custom_components/casa/const.py
// (bump both together on every release). The backend reports the version it had
// at import time in /api/casa/admin/summary; app.js compares it against this
// on-disk constant and shows a "restart Home Assistant" banner on any skew —
// the telltale of updated files under a still-running old Python process.
export const PANEL_VERSION = "26.07.21";
