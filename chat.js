/* Shared chat component. Self-contained — bails silently if the chat DOM
 * isn't on the page. If the host page wants citation clicks to do something
 * (e.g. fly a map to the parcel), it can define window.onChatCitation = (c) => ...
 * before this script runs. Otherwise, citation clicks with a known PDF open it. */
(function () {
  const toggle = document.getElementById("chat-toggle");
  const panel = document.getElementById("chat-panel");
  if (!toggle || !panel) return;  // page doesn't include the chat UI

  const closeBtn = document.getElementById("chat-close");
  const messages = document.getElementById("chat-messages");
  const input = document.getElementById("chat-input");
  const send = document.getElementById("chat-send");

  let pdfUrls = {};
  fetch("pdf_urls.json").then(r => r.ok ? r.json() : {}).then(j => pdfUrls = j).catch(() => {});

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function pdfUrlFor(c) {
    const src = c && c.source_pdf || "";
    const m = src.match(/([^/]+)\.pdf$/i);
    return m && pdfUrls[m[1]] ? pdfUrls[m[1]] : null;
  }

  function openChat() { panel.classList.add("open"); toggle.classList.add("hidden"); input.focus(); }
  function closeChat() { panel.classList.remove("open"); toggle.classList.remove("hidden"); }
  toggle.addEventListener("click", openChat);
  closeBtn.addEventListener("click", closeChat);

  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(120, input.scrollHeight) + "px";
  });

  function append(role, content, opts = {}) {
    const d = document.createElement("div");
    d.className = `chat-msg ${role}`;
    if (opts.raw) d.innerHTML = content; else d.textContent = content;
    messages.appendChild(d);
    messages.scrollTop = messages.scrollHeight;
    return d;
  }

  function renderMarkdown(text) {
    const safe = escapeHtml(text);
    const lines = safe.split(/\n/);
    const blocks = [];
    let para = [], bullets = [];
    const flushPara = () => { if (para.length) { blocks.push(`<p>${para.join(" ")}</p>`); para = []; } };
    const flushBullets = () => { if (bullets.length) { blocks.push(`<ul>${bullets.map(b => `<li>${b}</li>`).join("")}</ul>`); bullets = []; } };
    for (const line of lines) {
      const t = line.trim();
      if (!t) { flushPara(); flushBullets(); continue; }
      const mb = t.match(/^[-*]\s+(.*)/);
      if (mb) { flushPara(); bullets.push(mb[1]); continue; }
      flushBullets();
      para.push(t);
    }
    flushPara(); flushBullets();
    return blocks.join("").replace(
      /\[(ZBA|PB|VBoT)\s+([^\]]+?)\]/g,
      '<code style="background:rgba(255,107,53,0.15);padding:1px 5px;border-radius:3px;font-size:11px;">[$1 $2]</code>'
    );
  }

  function renderCitations(citations) {
    if (!citations || !citations.length) return "";
    const items = citations.slice(0, 8).map((c, i) => {
      const board = c.board || "?";
      const label = `${c.meeting_date || c.year || "?"} · ${c.name || "(unnamed)"}`;
      const loc = [c.street, c.locality].filter(Boolean).join(", ");
      const url = pdfUrlFor(c);
      const pdfIcon = url
        ? ` <a href="${url}" target="_blank" rel="noopener" title="Open source PDF" style="color:var(--accent);text-decoration:none;padding-left:4px;">↗</a>`
        : "";
      return `<div><a data-idx="${i}">
        <span class="citation-board ${board}">${board}</span>
        ${escapeHtml(label)}${loc ? ` <span style="color:var(--muted)"> — ${escapeHtml(loc)}</span>` : ""}
      </a>${pdfIcon}</div>`;
    }).join("");
    return `<div class="chat-citations"><div class="label">Sources (${citations.length})</div>${items}</div>`;
  }

  async function ask() {
    const q = input.value.trim();
    if (!q) return;
    append("user", q);
    input.value = "";
    input.style.height = "auto";
    send.disabled = true;
    const thinking = append("assistant", '<span class="thinking-dots">Searching minutes</span>', { raw: true });
    try {
      const res = await fetch("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q }),
      });
      const body = await res.json();
      thinking.remove();
      if (!res.ok || body.error) return void append("error", body.error || `error ${res.status}`);
      const html = renderMarkdown(body.answer) + renderCitations(body.citations);
      const msg = append("assistant", html, { raw: true });
      msg.querySelectorAll(".chat-citations a[data-idx]").forEach(a => {
        a.addEventListener("click", () => {
          const idx = parseInt(a.dataset.idx, 10);
          const c = body.citations[idx];
          if (!c) return;
          if (typeof window.onChatCitation === "function") {
            window.onChatCitation(c);
          } else {
            const url = pdfUrlFor(c);
            if (url) window.open(url, "_blank", "noopener");
          }
        });
      });
    } catch (e) {
      thinking.remove();
      append("error", `network error: ${e.message}`);
    } finally {
      send.disabled = false;
      input.focus();
    }
  }

  send.addEventListener("click", ask);
  input.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(); }
  });
})();
