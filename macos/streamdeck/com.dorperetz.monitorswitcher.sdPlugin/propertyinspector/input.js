/* global connectElgatoStreamDeckSocket */
(() => {
  let websocket = null;
  let uuid = null;
  let actionUUID = null;
  let settings = {};

  const select = document.getElementById("target");

  function send(event, payload) {
    if (!websocket || websocket.readyState !== WebSocket.OPEN) return;
    websocket.send(JSON.stringify({ event, context: uuid, payload }));
  }

  function saveSettings() {
    send("setSettings", settings);
  }

  function renderTargets(targets) {
    select.innerHTML = "";

    if (!targets.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "No monitors configured yet";
      select.appendChild(option);
      select.disabled = true;
      return;
    }

    select.disabled = false;
    for (const target of targets) {
      const option = document.createElement("option");
      option.value = target.id;
      option.textContent = target.label;
      select.appendChild(option);
    }

    // An unconfigured key defaults to the first monitor, matching the plugin.
    const known = targets.some((target) => target.id === settings.target);
    if (!known) {
      settings.target = targets[0].id;
      saveSettings();
    }
    select.value = settings.target;
  }

  select.addEventListener("change", () => {
    settings.target = select.value;
    saveSettings();
  });

  window.connectElgatoStreamDeckSocket = function (
    inPort,
    inUUID,
    inRegisterEvent,
    inInfo,
    inActionInfo,
  ) {
    uuid = inUUID;
    const actionInfo = JSON.parse(inActionInfo);
    actionUUID = actionInfo.action;
    settings = actionInfo.payload.settings || {};

    websocket = new WebSocket(`ws://127.0.0.1:${inPort}`);

    websocket.onopen = () => {
      websocket.send(JSON.stringify({ event: inRegisterEvent, uuid }));
      websocket.send(
        JSON.stringify({
          event: "sendToPlugin",
          action: actionUUID,
          context: uuid,
          payload: { event: "getTargets" },
        }),
      );
    };

    websocket.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.event === "didReceiveSettings") {
        settings = message.payload.settings || {};
        select.value = settings.target || "";
        return;
      }
      if (message.event === "sendToPropertyInspector") {
        const payload = message.payload || {};
        if (payload.event === "targets") {
          renderTargets(payload.targets || []);
        }
      }
    };
  };
})();
