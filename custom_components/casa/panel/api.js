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
    try {
      this.summary = await this._hass.callApi("GET", "casa/admin/summary");
      this.lastError = null;
    } catch (err) {
      this.lastError = (err && err.message) || String(err);
    }
    this._notify();
    return this.summary;
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
  deleteDevice(deviceId) {
    return this._callWs("delete_device", { device_id: deviceId });
  }
  deprovisionDevice(deviceId) {
    return this._callWs("deprovision_device", { device_id: deviceId });
  }
  reloadDevice(deviceId) {
    return this._hass.callService("casa", "reload_device", { device_id: deviceId });
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
  removeUser(username) {
    return this._callWs("remove_user", { username });
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

  getProvisionProfiles() {
    return this._hass.callApi("GET", "casa/admin/provision_profiles");
  }
  saveProvisionProfile(profile) {
    const method = profile.id ? "PUT" : "POST";
    return this._hass.callApi(method, "casa/admin/provision_profiles", profile);
  }
  deleteProvisionProfile(id) {
    return this._hass.callApi("DELETE", `casa/admin/provision_profiles?id=${encodeURIComponent(id)}`);
  }

  // Presence-conditional: pass only the keys you want to change
  // ({device_id, alias?, expires_at_override?}).
  updateDevice(body) {
    return this._hass.callApi("PUT", "casa/admin/device", body);
  }
  queueUpdate(req) {
    return this._hass.callApi("POST", "casa/admin/queue_update", req);
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
}
