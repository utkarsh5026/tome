const $ = (s) => document.querySelector(s);
let TREE = [], FLAT = [], CUR = null, VERSION = "", SEL = 0, RESULTS = [], PENDING_HIT = null;

/* ── chrome (the two side panels) ────────────────────────────────────────────
   Two independent, remembered preferences. HAS_TOC is a property of the open
   document, so it is kept apart from the user's choice — otherwise navigating
   to the next doc would silently undo a hidden outline. "zen" is not a third
   state: it simply means both panels are hidden.                            */
let HIDE_NAV = localStorage.getItem(K("hideNav")) === "1";
let HIDE_TOC = localStorage.getItem(K("hideToc")) === "1";
let HAS_TOC = false;

function applyChrome(){
  const zen = HIDE_NAV && HIDE_TOC;
  $("#app").classList.toggle("nonav", HIDE_NAV);
  $("#wrap").classList.toggle("notoc", HIDE_TOC || !HAS_TOC);
  $("#navBtn").classList.toggle("off", HIDE_NAV);
  $("#tocBtn").classList.toggle("off", HIDE_TOC);
  $("#tocBtn").disabled = !HAS_TOC;
  $("#tocBtn").style.opacity = HAS_TOC ? "" : ".35";
  $("#zenBtn").classList.toggle("on", zen);
  $("#zenBtn").textContent = zen ? "exit zen" : "zen";
  localStorage.setItem(K("hideNav"), HIDE_NAV ? "1" : "");
  localStorage.setItem(K("hideToc"), HIDE_TOC ? "1" : "");
}

/* ── themes ──────────────────────────────────────────────────────────────────
   id, label, and whether it is a light or dark palette. The colours live in
   the stylesheet; this list only decides what appears in the picker.       */
const THEMES = [
  ["mocha", "Catppuccin Mocha", "dark"],
  ["macchiato", "Catppuccin Macchiato", "dark"],
  ["tokyo", "Tokyo Night", "dark"],
  ["nord", "Nord", "dark"],
  ["dracula", "Dracula", "dark"],
  ["gruvbox", "Gruvbox Dark", "dark"],
  ["rosepine", "Rosé Pine", "dark"],
  ["onedark", "One Dark", "dark"],
  ["kanagawa", "Kanagawa", "dark"],
  ["everforest", "Everforest", "dark"],
  ["nightowl", "Night Owl", "dark"],
  ["solarized", "Solarized Dark", "dark"],
  ["ayu", "Ayu Mirage", "dark"],
  ["monokai", "Monokai", "dark"],
  ["latte", "Catppuccin Latte", "light"],
  ["github", "GitHub Light", "light"],
  ["solarized-light", "Solarized Light", "light"],
  ["rosepine-dawn", "Rosé Pine Dawn", "light"],
  ["gruvbox-light", "Gruvbox Light", "light"],
];
let THEME = localStorage.getItem(K("theme")) || TOME.theme;

const previewTheme = (id) => { document.documentElement.dataset.theme = id; };

function commitTheme(id){
  THEME = id;
  localStorage.setItem(K("theme"), id);
  previewTheme(id);
  markThemes();
}

function markThemes(){
  document.querySelectorAll(".tcard").forEach(c => {
    const on = c.dataset.theme === THEME;
    c.classList.toggle("on", on);
    const tick = c.querySelector(".tick");
    if (tick) tick.textContent = on ? "✓" : "";
  });
}

function renderThemes(){
  const card = (t) =>
    `<div class="tcard" data-theme="${t[0]}"><div class="tname">${esc(t[1])}` +
    `<span class="tick"></span></div><div class="tdots">` +
    ["--blue", "--green", "--yellow", "--red", "--mauve", "--panel2"]
      .map(v => `<i style="background:var(${v})"></i>`).join("") +
    `</div></div>`;
  const group = (mode) => `<div class="tsec">${mode}</div><div class="tgrid">` +
    THEMES.filter(t => t[2] === mode).map(card).join("") + "</div>";
  $("#tpop").innerHTML = group("dark") + group("light") +
    '<div class="thint">hover to preview · click to keep · esc to cancel</div>';
  document.querySelectorAll(".tcard").forEach(c => {
    c.onmouseenter = () => previewTheme(c.dataset.theme);
    c.onclick = () => { commitTheme(c.dataset.theme); themePop(false); };
  });
  $("#tpop").onmouseleave = () => previewTheme(THEME);
  markThemes();
}

/* Closing without a click reverts whatever was being previewed. */
function themePop(show){
  const el = $("#tpop");
  const on = show === undefined ? !el.classList.contains("on") : show;
  el.classList.toggle("on", on);
  $("#themeBtn").classList.toggle("on", on);
  on ? markThemes() : previewTheme(THEME);
}

function toggleNav(){ HIDE_NAV = !HIDE_NAV; applyChrome(); }
function toggleToc(){ if (HAS_TOC){ HIDE_TOC = !HIDE_TOC; applyChrome(); } }
/* Zen hides both; pressing it again brings both back. */
function toggleZen(){
  const zen = HIDE_NAV && HIDE_TOC;
  HIDE_NAV = HIDE_TOC = !zen;
  applyChrome();
}

/* ── data ───────────────────────────────────────────────────────────────── */
async function loadTree(){
  const r = await fetch("/api/tree").then(r => r.json());
  TREE = r.groups; VERSION = r.version;
  FLAT = [];
  for (const g of TREE) for (const d of g.docs) FLAT.push({...d, group: g.title, gid: g.id});
  $("#count").textContent = FLAT.length + " docs";
  renderNav();
}

function renderNav(){
  const filter = $("#filter").value.trim().toLowerCase();
  const nav = $("#nav");
  nav.innerHTML = "";
  const saved = JSON.parse(localStorage.getItem(RK("open")) || "{}");
  for (const g of TREE){
    const docs = filter
      ? g.docs.filter(d => (d.title + " " + d.path + " " + d.label).toLowerCase().includes(filter))
      : g.docs;
    if (!docs.length) continue;
    const isOpen = filter ? true : (saved[g.id] ?? defaultOpen(g));
    const div = document.createElement("div");
    div.className = "grp" + (isOpen ? " open" : "");
    div.innerHTML =
      `<div class="ghead${isOpen ? " open" : ""}"><span class="chev">▶</span>` +
      (g.num ? `<span class="num">${g.num}</span>` : "") +
      `<span>${esc(g.title)}</span>` +
      (g.state ? `<span class="st ${esc(g.state)}" title="${esc(g.state)}"></span>` : "") +
      `</div><div class="gitems">` +
      docs.map(d =>
        `<a class="item k${d.kind}${d.path === CUR ? " on" : ""}" href="#/${d.path}">` +
        `<span class="k">${d.kind === "doc" ? "·" : d.kind[0]}</span>` +
        `<span>${esc(d.label)}</span></a>`).join("") +
      `</div>`;
    div.querySelector(".ghead").onclick = () => {
      const now = !div.classList.contains("open");
      div.classList.toggle("open", now);
      div.querySelector(".ghead").classList.toggle("open", now);
      const st = JSON.parse(localStorage.getItem(RK("open")) || "{}");
      st[g.id] = now; localStorage.setItem(RK("open"), JSON.stringify(st));
    };
    nav.appendChild(div);
  }
}
/* Open the section holding the current doc; otherwise only the repo-root one.
   `prefix` comes from the server, which is the only side that knows how this
   particular repo was grouped.                                             */
const defaultOpen = (g) =>
  Boolean(g.prefix && CUR && CUR.startsWith(g.prefix)) || g.id === "root";
const ESCAPES = {"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;"};
const esc = (s) => (s || "").replace(/[&<>"]/g, (c) => ESCAPES[c]);

/* ── routing ────────────────────────────────────────────────────────────── */
function route(){
  const h = decodeURIComponent(location.hash.replace(/^#\/?/, ""));
  if (!h){
    const last = localStorage.getItem(RK("last"));
    if (last){ location.hash = "#/" + last; return; }
    return openDoc(TOME.home || (FLAT[0] && FLAT[0].path) || "README.md");
  }
  const [path, anchor] = h.split("::");
  openDoc(path, anchor);
}

async function openDoc(path, anchor, keepScroll){
  const scroll = keepScroll ? window.scrollY : 0;
  const d = await fetch("/api/doc?p=" + encodeURIComponent(path)).then(r => r.json());
  if (d.error){
    $("#doc").innerHTML = `<div class="empty">${esc(d.error)}</div>`;
    $("#crumb").textContent = path;
    HAS_TOC = false; $("#toc").innerHTML = ""; applyChrome();
    return;
  }
  CUR = d.path;
  localStorage.setItem(RK("last"), d.path);
  document.title = d.title + " · gauntlet docs";
  const parts = d.path.split("/");
  $("#crumb").innerHTML = parts.map((p, i) =>
    i === parts.length - 1 ? `<b>${esc(p)}</b>` : esc(p)).join(" / ");
  const ed = $("#editBtn");
  ed.href = TOME.editorUrl ? TOME.editorUrl.replace("{path}", d.abs) : "#";
  ed.style.display = TOME.editorUrl ? "" : "none";
  $("#doc").innerHTML = d.html + prevNext(d.path);
  buildToc(d.toc);
  wireCode();
  if (d.mermaid) loadMermaid();
  renderNav();
  document.querySelector(".item.on")?.scrollIntoView({block:"nearest"});
  if (keepScroll) window.scrollTo(0, scroll);
  else if (anchor){
    const el = document.getElementById(anchor);
    el ? el.scrollIntoView() : window.scrollTo(0, 0);
  } else window.scrollTo(0, 0);
  if (PENDING_HIT){ const q = PENDING_HIT; PENDING_HIT = null; scrollToText(q); }
}

/* After opening a full-text hit, land on the match instead of the page top. */
function scrollToText(q){
  const needle = q.toLowerCase();
  const walk = document.createTreeWalker($("#doc"), NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walk.nextNode())){
    const at = node.textContent.toLowerCase().indexOf(needle);
    if (at < 0) continue;
    const range = document.createRange();
    range.setStart(node, at);
    range.setEnd(node, at + needle.length);
    const mark = document.createElement("mark");
    mark.className = "flash";
    try { range.surroundContents(mark); } catch(_) { return; }
    mark.scrollIntoView({block: "center"});
    setTimeout(() => mark.classList.remove("flash"), 2200);
    return;
  }
}

function prevNext(path){
  const i = FLAT.findIndex(d => d.path === path);
  if (i < 0) return "";
  const p = FLAT[i - 1], n = FLAT[i + 1];
  let h = '<div class="pn">';
  const gap = "<span style='flex:1'></span>";
  h += p ? `<a href="#/${p.path}"><span class="d">← previous</span>${esc(p.title)}</a>` : gap;
  h += n ? `<a class="next" href="#/${n.path}"><span class="d">next →</span>${esc(n.title)}</a>`
         : gap;
  return h + "</div>";
}

function buildToc(toc){
  const el = $("#toc");
  HAS_TOC = Boolean(toc && toc.length >= 2);
  if (!HAS_TOC){ el.innerHTML = ""; applyChrome(); return; }
  applyChrome();
  el.innerHTML = '<div class="th">on this page</div>' + toc.map(t =>
    `<a class="l${t.level}" href="#${t.id}" data-id="${t.id}">${esc(t.text)}</a>`).join("");
  el.querySelectorAll("a").forEach(a => a.onclick = (e) => {
    e.preventDefault();
    document.getElementById(a.dataset.id)?.scrollIntoView();
  });
  observeHeadings(toc);
}

let OBS = null;
function observeHeadings(toc){
  OBS?.disconnect();
  const links = new Map([...$("#toc").querySelectorAll("a")].map(a => [a.dataset.id, a]));
  OBS = new IntersectionObserver((entries) => {
    for (const e of entries){
      if (!e.isIntersecting) continue;
      links.forEach(a => a.classList.remove("on"));
      links.get(e.target.id)?.classList.add("on");
      break;
    }
  }, {rootMargin: "-70px 0px -75% 0px"});
  toc.forEach(t => { const el = document.getElementById(t.id); if (el) OBS.observe(el); });
}

function wireCode(){
  document.querySelectorAll(".cb .copy").forEach(b => b.onclick = () => {
    navigator.clipboard.writeText(b.parentElement.querySelector("code").innerText);
    toast("copied");
  });
}

let toastT;
function toast(msg){
  const t = $("#toast"); t.textContent = msg; t.classList.add("on");
  clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove("on"), 1400);
}

/* Mermaid is the one thing we cannot render offline — try the CDN, and if it is
   not reachable the diagram just stays readable as its source text. */
let mermaidTried = false;
function loadMermaid(){
  if (window.mermaid){ window.mermaid.run({querySelector:"pre.mermaid"}); return; }
  if (mermaidTried) return;
  mermaidTried = true;
  // Diagrams follow the active theme — read the live variables rather than
  // baking in one palette.
  const cs = getComputedStyle(document.documentElement);
  const v = (name) => cs.getPropertyValue(name).trim();
  const light = (THEMES.find(t => t[0] === THEME) || [])[2] === "light";
  const s = document.createElement("script");
  s.type = "module";
  s.textContent =
    'import m from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";' +
    `window.mermaid = m; m.initialize({startOnLoad:false, theme:"${light ? "default" : "dark"}",` +
    `themeVariables:{background:"${v("--panel2")}",primaryColor:"${v("--panel")}",` +
    `lineColor:"${v("--dim")}",primaryTextColor:"${v("--text")}",` +
    `primaryBorderColor:"${v("--line2")}"}});` +
    'm.run({querySelector:"pre.mermaid"});';
  document.head.appendChild(s);
}

/* ── command palette ────────────────────────────────────────────────────── */
function fuzzy(q, s){
  q = q.toLowerCase(); s = s.toLowerCase();
  let qi = 0, score = 0, streak = 0;
  for (let i = 0; i < s.length && qi < q.length; i++){
    if (s[i] === q[qi]){ qi++; streak++; score += streak * 2 + (i === 0 ? 6 : 0); }
    else streak = 0;
  }
  return qi === q.length ? score - s.length * 0.04 : -1;
}

let searchT;
function palette(show){
  $("#scrim").classList.toggle("on", show);
  if (show){ $("#q").value = ""; $("#q").focus(); paletteRender(""); }
}

function paletteRender(q){
  const list = $("#pres");
  if (!q){
    RESULTS = FLAT.slice(0, 40).map(d => ({...d, _hit: false}));
  } else {
    RESULTS = FLAT
      .map(d => ({d, s: Math.max(fuzzy(q, d.title), fuzzy(q, d.path))}))
      .filter(x => x.s > 0).sort((a, b) => b.s - a.s).slice(0, 25)
      .map(x => ({...x.d, _hit: false}));
  }
  SEL = 0;
  list.innerHTML = RESULTS.map((r, i) => rowHtml(r, i)).join("") ||
    '<div class="empty" style="padding:30px">no matches</div>';
  wireRows();
  if (q.length >= 3){
    clearTimeout(searchT);
    searchT = setTimeout(async () => {
      const r = await fetch("/api/search?q=" + encodeURIComponent(q)).then(r => r.json());
      if ($("#q").value.trim() !== q) return;
      const hits = r.results.map(x => ({...x, _hit: true}));
      RESULTS = RESULTS.concat(hits);
      list.innerHTML = RESULTS.map((r, i) => rowHtml(r, i, q)).join("") ||
        '<div class="empty" style="padding:30px">no matches</div>';
      wireRows();
    }, 160);
  }
}

function rowHtml(r, i, q){
  const sel = i === SEL ? " sel" : "";
  if (r._hit){
    let sn = esc(r.snippet);
    if (q){
      const re = new RegExp("(" + q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "ig");
      sn = sn.replace(re, "<mark>$1</mark>");
    }
    return `<div class="pr${sel}" data-i="${i}"><div class="t"><span class="k">↳</span>` +
      `<span>${esc(r.title)}</span></div><div class="sn">${sn}</div>` +
      `<div class="p">${esc(r.path)}:${r.line}</div></div>`;
  }
  return `<div class="pr${sel}" data-i="${i}"><div class="t">` +
    `<span class="k">${esc((r.label || "").slice(0, 5))}</span>` +
    `<span>${esc(r.title)}</span></div><div class="p">${esc(r.path)}</div></div>`;
}

function wireRows(){
  document.querySelectorAll(".pr").forEach(el => {
    el.onclick = () => choose(+el.dataset.i);
    el.onmouseenter = () => { SEL = +el.dataset.i; highlight(); };
  });
}
function highlight(){
  document.querySelectorAll(".pr").forEach(el =>
    el.classList.toggle("sel", +el.dataset.i === SEL));
  document.querySelector(".pr.sel")?.scrollIntoView({block:"nearest"});
}
function choose(i){
  const r = RESULTS[i];
  if (!r) return;
  const q = $("#q").value.trim();
  palette(false);
  if (r._hit && q) PENDING_HIT = q;
  const target = "#/" + r.path;
  // Setting an identical hash fires no hashchange — route by hand in that case.
  if (location.hash === target) route();
  else location.hash = target;
}

/* ── events ─────────────────────────────────────────────────────────────── */
$("#filter").oninput = renderNav;
$("#searchBtn").onclick = () => palette(true);
$("#zenBtn").onclick = toggleZen;
$("#navBtn").onclick = toggleNav;
$("#tocBtn").onclick = toggleToc;
$("#themeBtn").onclick = (e) => { e.stopPropagation(); themePop(); };
document.addEventListener("click", (e) => {
  if (!$("#tpop").classList.contains("on")) return;
  if (!$("#tpop").contains(e.target)) themePop(false);
});
$("#q").oninput = (e) => paletteRender(e.target.value.trim());
$("#scrim").onclick = (e) => { if (e.target === $("#scrim")) palette(false); };

document.addEventListener("keydown", (e) => {
  const palOpen = $("#scrim").classList.contains("on");
  const typing = /input|textarea/i.test(document.activeElement.tagName);
  if ((e.ctrlKey || e.metaKey) && e.key === "k"){ e.preventDefault(); palette(!palOpen); return; }
  if (palOpen){
    if (e.key === "Escape") palette(false);
    else if (e.key === "ArrowDown"){
      e.preventDefault(); SEL = Math.min(SEL + 1, RESULTS.length - 1); highlight(); }
    else if (e.key === "ArrowUp"){ e.preventDefault(); SEL = Math.max(SEL - 1, 0); highlight(); }
    else if (e.key === "Enter"){ e.preventDefault(); choose(SEL); }
    return;
  }
  if ($("#tpop").classList.contains("on") && e.key === "Escape"){ themePop(false); return; }
  if (typing){ if (e.key === "Escape") document.activeElement.blur(); return; }
  if (e.key === "/"){ e.preventDefault(); palette(true); }
  else if (e.key === ","){ themePop(); }
  else if (e.key === "\\"){ toggleZen(); }
  else if (e.key === "s"){ toggleNav(); }
  else if (e.key === "t"){ toggleToc(); }
  else if (e.key === "Escape" && (HIDE_NAV || HIDE_TOC)){
    HIDE_NAV = HIDE_TOC = false; applyChrome();
  }
  else if (e.key === "[" || e.key === "]"){
    const i = FLAT.findIndex(d => d.path === CUR);
    const t = FLAT[i + (e.key === "]" ? 1 : -1)];
    if (t) location.hash = "#/" + t.path;
  }
});

window.addEventListener("hashchange", route);

/* live reload — repoll mtimes; re-render the open doc in place when it changes */
setInterval(async () => {
  try{
    const r = await fetch("/api/version").then(r => r.json());
    if (r.version !== VERSION){
      VERSION = r.version;
      await loadTree();
      if (CUR) await openDoc(CUR, null, true);
      toast("reloaded");
    }
  }catch(_){}
}, 2000);

(async () => { applyChrome(); renderThemes(); await loadTree(); route(); })();
