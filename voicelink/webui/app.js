const $ = (selector) => document.querySelector(selector);

const ui = {
  loginShell: $("#loginShell"), appShell: $("#appShell"), loginForm: $("#loginForm"),
  adminPassword: $("#adminPassword"), loginError: $("#loginError"), togglePassword: $("#togglePassword"),
  logout: $("#logoutButton"), botAvatar: $("#botAvatar"), botName: $("#botName"),
  botMeta: $("#botMeta"), botId: $("#botId"), guildCount: $("#guildCount"),
  guild: $("#guildSelect"), channel: $("#channelSelect"), connect: $("#connectButton"),
  discordStatus: $("#discordStatus"), lavalinkStatus: $("#lavalinkStatus"), mongoStatus: $("#mongoStatus"),
  discordValue: $("#discordValue"), lavalinkValue: $("#lavalinkValue"), mongoValue: $("#mongoValue"),
  latency: $("#latencyValue"), playerStatus: $("#playerStatus"), playerValue: $("#playerValue"),
  listeners: $("#listenerValue"), channelName: $("#channelName"), disconnect: $("#disconnectButton"),
  cover: $("#coverImage"), coverPlaceholder: $("#coverPlaceholder"), playingRing: $("#playingRing"),
  trackSource: $("#trackSource"), trackTitle: $("#trackTitle"), trackAuthor: $("#trackAuthor"),
  position: $("#positionRange"), currentTime: $("#currentTime"), totalTime: $("#totalTime"),
  shuffle: $("#shuffleButton"), previous: $("#previousButton"), pause: $("#pauseButton"),
  skip: $("#skipButton"), repeat: $("#repeatButton"), repeatMini: $("#repeatMiniButton"),
  queueCount: $("#queueCount"), queueList: $("#queueList"), clear: $("#clearButton"),
  playForm: $("#playForm"), play: $("#playButton"), search: $("#searchInput"), channelHint: $("#channelHint"),
  channelHintText: $("#channelHintText"),
  volume: $("#volumeRange"), volumeValue: $("#volumeValue"), autoplay: $("#autoplayToggle"),
  repeatMode: $("#repeatMode"), audioModes: [...document.querySelectorAll("[data-audio-mode]")],
  audioModeHint: $("#audioModeHint"), toast: $("#toast"),
};

let adminPassword = sessionStorage.getItem("vocard.adminPassword") || "";
let snapshot = null;
let pollTimer = null;
let toastTimer = null;

function notify(message, isError = false) {
  clearTimeout(toastTimer);
  ui.toast.querySelector("p").textContent = message;
  ui.toast.querySelector("span").textContent = isError ? "!" : "✓";
  ui.toast.classList.remove("hidden");
  toastTimer = setTimeout(() => ui.toast.classList.add("hidden"), 4500);
}

function lock(message = "") {
  clearInterval(pollTimer);
  pollTimer = null;
  snapshot = null;
  adminPassword = "";
  sessionStorage.removeItem("vocard.adminPassword");
  ui.appShell.classList.add("hidden");
  ui.loginShell.classList.remove("hidden");
  ui.loginError.textContent = message;
  ui.adminPassword.value = "";
  ui.adminPassword.focus();
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      Authorization: `Bearer ${adminPassword}`,
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  const payload = await response.json().catch(() => ({ message: "Invalid server response." }));
  if (response.status === 401) {
    lock("รหัสผ่าน Admin ไม่ถูกต้อง");
    throw new Error("รหัสผ่าน Admin ไม่ถูกต้อง");
  }
  if (!response.ok) throw new Error(payload.message || `Request failed (${response.status})`);
  return payload;
}

function optionList(select, items, selected, emptyLabel) {
  const previous = selected || select.value;
  select.replaceChildren();
  if (!items.length) select.add(new Option(emptyLabel, ""));
  for (const item of items) select.add(new Option(item.label || item.name, item.id));
  if ([...select.options].some((option) => option.value === previous)) select.value = previous;
}

function renderChannelAvailability(guild) {
  const channel = guild?.voiceChannels.find((item) => item.id === ui.channel.value);
  const connected = channel?.status === "connected";
  ui.channelHint.classList.toggle("available", Boolean(channel && !connected));
  ui.channelHint.classList.toggle("connected", connected);
  ui.connect.disabled = !channel || connected;
  ui.connect.textContent = connected ? "CONNECTED ✓" : "CONNECT ↗";

  if (!channel) {
    ui.channelHintText.textContent = "เลือก Voice channel ก่อนเปิดเพลง";
  } else if (connected) {
    ui.channelHintText.textContent = `เชื่อมต่อแล้ว · บอทอยู่ในห้องนี้ · ${channel.listeners} คนฟัง`;
  } else if (channel.status === "empty") {
    ui.channelHintText.textContent = "ยังไม่ได้เชื่อมต่อกับห้องนี้ · ห้องว่างและพร้อมใช้งาน";
  } else {
    ui.channelHintText.textContent = `ยังไม่ได้เชื่อมต่อกับห้องนี้ · มี ${channel.listeners} คนในห้อง`;
  }
}

function serviceStatus(indicator, value, ready) {
  indicator.classList.toggle("ok", Boolean(ready));
  value.textContent = ready ? "Ready" : "Starting";
}

function formatTime(milliseconds) {
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return "00:00";
  const seconds = Math.floor(milliseconds / 1000);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}` : `${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}

function selectedGuild() {
  return snapshot?.guilds.find((guild) => guild.id === ui.guild.value) || null;
}

function makeQueueRow(track, index) {
  const row = document.createElement("div");
  row.className = "queue-row";

  const number = document.createElement("span");
  number.className = "queue-number";
  number.textContent = String(index + 1).padStart(2, "0");

  let artwork;
  if (track.thumbnail) {
    artwork = document.createElement("img");
    artwork.src = track.thumbnail;
    artwork.alt = "";
  } else {
    artwork = document.createElement("div");
    artwork.textContent = "♫";
  }
  artwork.className = "queue-art";

  const copy = document.createElement("div");
  const title = document.createElement("strong");
  const author = document.createElement("small");
  title.textContent = track.title;
  author.textContent = track.author;
  copy.append(title, author);

  const duration = document.createElement("span");
  duration.className = "queue-duration";
  duration.textContent = track.isStream ? "LIVE" : formatTime(track.length);

  const remove = document.createElement("button");
  remove.type = "button";
  remove.textContent = "×";
  remove.setAttribute("aria-label", `Remove ${track.title}`);
  remove.addEventListener("click", () => action("remove", { index: index + 1 }));

  row.append(number, artwork, copy, duration, remove);
  return row;
}

function render(state) {
  snapshot = state;
  const { bot, services, guilds, player } = state;
  const desiredGuild = ui.guild.value || state.selectedGuildId;

  ui.botName.textContent = bot.name;
  ui.botAvatar.src = bot.avatarUrl;
  ui.botAvatar.classList.toggle("hidden", !bot.avatarUrl);
  ui.botMeta.textContent = `${bot.latencyMs} ms · ${bot.guildCount} servers`;
  ui.botId.textContent = bot.id;
  ui.guildCount.textContent = bot.guildCount;
  document.title = `${bot.name} · Vocard Control`;

  optionList(ui.guild, guilds, desiredGuild, "No Discord servers");
  const guild = selectedGuild();
  const voiceChannels = guild?.voiceChannels || [];
  const selectedChannelId = voiceChannels.some((channel) => channel.id === ui.channel.value)
    ? ui.channel.value
    : player?.channelId || guild?.playerChannelId;
  optionList(ui.channel, voiceChannels, selectedChannelId, "No connectable channels");
  ui.play.disabled = !ui.channel.value;
  renderChannelAvailability(guild);

  serviceStatus(ui.discordStatus, ui.discordValue, services.discord);
  serviceStatus(ui.lavalinkStatus, ui.lavalinkValue, services.lavalink);
  serviceStatus(ui.mongoStatus, ui.mongoValue, services.mongodb);
  ui.latency.textContent = `${bot.latencyMs} ms`;
  ui.playerStatus.classList.toggle("ok", Boolean(player));
  ui.playerValue.textContent = player ? (player.isPaused ? "Paused" : "Active") : "Idle";
  ui.listeners.textContent = `${player?.listeners || 0} listeners`;

  const current = player?.current;
  ui.channelName.textContent = player?.channelName || "ยังไม่ได้เชื่อม Voice";
  ui.disconnect.disabled = !player;
  ui.trackSource.textContent = current?.source || "VOCARD READY";
  ui.trackTitle.textContent = current?.title || "พร้อมรับคำสั่งเพลง";
  ui.trackAuthor.textContent = current?.author || "เลือกห้องเสียงและค้นหาเพลงเพื่อเริ่มเล่น";
  ui.cover.classList.toggle("hidden", !current?.thumbnail);
  ui.coverPlaceholder.classList.toggle("hidden", Boolean(current?.thumbnail));
  if (current?.thumbnail) ui.cover.src = current.thumbnail;
  ui.playingRing.classList.toggle("paused", !player || player.isPaused || !player.isPlaying);

  ui.position.disabled = !current || current.isStream;
  ui.position.max = Math.max(current?.length || 1, 1);
  if (document.activeElement !== ui.position) ui.position.value = Math.min(player?.position || 0, current?.length || 1);
  ui.currentTime.textContent = formatTime(player?.position || 0);
  ui.totalTime.textContent = current?.isStream ? "LIVE" : formatTime(current?.length || 0);

  const capabilities = player?.capabilities || {};
  ui.pause.disabled = !capabilities.canPause;
  ui.skip.disabled = !capabilities.canSkip;
  ui.previous.disabled = !capabilities.canPrevious;
  ui.shuffle.disabled = !capabilities.canShuffle;
  ui.clear.disabled = !capabilities.canClear;
  ui.repeat.disabled = !player;
  ui.repeatMini.disabled = !player;
  ui.autoplay.disabled = !player;
  ui.volume.disabled = !player;
  ui.pause.textContent = player?.isPaused ? "▶" : "Ⅱ";
  ui.repeat.classList.toggle("active", Boolean(player && player.repeatMode !== "off"));
  ui.repeatMode.textContent = player?.repeatMode || "off";
  ui.autoplay.classList.toggle("on", Boolean(player?.autoplay));
  ui.autoplay.setAttribute("aria-pressed", String(Boolean(player?.autoplay)));
  if (document.activeElement !== ui.volume) ui.volume.value = player?.volume ?? 100;
  ui.volumeValue.textContent = `${ui.volume.value}%`;

  const queue = player?.queue || [];
  ui.queueCount.textContent = queue.length;
  ui.queueList.replaceChildren();
  if (queue.length) queue.forEach((track, index) => ui.queueList.append(makeQueueRow(track, index)));
  else {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    const icon = document.createElement("span");
    const title = document.createElement("strong");
    const copy = document.createElement("p");
    icon.textContent = "◎"; title.textContent = "คิวยังว่าง"; copy.textContent = "เพลงที่เพิ่มจะแสดงตรงนี้";
    empty.append(icon, title, copy); ui.queueList.append(empty);
  }

  for (const button of ui.audioModes) {
    const active = Boolean(player && button.dataset.audioMode === player.audioMode);
    button.disabled = !player;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  }
  const activeMode = player?.audioMode || "normal";
  ui.audioModeHint.textContent = player
    ? `โหมดปัจจุบัน: ${activeMode === "custom" ? "Custom (ตั้งจาก Discord)" : activeMode.toUpperCase()}`
    : "เชื่อมต่อ Voice เพื่อเลือกโหมดเสียง";
}

async function loadState(silent = false) {
  try {
    const query = ui.guild.value ? `?guildId=${encodeURIComponent(ui.guild.value)}` : "";
    render(await api(`/api/state${query}`));
    return true;
  } catch (error) {
    if (!silent && adminPassword) notify(error.message, true);
    return false;
  }
}

async function unlock(password) {
  adminPassword = password.trim();
  if (!adminPassword) return;
  ui.loginError.textContent = "";
  const ok = await loadState(true);
  if (!ok) {
    if (adminPassword) ui.loginError.textContent = "เชื่อมต่อไม่ได้ ตรวจสอบ ADMIN_PASSWORD หรือ WEB_DASHBOARD_KEY อีกครั้ง";
    return;
  }
  sessionStorage.setItem("vocard.adminPassword", adminPassword);
  ui.loginShell.classList.add("hidden");
  ui.appShell.classList.remove("hidden");
  clearInterval(pollTimer);
  pollTimer = setInterval(() => loadState(true), 3000);
}

async function action(name, extra = {}) {
  if (!ui.guild.value) {
    notify("เลือก Discord server ก่อน", true);
    return false;
  }
  try {
    const payload = await api("/api/action", {
      method: "POST",
      body: JSON.stringify({ action: name, guildId: ui.guild.value, ...extra }),
    });
    if (payload.state) render(payload.state);
    notify(payload.message || "สำเร็จ");
    return true;
  } catch (error) {
    notify(error.message, true);
    return false;
  }
}

ui.loginForm.addEventListener("submit", (event) => { event.preventDefault(); unlock(ui.adminPassword.value); });
ui.togglePassword.addEventListener("click", () => {
  const visible = ui.adminPassword.type === "text";
  ui.adminPassword.type = visible ? "password" : "text";
  ui.togglePassword.textContent = visible ? "SHOW" : "HIDE";
});
ui.logout.addEventListener("click", () => lock());
ui.guild.addEventListener("change", () => loadState());
ui.channel.addEventListener("change", () => {
  ui.play.disabled = !ui.channel.value;
  renderChannelAvailability(selectedGuild());
});
ui.connect.addEventListener("click", () => action("connect", { voiceChannelId: ui.channel.value }));
ui.disconnect.addEventListener("click", () => action("disconnect"));
ui.playForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = ui.search.value.trim();
  if (!query) return;
  if (await action("play", { query, voiceChannelId: ui.channel.value })) ui.search.value = "";
});
ui.pause.addEventListener("click", () => action("pause", { pause: !snapshot?.player?.isPaused }));
ui.skip.addEventListener("click", () => action("skip"));
ui.previous.addEventListener("click", () => action("previous"));
ui.repeat.addEventListener("click", () => action("repeat"));
ui.repeatMini.addEventListener("click", () => action("repeat"));
ui.shuffle.addEventListener("click", () => action("shuffle"));
ui.clear.addEventListener("click", () => action("clear"));
ui.autoplay.addEventListener("click", () => action("autoplay", { enabled: !snapshot?.player?.autoplay }));
ui.position.addEventListener("change", () => action("seek", { position: Number(ui.position.value) }));
ui.volume.addEventListener("input", () => { ui.volumeValue.textContent = `${ui.volume.value}%`; });
ui.volume.addEventListener("change", () => action("volume", { volume: Number(ui.volume.value) }));
for (const button of ui.audioModes) {
  button.addEventListener("click", () => action("audio_mode", { mode: button.dataset.audioMode }));
}
ui.toast.querySelector("button").addEventListener("click", () => ui.toast.classList.add("hidden"));

if (adminPassword) unlock(adminPassword);
else ui.adminPassword.focus();
