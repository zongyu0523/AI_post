"""observe → act over a Playwright page, with refs that are born from each snapshot.

This is the substrate the browser agent works on, and the reason the Facebook channel cannot be a
list of hardcoded selectors. From an earlier project:
and the reason is worth stating:

    browser 的 `[ref=eN]` 是每次 snapshot 後才動態產生、上一輪的 ref 下一輪就過期
    —— browser work can never be decomposed into deterministic tool tasks

So every snapshot re-stamps `data-aipost-ref` from scratch and the previous generation stops
resolving. An act against a stale ref fails loudly instead of clicking whatever inherited that id.

Two behaviours are baked in here rather than left to the model:

  * **When a dialog is open, the snapshot is scoped to that dialog.** Facebook's composer is a
    modal over a feed of hundreds of controls; without scoping, the outline is mostly noise and
    the model picks something behind the modal.
  * **Uploading never clicks the site's upload button.** Files go straight onto the dialog's own
    `input[type=file]`, so no OS file dialog is ever opened.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

REF_ATTR = "data-aipost-ref"
DIALOG_ATTR = "data-aipost-dialog"

# The ONE definition of "which dialog is the model looking at", shared by the snapshot, the
# scroller and the uploader. It used to be hand-copied into all three, and every copy said
# `dialogs[dialogs.length - 1]` - so one wrong idea was wrong in three places at once, which is
# Two hand-written copies of one rule always diverge - here the lists were three lines of JS.
_DIALOG_JS = r"""
  const dialogShowing = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const s = getComputedStyle(el);
    return !(s.visibility === 'hidden' || s.display === 'none' || Number(s.opacity) === 0);
  };
  // Facebook marks a composer's own TITLE BAR role="dialog" as well, nested inside the composer.
  // So the last dialog in document order was a 60px strip holding a heading and a close button,
  // while the editor 449px below it never entered the outline at all - and the model looked and
  // scrolled for an input box it had never once been shown.
  //
  // Scoping outward cannot lose anything: a dialog contains whatever its nested dialogs contain.
  // So nested ones are dropped and the last of the rest wins, which still picks a menu that was
  // portalled out as a SIBLING and genuinely is on top of the composer.
  const openDialogs = () => {
    const open = [...document.querySelectorAll('[role="dialog"]')].filter(dialogShowing);
    return open.filter(d => !open.some(o => o !== d && o.contains(d)));
  };
"""

_SNAPSHOT_TMPL = r"""
(limit) => {
  const ATTR = 'data-aipost-ref';
  document.querySelectorAll('[' + ATTR + ']').forEach(e => e.removeAttribute(ATTR));

  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none' || Number(s.opacity) === 0) return false;
    if (r.bottom < -200 || r.top > innerHeight + 1200) return false;
    return true;
  };

  // Scope to the open dialog when there is one: a composer modal over a long feed otherwise
  // drowns in the page behind it.
  //<<DIALOG>>
  const dialogs = openDialogs();
  const root = dialogs.length ? dialogs[dialogs.length - 1] : document.body;

  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, 120);

  // Roles a person can act on; these are the ones that get a ref.
  const INTERACTIVE = new Set(['button', 'link', 'textbox', 'searchbox', 'checkbox', 'radio',
    'combobox', 'listbox', 'option', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'slider',
    'spinbutton', 'switch', 'tab', 'treeitem']);
  // Roles worth a line of their own when they carry a name, so the tree keeps its landmarks.
  const CONTENT = new Set(['heading', 'img', 'cell', 'columnheader', 'rowheader', 'listitem',
    'article', 'dialog', 'alert', 'status']);

  const roleOf = (el) => {
    const explicit = (el.getAttribute('role') || '').toLowerCase();
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return el.hasAttribute('href') ? 'link' : 'generic';
    if (tag === 'button') return 'button';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'select') return 'combobox';
    if (tag === 'img') return 'img';
    if (/^h[1-6]$/.test(tag)) return 'heading';
    if (tag === 'input') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'checkbox' || type === 'radio') return type;
      if (type === 'submit' || type === 'button' || type === 'reset') return 'button';
      if (type === 'search') return 'searchbox';
      return 'textbox';
    }
    if (el.isContentEditable) return 'textbox';
    return 'generic';
  };

  // What a screen reader would read out, as far as a page script can tell.
  const labelled = (el) => {
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const t = by.split(/\s+/).map(id => document.getElementById(id))
                  .filter(Boolean).map(node => node.innerText || '').join(' ');
      if (t.trim()) return t;
    }
    if (el.id) {
      const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lab) return lab.innerText || '';
    }
    const wrap = el.closest('label');
    return wrap ? (wrap.innerText || '') : '';
  };

  // A toggle with no words of its own: they are in the row drawn around it. The tree shows that
  // row anyway, so this is only here so CODE can match an option by name (see _set_audience).
  const rowWords = (el) => {
    let node = el.parentElement;
    for (let i = 0; i < 4 && node && node !== root; i++, node = node.parentElement) {
      const t = clean(node.innerText);
      if (t) return t;
    }
    return '';
  };

  const lines = [];
  const elements = [];
  let n = 0;
  let truncated = false;
  let lastName = '';          // to drop a text node that merely repeats the line above it

  const emit = (depth, text) => {
    if (lines.length >= limit) { truncated = true; return false; }
    lines.push('  '.repeat(Math.min(depth, 12)) + '- ' + text);
    return true;
  };

  const walk = (node, depth) => {
    if (truncated) return;
    for (const child of node.childNodes) {
      if (truncated) return;

      if (child.nodeType === 3) {                       // a text node
        const text = clean(child.nodeValue);
        // Skip text that is already printed as the previous line's name: the words of a button
        // are its name, not a separate line, and a row's words name its control.
        if (text && text !== lastName && !(lastName && lastName.includes(text))) {
          emit(depth, 'text: ' + text);
          lastName = '';
        }
        continue;
      }
      if (child.nodeType !== 1) continue;
      const tag = child.tagName.toLowerCase();
      if (tag === 'script' || tag === 'style' || tag === 'noscript') continue;
      if (child.getAttribute && child.getAttribute('aria-hidden') === 'true') continue;
      if (!visible(child)) continue;

      const role = roleOf(child);
      if (role === 'hidden') continue;
      const toggle = ['radio', 'checkbox', 'switch', 'menuitemradio', 'menuitemcheckbox']
                       .includes(role);
      const own = clean(child.getAttribute('aria-label') || child.getAttribute('placeholder') ||
                        child.getAttribute('alt') || child.getAttribute('title'));
      let name = own || clean(labelled(child));
      const interactive = INTERACTIVE.has(role);
      if (!name && interactive && !toggle) name = clean(child.innerText);
      if (!name && toggle) name = rowWords(child);

      const value = clean(child.isContentEditable ? child.innerText
                                                  : (toggle ? '' : (child.value || '')));

      let checked = null;
      const aria = child.getAttribute('aria-checked') || child.getAttribute('aria-selected');
      if (aria === 'true') checked = true;
      else if (aria === 'false') checked = false;
      else if (child.type === 'radio' || child.type === 'checkbox') checked = !!child.checked;

      const keep = interactive || (name && CONTENT.has(role));
      let childDepth = depth;
      if (keep) {
        let line = role;
        if (name) line += ' "' + name + '"';
        if (value && value !== name) line += ' (value: "' + value.slice(0, 120) + '")';
        if (checked === true) line += ' [SELECTED]';
        else if (checked === false) line += ' [not selected]';
        const disabled = child.getAttribute('aria-disabled') === 'true' || child.disabled;
        if (disabled) line += ' [disabled]';

        let ref = '';
        if (interactive) {
          ref = 'e' + (++n);
          child.setAttribute(ATTR, ref);
          line += ' [ref=' + ref + ']';
        }
        if (!emit(depth, line)) return;
        lastName = name;
        childDepth = depth + 1;
        if (ref) {
          const item = { ref, role, name };
          if (value && value !== name) item.value = value.slice(0, 120);
          if (checked !== null) item.checked = checked;
          if (disabled) item.disabled = true;
          elements.push(item);
        }
        // A CONTROL's own text is its name; do not walk into it and print it twice. Only a
        // control, though: the composer's title bar is a role=dialog whose innerText is exactly
        // its label, and pruning it threw away the close button nested inside. The text-node
        // dedup above already drops a repeat of the line we just emitted, so walking in is safe.
        if (interactive && name && clean(child.innerText) === name) continue;
      }
      walk(child, childDepth);
    }
  };

  walk(root, 0);

  return {
    url: location.href,
    title: document.title,
    dialog: dialogs.length > 0,
    // The composer itself carries no aria-label - the words 「建立貼文」 are on its nested title
    // bar - so the header line reads that if the dialog we scoped to has nothing of its own.
    dialogName: dialogs.length ? (() => {
      const d = dialogs[dialogs.length - 1];
      const own = clean(d.getAttribute('aria-label'));
      if (own) return own;
      for (const inner of d.querySelectorAll('[role="dialog"][aria-label]')) {
        const t = clean(inner.getAttribute('aria-label'));
        if (t) return t;
      }
      return '';
    })() : '',
    truncated,
    lines,
    elements,
  };
}
"""

_SCROLL_TMPL = r"""
([ref, dir]) => {
  //<<DIALOG>>
  const ATTR = 'data-aipost-ref';
  const scrollable = (el) => {
    if (!el || el === document.body || el === document.documentElement) return false;
    const s = getComputedStyle(el);
    return el.scrollHeight > el.clientHeight + 4 && /(auto|scroll|overlay)/.test(s.overflowY);
  };

  // Scroll the list the model is actually looking at, not the feed behind the modal.
  let box = null;
  const from = ref ? document.querySelector('[' + ATTR + '="' + ref + '"]') : null;
  for (let el = from; el && !box; el = el.parentElement) if (scrollable(el)) box = el;
  if (!box) {
    // Unfiltered before, so this could land on an offscreen 通知 / Messenger dialog, and then on
    // the composer's title bar - which scrolls nothing, so it fell through to the feed BEHIND the
    // modal. The model scrolled, the modal did not move, and the outline came back identical.
    const dialogs = openDialogs();
    if (dialogs.length) {
      const d = dialogs[dialogs.length - 1];
      box = scrollable(d) ? d : [...d.querySelectorAll('*')].find(scrollable) || null;
    }
  }

  const target = box || document.scrollingElement || document.documentElement;
  const page = box ? box.clientHeight : innerHeight;
  const before = target.scrollTop;
  target.scrollTop = before + dir * Math.max(200, Math.round(page * 0.8));
  return { moved: target.scrollTop - before, inList: !!box };
}
"""

_PAGE_TEXT_JS = r"""
() => (document.body ? document.body.innerText : '').replace(/\s+/g, ' ').trim()
"""

# Upload needs the same dialog as a Playwright locator, so it stamps it and locates by attribute.
_MARK_DIALOG_TMPL = r"""
(attr) => {
  //<<DIALOG>>
  document.querySelectorAll('[' + attr + ']').forEach(e => e.removeAttribute(attr));
  const open = openDialogs();
  if (!open.length) return false;
  open[open.length - 1].setAttribute(attr, '1');
  return true;
}
"""


def _with_dialog_rule(js: str) -> str:
    """Splice the shared dialog rule in, and refuse to produce JS that silently lost it."""
    if "//<<DIALOG>>" not in js:
        raise AssertionError("this JS no longer takes the shared dialog rule")
    return js.replace("//<<DIALOG>>", _DIALOG_JS)


_SNAPSHOT_JS = _with_dialog_rule(_SNAPSHOT_TMPL)
_SCROLL_JS = _with_dialog_rule(_SCROLL_TMPL)
_MARK_DIALOG_JS = _with_dialog_rule(_MARK_DIALOG_TMPL)


@dataclass(frozen=True)
class Snapshot:
    url: str
    title: str
    dialog: bool
    dialog_name: str
    elements: tuple[dict, ...]
    lines: tuple[str, ...] = ()
    truncated: bool = False

    def outline(self) -> str:
        """What the model reads: an indented role tree with the page's own text left in.

        The text matters as much as the controls. Facebook's audience options have no accessible
        name at all - not even in Chrome's own tree - and the only thing that tells them apart is
        the line of text sitting beside each one.
        """
        head = f"url: {self.url}\ntitle: {self.title}"
        if self.dialog:
            head += f"\n[a dialog is open{(' - ' + self.dialog_name) if self.dialog_name else ''};"\
                    " the tree below is INSIDE it]"
        body = list(self.lines) or ["(nothing readable on screen)"]
        if self.truncated:
            body.append(f"[...cut off at {len(self.lines)} lines - scroll to reach the rest]")
        return head + "\n" + "\n".join(body)

    def find(self, ref: str) -> dict | None:
        return next((e for e in self.elements if e["ref"] == ref), None)


class StaleRef(RuntimeError):
    """The ref is not in the current generation. Never a reason to guess another one."""


async def snapshot(page, limit: int = 220) -> Snapshot:
    data = await page.evaluate(_SNAPSHOT_JS, limit)
    return Snapshot(url=data["url"], title=data["title"], dialog=data["dialog"],
                    dialog_name=data.get("dialogName", ""),
                    truncated=bool(data.get("truncated")),
                    lines=tuple(data.get("lines") or ()),
                    elements=tuple(data["elements"]))


def _loc(page, ref: str):
    return page.locator(f'[{REF_ATTR}="{ref}"]')


async def click(page, ref: str, timeout: int = 15_000) -> None:
    loc = _loc(page, ref)
    if await loc.count() == 0:
        raise StaleRef(f"{ref} is not in the current outline - refs from the previous turn have expired. Snapshot again.")
    await loc.first.click(timeout=timeout)


async def fill(page, ref: str, text: str, timeout: int = 15_000) -> None:
    """`fill` goes through CDP Input.insertText - a trusted beforeinput/input, which is the only
    thing a Lexical/Draft-class editor accepts. Writing .textContent looks right and vanishes on
    the next render."""
    loc = _loc(page, ref)
    if await loc.count() == 0:
        raise StaleRef(f"{ref} is not in the current outline. Snapshot again.")
    await loc.first.click(timeout=timeout)
    await loc.first.fill(text, timeout=timeout)


async def upload(page, paths: list[str], timeout: int = 30_000) -> int:
    """Set files directly on a file input. Never clicks the site's own upload button, so no OS
    dialog is ever opened."""
    # `get_by_role("dialog").last` was the composer's title bar, which holds no file input, so
    # this always fell through to a page-wide search and could have set files on some unrelated
    # input. Same rule as the outline the model was shown, so they cannot disagree.
    target = page.locator('input[type="file"]')
    if await page.evaluate(_MARK_DIALOG_JS, DIALOG_ATTR):
        scoped = page.locator(f'[{DIALOG_ATTR}] input[type="file"]')
        if await scoped.count() > 0:
            target = scoped
    if await target.count() == 0:
        raise RuntimeError("No input[type=file] on the page. The site may only mount one after 「照片/影片」 is opened.")
    await target.first.set_input_files(paths, timeout=timeout)
    return len(paths)


async def scroll(page, ref: str | None = None, down: bool = True) -> dict:
    """Scroll the list containing `ref` (or the page). Returns how far it actually moved, so the
    caller can tell the model "nothing moved" rather than letting it scroll a wall forever."""
    return await page.evaluate(_SCROLL_JS, [ref or None, 1 if down else -1])


async def page_text(page) -> str:
    return await page.evaluate(_PAGE_TEXT_JS)


_POST_IDS_TMPL = r"""
() => {
  //<<DIALOG>>
  // Server-generated ids carried in the page's own links. The DOM around a post is deliberately
  // unreadable - measured on a live group: no role="article" anywhere, obfuscated class names, a
  // timestamp split across decoy spans - but these ids are real and stable. Four photos of one
  // post all carry the same `set=pcb.<id>`, and that id is the same on every page showing it.
  const PATTERNS = [/[?&]set=pcb\.(\d+)/, /[?&]fbid=(\d+)/, /\/posts\/(pfbid[A-Za-z0-9]+)/,
                    /\/posts\/(\d+)/, /[?&]story_fbid=(\d+)/, /[?&]multi_permalinks=(\d+)/,
                    /\/(pfbid[A-Za-z0-9]{20,})/];
  const shut = [...document.querySelectorAll('[role="dialog"]')].filter(d => !dialogShowing(d));
  const out = {};
  for (const a of document.querySelectorAll('a[href]')) {
    // The notification and Messenger panels sit in the DOM of every Facebook page, full of OTHER
    // people's post ids, and their contents change between loads - so a before/after diff would
    // read them as posts of ours. They are closed dialogs, which is how they get dropped.
    if (shut.some(d => d.contains(a))) continue;
    if (!a.getClientRects().length) continue;
    const h = a.href || a.getAttribute('href') || '';
    for (const p of PATTERNS) {
      const m = h.match(p);
      if (m && !out[m[1]]) out[m[1]] = h.slice(0, 300);
    }
  }
  return out;
}
"""

_POST_IDS_JS = _with_dialog_rule(_POST_IDS_TMPL)


async def post_ids(page) -> dict[str, str]:
    """Facebook's own ids for the posts on this page, each with a link that carries it.

    This replaces reading post blocks out of the DOM. Measured on the live site: `role="article"`
    is absent from a group page entirely and empty on a profile, so everything built on it only
    ever worked against fixtures written to match it.
    """
    try:
        return await page.evaluate(_POST_IDS_JS)
    except Exception:
        return {}
