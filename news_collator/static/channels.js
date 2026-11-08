/*
 * Channels: readers combine departments, team tags and their own tags into a
 * single feed.
 *
 * - Data: everything the site collected in the window, embedded in the page as
 *   JSON (see web.export_json). Keys: u url, t title, s summary, o outlet,
 *   p published ISO, k kind, d departments, g team tags.
 * - Storage: channels are saved in this browser's localStorage. There is no
 *   server, so a channel is shared by link: the whole definition is encoded in
 *   the URL after #share=.
 * - Live search: if DATA.search_url is set (the search Worker in worker/),
 *   each personal tag also searches UK outlets live and the results are merged
 *   in, de-duplicated against collected coverage by headline.
 * - Personal tag terms use the same syntax as the config files (web.py /
 *   classify.compile_term): whole words, case-insensitive, trailing * for any
 *   ending, ALL-CAPS acronyms and =terms case-sensitive.
 */
(function () {
  "use strict";

  var DATA = JSON.parse(document.getElementById("channel-data").textContent);
  var root = document.getElementById("channels-app");
  var STORE_KEY = "despatch.channels.v1";
  var MAX_ITEMS = 400;
  var SEARCH_URL = DATA.search_url || "";
  var MAX_LIVE_TAGS = 5;  // live searches per channel view, to stay well inside the Worker's free quota
  var TZ = "Europe/London";

  var deptNames = {}, tagNames = {};
  DATA.departments.forEach(function (d) { deptNames[d.slug] = d.name; });
  DATA.tags.forEach(function (t) { tagNames[t.slug] = t.name; });

  // ---------------------------------------------------------------- storage
  // localStorage can be unavailable (private windows, blocked storage);
  // the page still works, channels just aren't remembered.
  function load() {
    try {
      var raw = window.localStorage.getItem(STORE_KEY);
      var parsed = raw ? JSON.parse(raw) : null;
      if (parsed && Array.isArray(parsed.channels)) return parsed;
    } catch (e) { /* fall through */ }
    return { channels: [] };
  }
  function save() {
    try { window.localStorage.setItem(STORE_KEY, JSON.stringify(state)); return true; }
    catch (e) { return false; }
  }
  var state = load();

  // ---------------------------------------------------------------- helpers
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function uid() { return Math.random().toString(36).slice(2, 10); }
  function splitTerms(s) {
    return String(s || "").split(",").map(function (t) { return t.trim(); }).filter(Boolean);
  }
  function plural(n, w, many) { return n + " " + (n === 1 ? w : (many || w + "s")); }

  // Same rules as classify.compile_term in the Python code.
  function compileTerm(term) {
    var cs = term.charAt(0) === "=";
    term = term.replace(/^=+/, "");
    var wild = /\*$/.test(term);
    var core = term.replace(/\*+$/, "").trim();
    if (!core) return null;
    if (core === core.toUpperCase() && /[A-Z]/.test(core) && core.replace(/\s/g, "").length >= 2) cs = true;
    var pat = core.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/'/g, "['\u2019]").replace(/\s+/g, "\\s+");
    var word = "[\\p{L}\\p{N}_]";
    return new RegExp("(^|[^\\p{L}\\p{N}_])" + pat + (wild ? word + "*" : "") + "(?!" + word + ")",
                      cs ? "u" : "iu");
  }
  function compileTag(tag) {
    return {
      name: tag.name,
      terms: tag.terms.map(compileTerm).filter(Boolean),
      exclude: (tag.exclude || []).map(compileTerm).filter(Boolean)
    };
  }

  // Share links: the channel definition, base64url-encoded JSON.
  function encodeChannel(c) {
    var json = JSON.stringify({ n: c.name, d: c.depts, g: c.tags, m: c.mine });
    var bytes = new TextEncoder().encode(json), bin = "";
    bytes.forEach(function (b) { bin += String.fromCharCode(b); });
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function decodeChannel(s) {
    try {
      var bin = atob(s.replace(/-/g, "+").replace(/_/g, "/"));
      var bytes = Uint8Array.from(bin, function (ch) { return ch.charCodeAt(0); });
      var o = JSON.parse(new TextDecoder().decode(bytes));
      return {
        id: uid(), name: String(o.n || "Shared channel").slice(0, 80),
        depts: (o.d || []).filter(function (x) { return x in deptNames; }),
        tags: (o.g || []).filter(function (x) { return x in tagNames; }),
        mine: (o.m || []).slice(0, 20).map(function (t) {
          return { name: String(t.name || "").slice(0, 60), terms: (t.terms || []).slice(0, 30).map(String),
                   exclude: (t.exclude || []).slice(0, 30).map(String) };
        }).filter(function (t) { return t.name && t.terms.length; })
      };
    } catch (e) { return null; }
  }

  // ---------------------------------------------------------------- dates (UK time)
  var partsFmt = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" });
  var timeFmt = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "2-digit", minute: "2-digit" });
  var longFmt = new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", weekday: "long", day: "numeric", month: "long" });
  var shortFmt = new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", day: "numeric", month: "short" });

  function dayKey(date) {
    var p = {};
    partsFmt.formatToParts(date).forEach(function (x) { p[x.type] = x.value; });
    return p.year + "-" + p.month + "-" + p.day;
  }
  var today = dayKey(new Date()), yesterday = dayKey(new Date(Date.now() - 864e5));

  function itemDay(it) {  // written statements carry a bare date; don't shift it
    if (!it.p) return null;
    return it.k === "written_statement" ? it.p.slice(0, 10) : dayKey(new Date(it.p));
  }
  function keyToDate(k) { return new Date(k + "T12:00:00Z"); }
  function dayLabel(k) {
    if (k === today) return "Today";
    if (k === yesterday) return "Yesterday";
    return longFmt.format(keyToDate(k));
  }
  function gutter(it, inDayGroup) {
    var k = itemDay(it);
    if (!k) return "";
    if (it.k === "written_statement") return k === today ? "Today" : shortFmt.format(keyToDate(k));
    if (inDayGroup || k === today) return timeFmt.format(new Date(it.p));
    return shortFmt.format(keyToDate(k));
  }

  // ---------------------------------------------------------------- matching
  function isOfficial(it) { return it.k === "govuk" || it.k === "written_statement"; }

  // ---------------------------------------------------------------- live search
  // Results per personal tag, keyed by its terms: {status: "pending"|"done"|"error", items}.
  var live = {};
  function liveKey(t) { return JSON.stringify([t.terms, t.exclude || []]); }
  function liveTags(channel) { return SEARCH_URL ? channel.mine.slice(0, MAX_LIVE_TAGS) : []; }
  function titleKey(t) { return (String(t).toLowerCase().match(/[a-z0-9]+/g) || []).join(" "); }

  // Add live results to a channel's rows; a headline already present just gains the tag's name.
  function mergeLive(channel, out) {
    var byTitle = {};
    out.forEach(function (r) { byTitle[titleKey(r.item.t)] = r; });
    liveTags(channel).forEach(function (t) {
      var res = live[liveKey(t)];
      if (!res || res.status !== "done") return;
      res.items.forEach(function (it) {
        var k = titleKey(it.t), existing = byTitle[k];
        if (existing) {
          if (existing.why.indexOf(t.name) === -1) existing.why.push(t.name);
          return;
        }
        if (DATA.items.some(function (d) { return titleKey(d.t) === k; })) return;  // collected, but not in this channel
        var row = { item: { u: it.u, t: it.t, o: it.o, p: it.p, k: "search", s: "", d: [], g: [] }, why: [t.name] };
        byTitle[k] = row;
        out.push(row);
      });
    });
  }

  // Start any searches this channel still needs; `done` re-renders when each finishes.
  function startLive(channel, done) {
    liveTags(channel).forEach(function (t) {
      var key = liveKey(t);
      if (live[key]) return;
      live[key] = { status: "pending", items: [] };
      var url = SEARCH_URL + "/search?q=" + encodeURIComponent(t.terms.join(",")) +
                "&x=" + encodeURIComponent((t.exclude || []).join(","));
      fetch(url).then(function (r) { return r.json(); }).then(function (body) {
        live[key] = body && Array.isArray(body.items) && !body.error
          ? { status: "done", items: body.items } : { status: "error", items: [] };
      }, function () {
        live[key] = { status: "error", items: [] };
      }).then(done);
    });
  }

  function liveStatus(channel) {
    var tags = liveTags(channel);
    if (!tags.length) return "";
    var states = tags.map(function (t) { return (live[liveKey(t)] || {}).status; });
    var msg = states.indexOf("pending") > -1 || states.indexOf(undefined) > -1
      ? "Searching UK outlets for your tags…"
      : states.indexOf("error") > -1
        ? "The live search didn't respond for some of your tags, so they show collected coverage only."
        : "Your tags include a live search of UK outlets.";
    return '<p class="live-status" role="status">' + esc(msg) + "</p>";
  }

  // Returns [{item, why: [labels]}] for a channel, official first then newest.
  function run(channel) {
    var mine = channel.mine.map(compileTag);
    var out = [];
    DATA.items.forEach(function (it) {
      var why = [];
      channel.depts.forEach(function (d) { if (it.d.indexOf(d) > -1) why.push(deptNames[d]); });
      channel.tags.forEach(function (g) { if (it.g.indexOf(g) > -1) why.push(tagNames[g]); });
      if (mine.length) {
        var text = it.t + "\n" + (it.s || "");
        mine.forEach(function (t) {
          var hit = t.terms.some(function (rx) { return rx.test(text); });
          if (hit && !t.exclude.some(function (rx) { return rx.test(text); })) why.push(t.name);
        });
      }
      if (why.length) out.push({ item: it, why: why });
    });
    mergeLive(channel, out);
    out.sort(function (a, b) {
      var oa = isOfficial(a.item) ? 0 : 1, ob = isOfficial(b.item) ? 0 : 1;
      if (oa !== ob) return oa - ob;
      return (b.item.p || "").localeCompare(a.item.p || "");
    });
    return groupStories(out);
  }

  // Same story from several outlets -> one row with "also" links. Mirrors
  // digest.group_stories: headlines sharing enough content words (overlap
  // relative to the shorter one), from different outlets; official items never grouped.
  var STOP = {};
  ("a an the and or but of to in on at for with by from as is are was were be been it its this that " +
   "these those after over into about says say said will would could may new uk").split(" ")
    .forEach(function (w) { STOP[w] = 1; });
  function tokens(title) {
    var seen = {};
    (title.toLowerCase().match(/[a-z0-9]+/g) || []).forEach(function (w) {
      if (w.length > 2 && !STOP[w]) seen[w] = 1;
    });
    return Object.keys(seen);
  }
  function similar(a, b) {
    if (!a.length || !b.length) return false;
    var shared = a.filter(function (w) { return b.indexOf(w) > -1; }).length;
    return shared / Math.min(a.length, b.length) >= 0.45;
  }
  function groupStories(rows) {
    var stories = [];
    rows.forEach(function (r) {
      r.others = [];
      if (!isOfficial(r.item)) {
        r.tokens = tokens(r.item.t);
        for (var i = 0; i < stories.length; i++) {
          var s = stories[i];
          if (isOfficial(s.item)) continue;
          var outlets = [s.item.o].concat(s.others.map(function (o) { return o.o; }));
          if (outlets.indexOf(r.item.o) === -1 && similar(r.tokens, s.tokens)) {
            s.others.push(r.item);
            r.why.forEach(function (w) { if (s.why.indexOf(w) === -1) s.why.push(w); });
            return;
          }
        }
      }
      stories.push(r);
    });
    return stories;
  }

  // ---------------------------------------------------------------- rendering
  function sourceLabel(it) {
    if (it.k === "govuk") return "GOV.UK announcement";
    if (it.k === "written_statement") {
      var m = /\((.+)\)/.exec(it.o);
      return "Written ministerial statement" + (m ? ", " + m[1] : "");
    }
    return it.o;
  }
  function also(others) {
    if (!others || !others.length) return "";
    return "; also " + others.map(function (o) {
      var href = /^https?:\/\//.test(o.u) ? o.u : "#";
      return '<a href="' + esc(href) + '" target="_blank" rel="noopener">' + esc(o.o) + "</a>";
    }).join(", ");
  }
  function row(r, inDayGroup) {
    var it = r.item;
    var href = /^https?:\/\//.test(it.u) ? it.u : "#";
    return '<li class="item"><div class="gutter">' + esc(gutter(it, inDayGroup)) + "</div><div>" +
      '<a class="headline" href="' + esc(href) + '" target="_blank" rel="noopener">' + esc(it.t) + "</a>" +
      '<p class="source">' + esc(sourceLabel(it)) + also(r.others) + "</p>" +
      (it.s ? '<p class="summary">' + esc(it.s) + "</p>" : "") +
      '<p class="filed">Matched ' + esc(r.why.join(", ")) + "</p></div></li>";
  }
  function results(rows) {
    if (!rows.length) return '<p class="empty">Nothing in the last three days matches this channel yet.</p>';
    var shown = rows.slice(0, MAX_ITEMS), html = "";
    var official = shown.filter(function (r) { return isOfficial(r.item); });
    var news = shown.filter(function (r) { return !isOfficial(r.item); });
    if (official.length) {
      html += '<div class="group official"><h3>Official</h3><ol class="items">' +
        official.map(function (r) { return row(r, false); }).join("") + "</ol></div>";
    }
    var groups = [];
    news.forEach(function (r) {
      var k = itemDay(r.item), label = k ? dayLabel(k) : "Undated";
      if (groups.length && groups[groups.length - 1].label === label) groups[groups.length - 1].rows.push(r);
      else groups.push({ label: label, rows: [r] });
    });
    groups.forEach(function (g) {
      html += '<div class="group"><h3>' + esc(g.label) + '</h3><ol class="items">' +
        g.rows.map(function (r) { return row(r, true); }).join("") + "</ol></div>";
    });
    if (rows.length > MAX_ITEMS) {
      html += '<p class="empty">Showing the newest ' + MAX_ITEMS + " of " + rows.length +
        " items. Narrow the channel to see fewer.</p>";
    }
    return html;
  }
  function sourcesSummary(c) {
    var parts = [];
    if (c.depts.length) parts.push("<b>Departments</b> " + esc(c.depts.map(function (d) { return deptNames[d]; }).join(", ")));
    if (c.tags.length) parts.push("<b>Tags</b> " + esc(c.tags.map(function (g) { return tagNames[g]; }).join(", ")));
    if (c.mine.length) parts.push("<b>Your tags</b> " + esc(c.mine.map(function (t) { return t.name; }).join(", ")));
    return parts.join("<br>");
  }
  function switcher(activeId) {
    if (!state.channels.length) return "";
    return '<nav class="chan-switch" aria-label="Your channels">' + state.channels.map(function (c) {
      return '<a href="#c=' + esc(c.id) + '"' + (c.id === activeId ? ' aria-current="page"' : "") + ">" +
        esc(c.name) + "</a>";
    }).join("") + '<a class="chan-new" href="#new">New channel</a></nav>';
  }

  var currentView = 0;
  function viewChannel(c, shared) {
    var view = ++currentView;
    startLive(c, function () {
      if (view !== currentView) return;  // reader has moved on
      var y = window.scrollY;
      viewChannel(c, shared);
      window.scrollTo(0, y);
    });
    var rows = run(c);
    var banner = shared
      ? '<div class="chan-banner"><p>Someone shared this channel with you. Save it to keep it in this browser.</p>' +
        '<button type="button" class="btn primary" data-act="keep">Save to my channels</button></div>'
      : "";
    var actions = shared ? "" :
      '<div class="chan-actions"><button type="button" class="btn" data-act="edit">Edit channel</button>' +
      '<button type="button" class="btn" data-act="share">Copy share link</button>' +
      '<span class="chan-status" role="status"></span></div>';
    root.innerHTML = (shared ? "" : switcher(c.id)) + banner +
      '<div class="dept-head"><div><h1 class="page-title">' + esc(c.name) + "</h1>" +
      '<p class="page-sub">' + plural(rows.length, "story", "stories") + " in the last three days.</p>" +
      '<p class="chan-sources">' + sourcesSummary(c) + "</p>" + liveStatus(c) + "</div></div>" + actions + results(rows);

    root.onclick = function (e) {
      var act = e.target.getAttribute && e.target.getAttribute("data-act");
      if (act === "edit") editor(c);
      if (act === "keep") {
        state.channels.push(c);
        var ok = save();
        location.hash = "c=" + c.id;
        if (!ok) alert("This browser isn't allowing the site to save data, so the channel will be lost when you leave.");
      }
      if (act === "share") share(c);
    };
  }

  function share(c) {
    var link = location.origin + location.pathname + "#share=" + encodeChannel(c);
    var status = root.querySelector(".chan-status");
    function shown(msg) { status.textContent = msg; }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(link).then(function () { shown("Link copied. Anyone with it sees this channel."); },
        function () { window.prompt("Copy this link:", link); });
    } else {
      window.prompt("Copy this link:", link);
    }
  }

  // ---------------------------------------------------------------- editor
  function editor(existing) {
    currentView++;  // stop pending live searches re-rendering over the form
    var c = existing ? JSON.parse(JSON.stringify(existing)) : { id: uid(), name: "", depts: [], tags: [], mine: [] };
    var isNew = !existing;

    function checks(list, chosen, group) {
      return list.map(function (x) {
        var id = group + "-" + x.slug;
        return '<label class="check" for="' + id + '"><input type="checkbox" id="' + id + '" name="' + group +
          '" value="' + esc(x.slug) + '"' + (chosen.indexOf(x.slug) > -1 ? " checked" : "") + "> " + esc(x.name) + "</label>";
      }).join("");
    }
    function mineList() {
      if (!c.mine.length) return '<p class="hint">None yet.</p>';
      return '<ul class="mine">' + c.mine.map(function (t, i) {
        return "<li><span><b>" + esc(t.name) + "</b> " + esc(t.terms.join(", ")) +
          (t.exclude.length ? " <i>excluding " + esc(t.exclude.join(", ")) + "</i>" : "") + "</span>" +
          '<button type="button" class="btn small" data-remove="' + i + '">Remove</button></li>';
      }).join("") + "</ul>";
    }

    function render() {
      root.innerHTML = switcher(isNew ? null : c.id) +
        '<h1 class="page-title">' + (isNew ? "New channel" : "Edit channel") + "</h1>" +
        '<p class="page-sub">Choose any departments and tags. The channel shows everything that matches any of them.</p>' +
        '<form class="editor" novalidate>' +
        '<label class="field"><span>Channel name</span><input name="cname" maxlength="80" required value="' + esc(c.name) +
        '" placeholder="For example: Justice and policing"></label>' +
        '<fieldset><legend>Departments</legend><div class="checks">' + checks(DATA.departments, c.depts, "dept") + "</div></fieldset>" +
        (DATA.tags.length ? '<fieldset><legend>Tags</legend><div class="checks">' + checks(DATA.tags, c.tags, "tag") + "</div></fieldset>" : "") +
        '<fieldset><legend>Your own tags</legend>' +
        '<p class="hint">Search for any words in headlines and summaries. Separate terms with commas; add * for any ending (sanction* finds sanctions). ' + (SEARCH_URL
          ? "They search everything this site has collected, plus a live search of UK outlets."
          : "They search what this site has collected.") + "</p>" +
        mineList() +
        '<div class="mine-add"><label class="field"><span>Tag name</span><input name="tname" maxlength="60" placeholder="Sanctions"></label>' +
        '<label class="field"><span>Terms</span><input name="tterms" placeholder="sanction*, asset freeze, OFSI"></label>' +
        '<label class="field"><span>Leave out stories mentioning <i>(optional)</i></span><input name="texcl" placeholder="sport"></label>' +
        '<button type="button" class="btn" data-act="addtag">Add tag</button></div></fieldset>' +
        '<p class="form-error" role="alert"></p>' +
        '<div class="chan-actions"><button type="submit" class="btn primary">Save channel</button>' +
        '<button type="button" class="btn" data-act="cancel">Cancel</button>' +
        (isNew ? "" : '<button type="button" class="btn danger" data-act="delete">Delete channel</button>') + "</div></form>";
      var form = root.querySelector("form");
      form.onsubmit = submit;
    }

    function readChecks(form) {
      var f = form.elements;
      c.name = f.cname.value.trim();
      c.depts = [].map.call(form.querySelectorAll('input[name="dept"]:checked'), function (i) { return i.value; });
      c.tags = [].map.call(form.querySelectorAll('input[name="tag"]:checked'), function (i) { return i.value; });
    }
    function error(msg) { root.querySelector(".form-error").textContent = msg; }

    function submit(e) {
      e.preventDefault();
      var form = e.target;
      readChecks(form);
      if (form.elements.tname.value.trim() || form.elements.tterms.value.trim()) addTag(form, true);
      if (!c.name) return error("Give the channel a name.");
      if (!c.depts.length && !c.tags.length && !c.mine.length) return error("Choose at least one department or tag.");
      var i = state.channels.findIndex(function (x) { return x.id === c.id; });
      if (i > -1) state.channels[i] = c; else state.channels.push(c);
      if (!save()) alert("This browser isn't allowing the site to save data, so the channel will be lost when you leave.");
      location.hash = "c=" + c.id;
      route();
    }

    function addTag(form, quiet) {
      var f = form.elements, name = f.tname.value.trim(), terms = splitTerms(f.tterms.value);
      if (!name || !terms.length) { if (!quiet) error("A tag needs a name and at least one term."); return; }
      c.mine.push({ name: name, terms: terms, exclude: splitTerms(f.texcl.value) });
    }

    root.onclick = function (e) {
      var t = e.target, form = root.querySelector("form");
      if (!form) return;
      var act = t.getAttribute("data-act"), rm = t.getAttribute("data-remove");
      if (act === "addtag") { readChecks(form); addTag(form, false); render(); }
      if (rm !== null) { readChecks(form); c.mine.splice(+rm, 1); render(); }
      if (act === "cancel") { location.hash = isNew ? "" : "c=" + c.id; route(); }
      if (act === "delete" && window.confirm("Delete the channel \u201c" + c.name + "\u201d? This can't be undone.")) {
        state.channels = state.channels.filter(function (x) { return x.id !== c.id; });
        save();
        location.hash = "";
        route();
      }
    };
    render();
  }

  // ---------------------------------------------------------------- routing
  function route() {
    var h = location.hash.slice(1);
    if (h.indexOf("share=") === 0) {
      var shared = decodeChannel(h.slice(6));
      if (shared) return viewChannel(shared, true);
    }
    if (h === "new") return editor(null);
    var id = h.indexOf("c=") === 0 ? h.slice(2) : null;
    var c = state.channels.filter(function (x) { return x.id === id; })[0] || state.channels[0];
    if (c) return viewChannel(c, false);
    intro();
  }

  function intro() {
    currentView++;
    root.innerHTML = '<h1 class="page-title">Your channels</h1>' +
      '<p class="page-sub">A channel is your own feed: any mix of departments' + (DATA.tags.length ? ", team tags" : "") +
      " and tags you write yourself, such as Ministry of Justice + Home Office + your own tag for a topic you follow.</p>" +
      '<p class="page-sub">Channels are saved in this browser. Share one with colleagues by sending its link.</p>' +
      '<p><a class="btn primary" href="#new">Create a channel</a></p>';
    root.onclick = null;
  }

  window.addEventListener("hashchange", route);
  route();
})();
