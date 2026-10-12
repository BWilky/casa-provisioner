// Optional `data` for a test push, plus the actionable-notification example
// the admin can insert. Pure; shared by the devices-list modal and the
// device editor's Push section.
export const ACTIONS_EXAMPLE = {
  tag: "test",
  actions: [
    { action: "VIEW", title: "Open dashboard", nav_path: "/lovelace/0" },
    { action: "RUN", title: "Run script", service: "script.turn_on", target: { entity_id: "script.example" } },
    { action: "IGNORE", title: "Ignore", destructive: true },
  ],
};

export function actionsExampleText() {
  return JSON.stringify(ACTIONS_EXAMPLE, null, 2);
}

export function parsePushData(text) {
  const trimmed = String(text ?? "").trim();
  if (!trimmed) return { data: undefined };
  let parsed;
  try {
    parsed = JSON.parse(trimmed);
  } catch (e) {
    return { error: "Data must be valid JSON: " + e.message };
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { error: "Data must be a JSON object." };
  }
  return { data: parsed };
}

export const PUSH_DATA_HELP =
  "Optional. Button presses arrive in Home Assistant as the casa_notification_action event (Developer Tools → Events).";
