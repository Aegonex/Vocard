(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const STORE = { password: "vocard.admin", guild: "vocard.guild", user: "vocard.userId" };

  let password = localStorage.getItem(STORE.password) || "";
  let state = null;
  let selectedGuildId = localStorage.getItem(STORE.guild) || "";
  let pollTimer = null;
  let seeking = false;
  let volumeDragging = false;
  let positionBase = 0;
  let positionStamp = 0;
  let playlistUserId = localStorage.getItem(STORE.user) || "";
  let playlistData = null;
  let selectedPlaylistId = null;

  const SETTING_STATE_KEYS = {
    play_forever: "playForever",
    bypass_vote: "bypassVote",
    controller: "controller",
    controller_msg: "controllerMsg",
    duplicate_track: "duplicateTrack",
    silent_msg: "silentMsg",
  };
  const CHANNEL_STATUS = {
    connected: "กำลังเล่นเพลงอยู่ห้องนี้",
    empty: "ว่าง",
    available: "มีคนฟังอยู่",
  };
  const REPEAT_NEXT = { off: "track", track: "queue", queue: "off" };
  const REPEAT_LABEL = { off: "ปิด", track: "เพลงนี้", queue: "ทั้งคิว" };

  const ICON_X = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>';
  const ICON_PLAY = '<svg viewBox="0 0 24 24" fill="currentColor"><polygon points="6 4 20 12 6 20 6 4"/></svg>';

  // ---------------------------------------------------------------- utilities

  function esc(value) {
    return String(value == null ? "" : value)
      .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
  }

  function fmt(ms) {
    if (!Number.isFinite(ms) || ms < 0) return "00:00";
    const total = Math.floor(ms / 1000);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    const mm = String(m).padStart(2, "0");
    const ss = String(s).padStart(2, "0");
    return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
  }

  function paint(input) {
    const min = Number(input.min) || 0;
    const max = Number(input.max) || 100;
    const ratio = max > min ? ((Number(input.value) - min) / (max - min)) * 100 : 0;
    input.style.setProperty("--fill", `${ratio}%`);
  }

  // Swap a button's label for a spinner and disable it while a request is in
  // flight, so the user sees progress and cannot double-submit.
  function setLoading(el, on) {
    if (!el) return;
    if (on) {
      if (el.dataset.loading) return;
      el.dataset.loading = "1";
      el.style.minWidth = `${el.offsetWidth}px`;
      el._label = el.innerHTML;
      el.innerHTML = '<i class="spin"></i>';
      el.disabled = true;
    } else {
      if (!el.dataset.loading) return;
      delete el.dataset.loading;
      el.style.minWidth = "";
      if (el._label != null) { el.innerHTML = el._label; el._label = null; }
      el.disabled = false;
    }
  }

  let toastTimer = null;
  function toast(message, isError = false) {
    const el = $("toast");
    el.textContent = message;
    el.classList.toggle("error", isError);
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
  }

  function headers(extra) {
    return Object.assign({ Authorization: `Bearer ${password}` }, extra || {});
  }

  function currentGuild() {
    if (!state) return null;
    return state.guilds.find((g) => g.id === state.selectedGuildId) || null;
  }

  // ---------------------------------------------------------------- api layer

  async function fetchState() {
    const query = selectedGuildId ? `?guildId=${encodeURIComponent(selectedGuildId)}` : "";
    const response = await fetch(`/api/state${query}`, { headers: headers() });
    const payload = await response.json().catch(() => ({}));
    if (response.status === 401) throw { auth: true };
    if (!response.ok || !payload.ok) throw new Error(payload.message || "โหลดสถานะไม่สำเร็จ");
    return payload;
  }

  async function doAction(body, { silent = false } = {}) {
    if (!state || !state.selectedGuildId) {
      toast("ยังไม่ได้เลือกเซิร์ฟเวอร์", true);
      return null;
    }
    body.guildId = state.selectedGuildId;
    try {
      const response = await fetch("/api/action", {
        method: "POST",
        headers: headers({ "Content-Type": "application/json" }),
        body: JSON.stringify(body),
      });
      const payload = await response.json().catch(() => ({}));
      if (response.status === 401) { lock(); return null; }
      if (!response.ok || !payload.ok) {
        toast(payload.message || "ทำรายการไม่สำเร็จ", true);
        return null;
      }
      if (payload.state) { state = payload.state; render(); }
      if (!silent && payload.message) toast(payload.message);
      return payload;
    } catch (err) {
      toast("เชื่อมต่อเซิร์ฟเวอร์ไม่ได้", true);
      return null;
    }
  }

  // ---------------------------------------------------------------- rendering

  function render() {
    if (!state) return;
    renderBot();
    renderToolbar();
    renderPlayerPanel();
    renderBar();
    renderEffects();
    renderSettings();
  }

  function renderBot() {
    const services = state.services || {};
    const healthy = services.status === "ok";
    $("botName").textContent = state.bot.name;
    const mode = state.commandsEnabled ? "" : " · เว็บเท่านั้น";
    $("botMeta").textContent = `${state.bot.latencyMs} ms · ${state.bot.guildCount} เซิร์ฟเวอร์${mode}`;
    $("botDot").classList.toggle("ok", healthy);
    const avatar = $("botAvatar");
    if (state.bot.avatarUrl && avatar.src !== state.bot.avatarUrl) avatar.src = state.bot.avatarUrl;
  }

  function renderToolbar() {
    const guildSelect = $("guildSelect");
    if (document.activeElement !== guildSelect) {
      guildSelect.innerHTML = state.guilds.map((g) =>
        `<option value="${esc(g.id)}"${g.id === state.selectedGuildId ? " selected" : ""}>${esc(g.name)}</option>`
      ).join("");
    }

    const guild = currentGuild();
    const channelSelect = $("channelSelect");
    if (document.activeElement !== channelSelect) {
      const previous = channelSelect.value;
      const options = ['<option value="">เลือกห้องที่ต้องการเชื่อมต่อ</option>'];
      for (const channel of (guild ? guild.voiceChannels : [])) {
        const status = channel.status === "available"
          ? `มีคนฟัง ${channel.listeners} คน`
          : CHANNEL_STATUS[channel.status] || "";
        const kind = channel.kind === "stage" ? "STAGE" : "VOICE";
        options.push(
          `<option value="${esc(channel.id)}">${esc(channel.name)} [${kind}] - ${esc(status)}</option>`
        );
      }
      channelSelect.innerHTML = options.join("");
      const keep = guild && guild.voiceChannels.some((c) => c.id === previous);
      channelSelect.value = keep ? previous
        : (guild && guild.playerChannelId ? guild.playerChannelId : "");
    }
  }

  function trackRow(track, index, action) {
    const requester = track.requester ? `ขอโดย ${track.requester}` : "";
    const button = action === "remove"
      ? `<button class="act" data-remove="${index + 1}" title="ลบออกจากคิว">${ICON_X}</button>`
      : `<button class="act play" data-replay="${esc(track.id)}" title="เล่นอีกครั้ง">${ICON_PLAY}</button>`;
    return `<div class="q-row">
      <span class="idx">${index + 1}</span>
      <div class="t"><strong>${esc(track.title)} - ${esc(track.author)}</strong><small>${esc(requester)}</small></div>
      <span class="dur">${track.isStream ? "LIVE" : fmt(track.length)}</span>
      ${button}
    </div>`;
  }

  function renderPlayerPanel() {
    const player = state.player;
    const guild = currentGuild();
    const where = $("whereLine").parentElement;

    if (player && player.channelName) {
      $("whereLine").textContent =
        `NOW PLAYING · ${player.channelName.toUpperCase()} · ผู้ฟัง ${player.listeners} คน`;
      where.classList.add("live");
    } else {
      $("whereLine").textContent = guild ? `${guild.name} · ยังไม่ได้เชื่อมต่อห้องเสียง` : "ยังไม่ได้เชื่อมต่อห้องเสียง";
      where.classList.remove("live");
    }
    $("disconnectButton").disabled = !player;

    const current = player && player.current;
    const cover = $("coverImage");
    if (current && current.thumbnail) {
      if (cover.src !== current.thumbnail) cover.src = current.thumbnail;
      cover.classList.remove("hidden");
      $("coverIcon").classList.add("hidden");
    } else {
      cover.classList.add("hidden");
      $("coverIcon").classList.remove("hidden");
    }
    if (current) {
      $("trackKicker").textContent = `${current.source} · ขอโดย ${current.requester}`.toUpperCase();
      $("trackTitle").textContent = current.title;
      $("trackAuthor").textContent = current.author;
    } else {
      $("trackKicker").textContent = "VOCARD READY";
      $("trackTitle").textContent = player ? "ยังไม่มีเพลงที่กำลังเล่น" : "พร้อมรับคำสั่งเพลง";
      $("trackAuthor").textContent = player
        ? "เพิ่มเพลงจากช่องค้นหาด้านขวา" : "เลือกห้องเสียงและกดเชื่อมต่อเพื่อเริ่มเล่น";
    }

    const queue = player ? player.queue : [];
    const history = player ? player.history : [];
    $("tagQueue").textContent = queue.length;
    $("tagRepeat").textContent = player ? (REPEAT_LABEL[player.repeatMode] || player.repeatMode) : "ปิด";
    $("tagMode").textContent = player
      ? (player.audioMode === "custom" ? "Custom" : (player.audioMode || "normal")) : "Normal";

    const totalMs = queue.reduce((sum, t) => sum + (t.isStream ? 0 : t.length || 0), 0);
    $("queueSummary").textContent = queue.length
      ? `${queue.length} เพลง · ${fmt(totalMs)}` : "0 เพลง";
    $("queueList").innerHTML = queue.length
      ? queue.map((t, i) => trackRow(t, i, "remove")).join("")
      : '<div class="empty-note">คิวยังว่าง<br>พิมพ์ชื่อเพลงด้านบนเพื่อเพิ่มเพลง</div>';

    $("historyCount").textContent = player && player.historyCount ? `${player.historyCount} เพลง` : "";
    $("historyList").innerHTML = history.length
      ? history.map((t, i) => trackRow(t, i, "replay")).join("")
      : '<div class="empty-note">ยังไม่มีประวัติการเล่น</div>';

    $("clearQueueButton").disabled = !player || !queue.length;
  }

  function renderBar() {
    const player = state.player;
    const current = player && player.current;
    const capabilities = (player && player.capabilities) || {};

    $("barTitle").textContent = current ? current.title : "ไม่มีเพลงที่กำลังเล่น";
    $("barAuthor").textContent = current ? current.author : state.bot.name;
    const thumb = $("barThumbImage");
    if (current && current.thumbnail) {
      if (thumb.src !== current.thumbnail) thumb.src = current.thumbnail;
      thumb.classList.remove("hidden");
      $("barThumbIcon").classList.add("hidden");
    } else {
      thumb.classList.add("hidden");
      $("barThumbIcon").classList.remove("hidden");
    }

    $("pauseButton").disabled = !capabilities.canPause;
    $("skipButton").disabled = !capabilities.canSkip;
    $("previousButton").disabled = !capabilities.canPrevious;
    $("shuffleButton").disabled = !capabilities.canShuffle;
    $("shuffleButton2").disabled = !capabilities.canShuffle;
    $("repeatButton").disabled = !player;
    $("autoplayToggle").disabled = !player;
    $("volumeRange").disabled = !player;
    $("seekRange").disabled = !capabilities.canSeek;

    const paused = player ? player.isPaused : false;
    $("iconPause").classList.toggle("hidden", paused || !current);
    $("iconPlay").classList.toggle("hidden", !(paused || !current));

    const repeat = player ? player.repeatMode : "off";
    $("repeatButton").classList.toggle("on", repeat !== "off");
    $("repeatBadge").classList.toggle("hidden", repeat !== "track");

    $("autoplayToggle").classList.toggle("on", Boolean(player && player.autoplay));

    if (!volumeDragging && player) {
      $("volumeRange").value = player.volume;
      $("volumeLabel").textContent = `${player.volume}%`;
      paint($("volumeRange"));
    }

    positionBase = player ? player.position : 0;
    positionStamp = Date.now();
    updateSeekUI();
  }

  function updateSeekUI() {
    const player = state && state.player;
    const current = player && player.current;
    const seek = $("seekRange");
    if (!current) {
      if (!seeking) { seek.value = 0; seek.max = 1; paint(seek); }
      $("currentTime").textContent = "00:00";
      $("totalTime").textContent = "00:00";
      return;
    }
    const elapsed = player.isPlaying && !player.isPaused ? Date.now() - positionStamp : 0;
    const position = Math.min(positionBase + elapsed, current.length || 0);
    seek.max = current.length || 1;
    if (!seeking) {
      seek.value = position;
      paint(seek);
      $("currentTime").textContent = fmt(position);
    }
    $("totalTime").textContent = current.isStream ? "LIVE" : fmt(current.length);
  }

  function renderEffects() {
    const player = state.player;
    const active = new Set(player ? player.activeFilters : []);
    document.querySelectorAll("[data-audio-mode]").forEach((button) => {
      const mode = button.dataset.audioMode;
      button.disabled = !player;
      button.classList.toggle(
        "on",
        player ? (mode === "normal" ? active.size === 0 : active.has(mode)) : false
      );
    });
    document.querySelectorAll("[data-effect]").forEach((input) => { input.disabled = !player; });
    $("clearEffectsButton").disabled = !player || active.size === 0;
    $("effectHint").textContent = player
      ? "กดพรีเซ็ตเพื่อสลับโหมด หรือเลื่อนสไลเดอร์เพื่อปรับละเอียด"
      : "เชื่อมต่อห้องเสียงก่อนจึงจะใช้เอฟเฟกต์ได้";
  }

  function renderSettings() {
    const settings = state.settings;
    const guild = currentGuild();
    if (!settings) return;

    const language = $("setLanguage");
    if (document.activeElement !== language) {
      const languages = state.languages && state.languages.length ? state.languages : [settings.language];
      language.innerHTML = languages.map((lang) =>
        `<option value="${esc(lang)}"${lang === settings.language ? " selected" : ""}>${esc(lang)}</option>`
      ).join("");
    }

    const djRole = $("setDjRole");
    if (document.activeElement !== djRole && guild) {
      const options = ['<option value="">ไม่กำหนด</option>'];
      for (const role of guild.roles) {
        options.push(
          `<option value="${esc(role.id)}"${role.id === settings.djRoleId ? " selected" : ""}>${esc(role.name)}</option>`
        );
      }
      djRole.innerHTML = options.join("");
      djRole.value = settings.djRoleId || "";
    }

    const queueMode = $("setQueueMode");
    if (document.activeElement !== queueMode) queueMode.value = settings.queueMode;

    if (document.activeElement !== $("setVolume")) {
      $("setVolume").value = settings.defaultVolume;
      $("setVolumeLabel").textContent = `${settings.defaultVolume}%`;
      paint($("setVolume"));
    }

    document.querySelectorAll("[data-setting]").forEach((button) => {
      const key = SETTING_STATE_KEYS[button.dataset.setting];
      button.classList.toggle("on", Boolean(settings[key]));
    });

    const requestChannel = $("setRequestChannel");
    if (document.activeElement !== requestChannel && guild) {
      const options = ['<option value="">ไม่กำหนด</option>'];
      for (const channel of guild.textChannels) {
        options.push(
          `<option value="${esc(channel.id)}"${channel.id === settings.requestChannelId ? " selected" : ""}># ${esc(channel.name)}</option>`
        );
      }
      requestChannel.innerHTML = options.join("");
      requestChannel.value = settings.requestChannelId || "";
    }

    const template = $("setStageTemplate");
    if (document.activeElement !== template) template.value = settings.stageAnnounceTemplate || "";
  }

  // ---------------------------------------------------------------- playlists

  async function loadPlaylists(playlistId) {
    if (!playlistUserId) return;
    const params = new URLSearchParams({ userId: playlistUserId });
    if (playlistId) params.set("playlistId", playlistId);
    try {
      const response = await fetch(`/api/playlists?${params}`, { headers: headers() });
      const payload = await response.json().catch(() => ({}));
      if (response.status === 401) { lock(); return; }
      if (!response.ok || !payload.ok) {
        toast(payload.message || "โหลดเพลย์ลิสต์ไม่สำเร็จ", true);
        return;
      }
      playlistData = payload;
      selectedPlaylistId = playlistId || (payload.playlists[0] && payload.playlists[0].id) || null;
      if (!playlistId && selectedPlaylistId) return loadPlaylists(selectedPlaylistId);
      renderPlaylists();
    } catch (err) {
      toast("เชื่อมต่อเซิร์ฟเวอร์ไม่ได้", true);
    }
  }

  function renderPlaylists() {
    if (!playlistData) return;
    $("plWorkspace").classList.remove("hidden");
    $("plCount").textContent =
      `${playlistData.playlists.length}/${playlistData.limits.maxPlaylists}`;

    $("plList").innerHTML = playlistData.playlists.map((playlist) => {
      const badges = [];
      if (playlist.type === "share") badges.push("แชร์มา");
      if (playlist.type === "link") badges.push("ลิงก์");
      if (playlist.locked) badges.push("ล็อก");
      if (playlist.broken) badges.push("ใช้ไม่ได้");
      const count = playlist.trackCount == null ? "" : `${playlist.trackCount} เพลง`;
      return `<button class="list-pick${playlist.id === selectedPlaylistId ? " on" : ""}" data-playlist="${esc(playlist.id)}" type="button">
        <span>${esc(playlist.name)}</span>
        <span class="badge-row">${badges.map((b) => `<span class="badge">${b}</span>`).join(" ")}
        <small>${count}</small></span>
      </button>`;
    }).join("");

    const detail = playlistData.detail;
    $("plDetailName").innerHTML = detail
      ? `เพลงใน ${esc(detail.name)}<span class="count">${detail.tracks.length} เพลง</span>`
      : "เพลงในเพลย์ลิสต์";
    if (detail && detail.type === "link") {
      $("plTracks").innerHTML =
        `<div class="empty-note">เพลย์ลิสต์แบบลิงก์<br>${esc(detail.uri || "")}</div>`;
    } else if (detail && detail.tracks.length) {
      $("plTracks").innerHTML = detail.tracks.map((track, index) => `<div class="q-row">
        <span class="idx">${index + 1}</span>
        <div class="t"><strong>${esc(track.title)} - ${esc(track.author)}</strong></div>
        <span class="dur">${fmt(track.length)}</span>
        <span class="act-pair">
          <button class="act play" data-pl-play-track="${esc(track.id)}" title="เล่นเพลงนี้">${ICON_PLAY}</button>
          <button class="act" data-pl-remove="${index + 1}" title="ลบออกจากเพลย์ลิสต์">${ICON_X}</button>
        </span>
      </div>`).join("");
    } else {
      $("plTracks").innerHTML = '<div class="empty-note">เพลย์ลิสต์นี้ยังว่าง</div>';
    }

    $("inboxCount").textContent = playlistData.inbox.length ? `${playlistData.inbox.length}` : "";
    $("inboxList").innerHTML = playlistData.inbox.length
      ? playlistData.inbox.map((mail) => `<div class="inbox-item">
          <strong>${esc(mail.title)}</strong>
          <small>${esc(mail.description)}</small>
          <div class="btn-row">
            <button class="btn solid" data-inbox-accept="${mail.index}" type="button">รับ</button>
            <button class="btn danger" data-inbox-decline="${mail.index}" type="button">ปฏิเสธ</button>
          </div>
        </div>`).join("")
      : '<div class="empty-note">ไม่มีคำเชิญ</div>';
  }

  async function playlistAction(body, reloadDetail = true) {
    body.userId = playlistUserId;
    const result = await doAction(body);
    if (result) await loadPlaylists(reloadDetail ? selectedPlaylistId : null);
    return result;
  }

  // ---------------------------------------------------------------- polling

  async function refresh() {
    try {
      const payload = await fetchState();
      state = payload;
      if (payload.selectedGuildId) {
        selectedGuildId = payload.selectedGuildId;
        localStorage.setItem(STORE.guild, selectedGuildId);
      }
      render();
      return true;
    } catch (err) {
      if (err && err.auth) { lock(); return false; }
      return false;
    }
  }

  function startPolling() {
    stopPolling();
    pollTimer = setInterval(() => {
      if (!document.hidden) refresh();
    }, 4000);
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  function unlock() {
    $("loginShell").classList.add("hidden");
    $("appShell").classList.remove("hidden");
    startPolling();
  }

  function lock() {
    password = "";
    localStorage.removeItem(STORE.password);
    stopPolling();
    $("appShell").classList.add("hidden");
    $("loginShell").classList.remove("hidden");
    $("loginError").textContent = "";
  }

  // ---------------------------------------------------------------- wiring

  $("loginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    password = $("adminPassword").value.trim();
    $("loginError").textContent = "";
    $("loginButton").disabled = true;
    const ok = await refresh();
    $("loginButton").disabled = false;
    if (ok) {
      localStorage.setItem(STORE.password, password);
      unlock();
    } else {
      $("loginError").textContent = "รหัสผ่านไม่ถูกต้อง หรือเชื่อมต่อบอทไม่ได้";
    }
  });

  $("togglePassword").addEventListener("click", () => {
    const input = $("adminPassword");
    const isHidden = input.type === "password";
    input.type = isHidden ? "text" : "password";
    $("togglePassword").textContent = isHidden ? "HIDE" : "SHOW";
  });

  $("lockButton").addEventListener("click", lock);

  document.getElementById("nav").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-tab]");
    if (!button) return;
    document.querySelectorAll("#nav button").forEach((b) => b.classList.remove("active"));
    button.classList.add("active");
    document.querySelectorAll(".panel").forEach((panel) =>
      panel.classList.toggle("active", panel.dataset.panel === button.dataset.tab));
  });

  $("guildSelect").addEventListener("change", () => {
    selectedGuildId = $("guildSelect").value;
    localStorage.setItem(STORE.guild, selectedGuildId);
    refresh();
  });

  $("connectButton").addEventListener("click", () => {
    const channelId = $("channelSelect").value;
    if (!channelId) { toast("เลือกห้องเสียงก่อน", true); return; }
    doAction({ action: "connect", voiceChannelId: channelId });
  });

  $("disconnectButton").addEventListener("click", () => doAction({ action: "disconnect" }));

  const addSubmit = () => document.querySelector('#addForm button[type="submit"]');

  function queueAdd(mode, trigger) {
    const query = $("queryInput").value.trim();
    if (!query) { toast("พิมพ์ชื่อเพลงหรือวางลิงก์ก่อน", true); return; }
    const body = { action: "play", query, mode };
    const channelId = $("channelSelect").value;
    if (channelId) body.voiceChannelId = channelId;
    const button = trigger || addSubmit();
    setLoading(button, true);
    $("queryInput").disabled = true;
    doAction(body)
      .then((result) => { if (result) $("queryInput").value = ""; })
      .finally(() => { setLoading(button, false); $("queryInput").disabled = false; });
  }
  $("addForm").addEventListener("submit", (event) => { event.preventDefault(); queueAdd("append", addSubmit()); });
  $("addTopButton").addEventListener("click", (event) => queueAdd("top", event.currentTarget));
  $("addForceButton").addEventListener("click", (event) => queueAdd("force", event.currentTarget));

  $("clearQueueButton").addEventListener("click", () => doAction({ action: "clear" }));
  $("shuffleButton").addEventListener("click", () => doAction({ action: "shuffle" }));
  $("shuffleButton2").addEventListener("click", () => doAction({ action: "shuffle" }));
  $("pauseButton").addEventListener("click", () => {
    const paused = state && state.player ? state.player.isPaused : false;
    doAction({ action: "pause", pause: !paused });
  });
  $("skipButton").addEventListener("click", () => doAction({ action: "skip" }));
  $("previousButton").addEventListener("click", () => doAction({ action: "previous" }));
  $("repeatButton").addEventListener("click", () => {
    const mode = state && state.player ? state.player.repeatMode : "off";
    doAction({ action: "repeat", mode: REPEAT_NEXT[mode] || "off" });
  });
  $("autoplayToggle").addEventListener("click", () => {
    const enabled = Boolean(state && state.player && state.player.autoplay);
    doAction({ action: "autoplay", enabled: !enabled });
  });

  $("queueList").addEventListener("click", (event) => {
    const remove = event.target.closest("[data-remove]");
    if (remove) doAction({ action: "remove", index: Number(remove.dataset.remove) });
  });
  $("historyList").addEventListener("click", (event) => {
    const replay = event.target.closest("[data-replay]");
    if (replay) doAction({ action: "play_encoded", trackId: replay.dataset.replay, mode: "append" });
  });

  $("exportButton").addEventListener("click", async () => {
    const result = await doAction({ action: "queue_export" }, { silent: true });
    if (!result || !result.data) return;
    const tracks = result.data.tracks;
    const total = tracks.reduce((sum, t) => sum + (t.length || 0), 0);
    const lines = [
      "!Remember do not change this file!",
      "------------->Info<-------------",
      `Guild: ${result.data.guildName}`,
      "Requester: Web Dashboard",
      `Tracks: ${tracks.length} - ${fmt(total)}`,
      "------------>Tracks<------------",
      ...tracks.map((t, i) => `${i + 1}. ${t.title} [${fmt(t.length)}]`),
      "----------->Raw Info<-----------",
      tracks.map((t) => t.id).join(","),
    ];
    const blob = new Blob([lines.join("\n")], { type: "text/plain;charset=utf-8" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `${result.data.guildName}_Full_Queue.txt`;
    link.click();
    URL.revokeObjectURL(link.href);
    toast("ส่งออกคิวแล้ว");
  });

  $("importButton").addEventListener("click", () => $("importFile").click());
  $("importFile").addEventListener("change", async () => {
    const file = $("importFile").files[0];
    $("importFile").value = "";
    if (!file) return;
    const text = await file.text();
    const lines = text.split("\n").map((line) => line.trim()).filter(Boolean);
    const last = lines[lines.length - 1] || "";
    const trackIds = last.split(",").map((item) => item.trim()).filter(Boolean);
    if (!trackIds.length) { toast("ไม่พบเพลงในไฟล์นี้", true); return; }
    const body = { action: "queue_import", trackIds };
    const channelId = $("channelSelect").value;
    if (channelId) body.voiceChannelId = channelId;
    doAction(body);
  });

  // seek + volume
  $("seekRange").addEventListener("input", () => {
    seeking = true;
    $("currentTime").textContent = fmt(Number($("seekRange").value));
    paint($("seekRange"));
  });
  $("seekRange").addEventListener("change", () => {
    const position = Math.floor(Number($("seekRange").value));
    seeking = false;
    doAction({ action: "seek", position }, { silent: true });
  });
  $("volumeRange").addEventListener("input", () => {
    volumeDragging = true;
    $("volumeLabel").textContent = `${$("volumeRange").value}%`;
    paint($("volumeRange"));
  });
  $("volumeRange").addEventListener("change", () => {
    volumeDragging = false;
    doAction({ action: "volume", volume: Number($("volumeRange").value) }, { silent: true });
  });

  // effects
  document.querySelectorAll("[data-audio-mode]").forEach((button) => {
    button.addEventListener("click", () =>
      doAction({ action: "audio_mode", mode: button.dataset.audioMode }));
  });
  document.querySelectorAll("[data-effect]").forEach((input) => {
    input.addEventListener("input", () => paint(input));
    input.addEventListener("change", () => {
      const params = {};
      params[input.dataset.param] = Number(input.value);
      doAction({ action: "effect_set", effect: input.dataset.effect, params });
    });
    paint(input);
  });
  $("clearEffectsButton").addEventListener("click", () => doAction({ action: "effect_clear" }));

  // settings
  $("setLanguage").addEventListener("change", () =>
    doAction({ action: "settings_update", field: "language", value: $("setLanguage").value }));
  $("setDjRole").addEventListener("change", () =>
    doAction({ action: "settings_update", field: "dj_role", value: $("setDjRole").value || null }));
  $("setQueueMode").addEventListener("change", () =>
    doAction({ action: "settings_update", field: "queue_mode", value: $("setQueueMode").value }));
  $("setVolume").addEventListener("input", () => {
    $("setVolumeLabel").textContent = `${$("setVolume").value}%`;
    paint($("setVolume"));
  });
  $("setVolume").addEventListener("change", () =>
    doAction({ action: "settings_update", field: "default_volume", value: Number($("setVolume").value) }));
  document.querySelectorAll("[data-setting]").forEach((button) => {
    button.addEventListener("click", () => {
      const key = SETTING_STATE_KEYS[button.dataset.setting];
      const currentValue = Boolean(state && state.settings && state.settings[key]);
      doAction({ action: "settings_update", field: button.dataset.setting, value: !currentValue });
    });
  });
  $("saveRequestChannel").addEventListener("click", () =>
    doAction({
      action: "settings_update",
      field: "music_request_channel",
      value: $("setRequestChannel").value || null,
    }));
  $("saveStageTemplate").addEventListener("click", () =>
    doAction({
      action: "settings_update",
      field: "stage_announce_template",
      value: $("setStageTemplate").value.trim() || null,
    }));
  $("resetButton").addEventListener("click", () =>
    doAction({ action: "settings_reset", field: $("resetField").value }));

  // playlists
  $("plUserId").value = playlistUserId;
  $("plLoadButton").addEventListener("click", () => {
    const value = $("plUserId").value.trim();
    if (!/^\d{15,21}$/.test(value)) { toast("ใส่ Discord User ID ให้ถูกต้อง", true); return; }
    playlistUserId = value;
    localStorage.setItem(STORE.user, value);
    loadPlaylists();
  });
  $("plList").addEventListener("click", (event) => {
    const pick = event.target.closest("[data-playlist]");
    if (pick) loadPlaylists(pick.dataset.playlist);
  });
  $("plPlayButton").addEventListener("click", () => {
    if (!selectedPlaylistId) return;
    const body = { action: "playlist_play", playlistId: selectedPlaylistId };
    const channelId = $("channelSelect").value;
    if (channelId) body.voiceChannelId = channelId;
    playlistAction(body);
  });
  $("plAddButton").addEventListener("click", (event) => {
    const query = $("plAddInput").value.trim();
    if (!query || !selectedPlaylistId) return;
    const button = event.currentTarget;
    setLoading(button, true);
    playlistAction({ action: "playlist_add", playlistId: selectedPlaylistId, query })
      .then((result) => { if (result) $("plAddInput").value = ""; })
      .finally(() => setLoading(button, false));
  });
  $("plTracks").addEventListener("click", (event) => {
    const play = event.target.closest("[data-pl-play-track]");
    if (play) {
      const body = { action: "play_encoded", trackId: play.dataset.plPlayTrack, mode: "append" };
      const channelId = $("channelSelect").value;
      if (channelId) body.voiceChannelId = channelId;
      doAction(body);
      return;
    }
    const remove = event.target.closest("[data-pl-remove]");
    if (remove) {
      playlistAction({
        action: "playlist_remove_track",
        playlistId: selectedPlaylistId,
        position: Number(remove.dataset.plRemove),
      });
    }
  });
  $("plCreateButton").addEventListener("click", () => {
    const name = $("plCreateName").value.trim();
    if (!name) { toast("ตั้งชื่อเพลย์ลิสต์ก่อน", true); return; }
    playlistAction({
      action: "playlist_create",
      name,
      link: $("plCreateLink").value.trim(),
    }, false).then((result) => {
      if (result) { $("plCreateName").value = ""; $("plCreateLink").value = ""; }
    });
  });
  $("plRenameButton").addEventListener("click", () => {
    const name = $("plRenameInput").value.trim();
    if (!name || !selectedPlaylistId) return;
    playlistAction({ action: "playlist_rename", playlistId: selectedPlaylistId, name })
      .then((result) => { if (result) $("plRenameInput").value = ""; });
  });
  $("plDeleteButton").addEventListener("click", () => {
    if (!selectedPlaylistId) return;
    playlistAction({ action: "playlist_delete", playlistId: selectedPlaylistId }, false);
  });
  $("plClearButton").addEventListener("click", () => {
    if (!selectedPlaylistId) return;
    playlistAction({ action: "playlist_clear", playlistId: selectedPlaylistId });
  });
  $("plShareButton").addEventListener("click", () => {
    const target = $("plShareTarget").value.trim();
    if (!/^\d{15,21}$/.test(target)) { toast("ใส่ User ID ผู้รับให้ถูกต้อง", true); return; }
    if (!selectedPlaylistId) return;
    playlistAction({
      action: "playlist_share",
      playlistId: selectedPlaylistId,
      targetUserId: Number(target),
    }).then((result) => { if (result) $("plShareTarget").value = ""; });
  });
  $("plPermButton").addEventListener("click", () => {
    const target = $("plShareTarget").value.trim();
    if (!/^\d{15,21}$/.test(target)) { toast("ใส่ User ID ในช่องผู้รับก่อน", true); return; }
    if (!selectedPlaylistId) return;
    playlistAction({
      action: "playlist_permission",
      playlistId: selectedPlaylistId,
      permission: $("plPermSelect").value,
      permAction: $("plPermAction").value,
      targetUserId: Number(target),
    });
  });
  $("inboxList").addEventListener("click", (event) => {
    const accept = event.target.closest("[data-inbox-accept]");
    if (accept) {
      playlistAction({ action: "inbox_accept", index: Number(accept.dataset.inboxAccept) }, false);
      return;
    }
    const decline = event.target.closest("[data-inbox-decline]");
    if (decline) {
      playlistAction({ action: "inbox_decline", index: Number(decline.dataset.inboxDecline) }, false);
    }
  });

  // position ticker
  setInterval(() => { if (state && state.player) updateSeekUI(); }, 500);

  // ---------------------------------------------------------------- boot

  document.querySelectorAll("input[type=range]").forEach(paint);
  if (password) {
    refresh().then((ok) => {
      if (ok) unlock();
      else lock();
    });
  }
})();
