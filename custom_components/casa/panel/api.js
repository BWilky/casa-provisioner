// Casa admin panel — data layer. Owns the admin/summary cache and the 30s poll
// loop (independent of which view is mounted), plus thin wrappers around every
// HA service and admin REST endpoint the panel uses.

export class CasaApi {
  constructor() {
    this._hass = null;
    this.summary = null;
    this.lastError = null;
    this._subs = new Set();
    this._timer = null;
    this._paused = false;
    this._intervalMs = 30000;
    this._reqSeq = 0;
  }

  setHass(hass) {
    this._hass = hass;
  }
  get hass() {
    return this._hass;
  }

  subscribe(cb) {
    this._subs.add(cb);
    return () => this._subs.delete(cb);
  }

  _notify() {
    for (const cb of this._subs) {
      try {
        cb(this.summary, this.lastError);
      } catch (e) {
        console.error("casa: summary subscriber failed", e);
      }
    }
  }

  async refreshSummary() {
    if (!this._hass) return null;
    // Only the newest in-flight request may assign the summary: a slow poll
    // response that started before a mutation must not overwrite the fresher
    // post-mutation refresh (which would resurrect deleted rows).
    const seq = ++this._reqSeq;
    let summary = null;
    let error = null;
    try {
      summary = await this._hass.callApi("GET", "casa/admin/summary");
    } catch (err) {
      error = (err && (err.message || (err.body && err.body.message) || err.error)) || "Request failed";
    }
    if (seq !== this._reqSeq) return this.summary;
    if (error === null) {
      this.summary = summary;
      this.lastError = null;
    } else {
      this.lastError = error;
    }
    this._notify();
    return this.summary;
  }

  // Optimistically remove a device from the cached summary so the UI updates
  // instantly after a delete/deprovision; the follow-up refresh reconciles.
  _dropDeviceLocal(deviceId) {
    const s = this.summary;
    if (!s || !Array.isArray(s.devices)) return;
    const dropped = s.devices.find((d) => d.device_id === deviceId);
    if (!dropped) return;
    s.devices = s.devices.filter((d) => d.device_id !== deviceId);
    if (s.stats) {
      const dec = (key, by = 1) => {
        if (typeof s.stats[key] === "number") s.stats[key] = Math.max(0, s.stats[key] - by);
      };
      dec("devices");
      if (dropped.orphaned) dec("orphaned");
      if (dropped.stale) dec("stale");
      if (dropped.pending_updates) dec("pending_updates", Number(dropped.pending_updates) || 0);
    }
    this._notify();
  }

  _dropAccountLocal(username) {
    const s = this.summary;
    if (!s) return;
    if (Array.isArray(s.accounts)) s.accounts = s.accounts.filter((a) => a.username !== username);
    if (Array.isArray(s.devices)) {
      for (const d of s.devices.filter((d) => d.username === username)) {
        this._dropDeviceLocal(d.device_id);
      }
    }
    this._notify();
  }

  startPolling(ms = 30000) {
    this._intervalMs = ms;
    this.stopPolling();
    this._timer = setInterval(() => {
      if (!this._paused) this.refreshSummary();
    }, this._intervalMs);
  }
  stopPolling() {
    if (this._timer) clearInterval(this._timer);
    this._timer = null;
  }
  pausePolling() {
    this._paused = true;
  }
  resumePolling() {
    this._paused = false;
  }

  /* ---------- HA services ---------- */

  _callWs(service, data) {
    return this._hass.callWS({
      type: "call_service",
      domain: "casa",
      service,
      service_data: data,
      return_response: true,
    });
  }
  static response(res) {
    return (res && res.response) || res || {};
  }

  provision(data) {
    return this._callWs("provision", data);
  }
  async deleteDevice(deviceId) {
    const res = await this._callWs("delete_device", { device_id: deviceId });
    this._dropDeviceLocal(deviceId);
    return res;
  }
  async deprovisionDevice(deviceId) {
    const res = await this._callWs("deprovision_device", { device_id: deviceId });
    this._dropDeviceLocal(deviceId);
    return res;
  }
  reloadDevice(deviceId) {
    return this._hass.callService("casa", "reload_device", { device_id: deviceId });
  }
  requestDeviceReport(deviceId) {
    return this._hass.callService("casa", "request_device_report", { device_id: deviceId });
  }
  notifyUser({ deviceId, username, title, message, data }) {
    const payload = { title, message };
    if (deviceId) payload.device_id = deviceId;
    if (username) payload.username = username;
    if (data) payload.data = data;
    return this._hass.callService("casa", "notify_user", payload);
  }
  createUser({ name, username, password, localOnly = true }) {
    const payload = { name, username, local_only: localOnly };
    if (password) payload.password = password;
    return this._callWs("create_user", payload);
  }
  async removeUser(username) {
    const res = await this._callWs("remove_user", { username });
    // Server also deregisters the user's devices — mirror that locally.
    this._dropAccountLocal(username);
    return res;
  }
  scrambleGuestPassword(username, deauthenticate = true) {
    return this._callWs("scramble_guest_password", { username, deauthenticate });
  }
  reconcile() {
    return this._hass.callService("casa", "reconcile", {});
  }
  regenerateSite() {
    return this._hass.callService("casa", "regenerate_site", {});
  }
  setDeviceExpiration(data) {
    return this._callWs("set_device_expiration", data);
  }

  /* ---------- admin REST ---------- */

  getWireguardProfiles() {
    return this._hass.callApi("GET", "casa/admin/wireguard_profiles");
  }
  saveWireguardProfile(profile) {
    const method = profile.id ? "PUT" : "POST";
    return this._hass.callApi(method, "casa/admin/wireguard_profiles", profile);
  }
  deleteWireguardProfile(id) {
    return this._hass.callApi("DELETE", `casa/admin/wireguard_profiles?id=${encodeURIComponent(id)}`);
  }

  // Provision templates (the REST path keeps its historical
  // "provision_profiles" name for compatibility). Templates are sparse:
  // `fields` carries only explicitly-set keys, nested under a "fields" key —
  // {id?, name, fields: {...}}.
  getProvisionTemplates() {
    return this._hass.callApi("GET", "casa/admin/provision_profiles");
  }
  saveProvisionTemplate(template) {
    // A version-skewed backend (updated on disk, not yet restarted) predates
    // the nested sparse `fields` body and would silently save an empty or
    // stale template — refuse instead; the skew banner tells the admin to
    // restart Home Assistant.
    const backend = this.summary && this.summary.version;
    if (this.panelVersion && backend && backend !== this.panelVersion) {
      return Promise.reject(new Error("Casa was updated on disk — restart Home Assistant before saving templates."));
    }
    const method = template.id ? "PUT" : "POST";
    return this._hass.callApi(method, "casa/admin/provision_profiles", template);
  }
  deleteProvisionTemplate(id) {
    return this._hass.callApi("DELETE", `casa/admin/provision_profiles?id=${encodeURIComponent(id)}`);
  }
  // One-time bulk apply of a template's set live fields to selected devices;
  // devices do not become attached to the template.
  applyTemplateToDevices({ templateId, deviceIds, sendPush = true, notifyPush = false, title = "", message = "" }) {
    return this.queueUpdate({
      update_type: "profile",
      action: "update",
      profile_id: templateId,
      device_ids: deviceIds,
      send_update_push: sendPush,
      notify_push: notifyPush,
      title,
      message,
    });
  }

  checkUsername(username, name = "") {
    const q = `username=${encodeURIComponent(username)}&name=${encodeURIComponent(name)}`;
    return this._hass.callApi("GET", `casa/admin/check_username?${q}`);
  }

  // Presence-conditional: pass only the keys you want to change
  // ({device_id, alias?, expires_at_override?}).
  updateDevice(body) {
    return this._hass.callApi("PUT", "casa/admin/device", body);
  }
  // "Force Device Changes": one-time direct stamp of live fields onto a
  // single device (see CasaAdminDeviceView.put's provisioning_fields branch).
  updateDeviceProvisioning(deviceId, fields) {
    return this.updateDevice({ device_id: deviceId, provisioning_fields: fields });
  }
  queueUpdate(req) {
    return this._hass.callApi("POST", "casa/admin/queue_update", req);
  }
  // Reauthenticate a device with a new username/password
  // ({device_id, user_id? | username? | create_user: {name, username},
  //   password?, scramble_old?, send_update_push?}).
  reauthDevice(body) {
    // A version-skewed backend (updated on disk, not yet restarted) has no
    // reauth_device route and would 404 confusingly — refuse instead; the
    // skew banner tells the admin to restart Home Assistant.
    const backend = this.summary && this.summary.version;
    if (this.panelVersion && backend && backend !== this.panelVersion) {
      return Promise.reject(new Error("Casa was updated on disk — restart Home Assistant before reauthenticating devices."));
    }
    return this._hass.callApi("POST", "casa/admin/reauth_device", body);
  }
  deleteQueuedUpdate(deviceId, updateId) {
    return this._hass.callApi(
      "DELETE",
      `casa/admin/queue_update?device_id=${encodeURIComponent(deviceId)}&id=${encodeURIComponent(updateId)}`
    );
  }
  regenerateDeviceKey() {
    return this._hass.callApi("POST", "casa/admin/regenerate_device_key");
  }
  getSessions() {
    return this._hass.callApi("GET", "casa/admin/sessions");
  }
  revokeSession(userId, tokenId) {
    return this._hass.callApi(
      "DELETE",
      `casa/admin/sessions?user_id=${encodeURIComponent(userId)}&token_id=${encodeURIComponent(tokenId)}`
    );
  }
  // Presence-conditional: pass only the keys you want to change
  // ({require_device_alias?}).
  updateSettings(body) {
    return this._hass.callApi("PUT", "casa/admin/settings", body);
  }
}
