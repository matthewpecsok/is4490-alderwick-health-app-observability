/* Alderwick website chat assistant. Talks to public.views.chat (POST JSON). */
(function () {
  const root = document.getElementById("chat-widget");
  if (!root) return;
  const launcher = root.querySelector(".chat-launcher");
  const panel = root.querySelector(".chat-panel");
  const log = root.querySelector(".chat-log");
  const form = root.querySelector(".chat-form");
  const input = form.querySelector("input[name=message]");
  const button = form.querySelector("button[type=submit]");
  const csrf = form.querySelector("input[name=csrfmiddlewaretoken]").value;
  const greeting = log.innerHTML;

  function setOpen(open) {
    panel.hidden = !open;
    launcher.setAttribute("aria-expanded", String(open));
    if (open) input.focus();
  }

  function addMessage(role, text) {
    const div = document.createElement("div");
    div.className = "chat-msg chat-msg-" + role;
    div.textContent = text; // textContent, never innerHTML: replies are untrusted text
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
    return div;
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
      body: JSON.stringify(body || {}),
      credentials: "same-origin",
    });
  }

  launcher.addEventListener("click", () => setOpen(panel.hidden));
  root.querySelector("[data-chat-close]").addEventListener("click", () => setOpen(false));
  root.querySelector("[data-chat-reset]").addEventListener("click", async () => {
    await post(root.dataset.resetUrl);
    log.innerHTML = greeting;
    input.focus();
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = input.value.trim();
    if (!message) return;
    addMessage("user", message);
    input.value = "";
    button.disabled = true;
    const pending = addMessage("model pending", "Thinking...");
    try {
      const response = await post(root.dataset.chatUrl, { message });
      const data = await response.json();
      pending.remove();
      addMessage(data.reply ? "model" : "error", data.reply || data.error || "Something went wrong.");
    } catch (err) {
      pending.remove();
      addMessage("error", "Couldn't reach the assistant. Please try again.");
    } finally {
      button.disabled = false;
      input.focus();
    }
  });
})();
