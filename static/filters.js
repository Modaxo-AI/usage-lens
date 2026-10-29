/* ===========================================================================
   MultiSelect — a searchable, keyboard-operable multi-select.

   Replaces `<select multiple>`, which required ctrl+click, offered no search,
   truncated long project paths, and gave no readable indication of what was
   selected.

   Usage:
     const ms = new MultiSelect(mountEl, {
       label: 'Project', allLabel: 'All projects', noun: 'project',
       options: [{value, label, title}],
       onChange: values => { ... },
     });
     ms.getValues(); ms.setValues([...]); ms.clear();

   Keyboard: Enter/Space/ArrowDown opens · ArrowUp/Down moves · Enter/Space
   toggles · Escape closes and restores focus · Tab closes · typing filters.
   =========================================================================== */
(function (global) {
  'use strict';

  let uid = 0;

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  class MultiSelect {
    constructor(mount, opts) {
      this.id = 'ms-' + (++uid);
      this.mount = mount;
      this.allLabel = opts.allLabel || 'All';
      this.noun = opts.noun || 'item';
      this.onChange = opts.onChange || function () {};
      this.options = [];
      this.selected = new Set();
      this.open = false;
      this.cursor = -1;
      this.query = '';
      this._build(opts.label || '');
      this.setOptions(opts.options || []);
    }

    // ---------- DOM ----------
    _build(label) {
      this.root = el('div', 'ms');
      this.root.setAttribute('role', 'group');
      if (label) this.root.setAttribute('aria-label', label);

      this.trigger = el('button', 'ms-trigger');
      this.trigger.type = 'button';
      this.trigger.setAttribute('aria-expanded', 'false');
      this.trigger.setAttribute('aria-haspopup', 'listbox');
      this.summaryEl = el('span', 'ms-summary');
      this.countEl = el('span', 'ms-count');
      this.countEl.hidden = true;
      this.caretEl = el('span', 'ms-caret', '▾');
      this.caretEl.setAttribute('aria-hidden', 'true');
      this.trigger.append(this.summaryEl, this.countEl, this.caretEl);

      this.panel = el('div', 'ms-panel');
      this.panel.hidden = true;

      const searchRow = el('div', 'ms-search-row');
      this.search = el('input', 'ms-search');
      this.search.type = 'search';
      this.search.placeholder = 'Search ' + this.noun + 's…';
      this.search.setAttribute('aria-label', 'Search ' + this.noun + 's');
      this.search.setAttribute('autocomplete', 'off');
      searchRow.append(this.search);

      this.list = el('ul', 'ms-list');
      this.list.id = this.id + '-list';
      this.list.setAttribute('role', 'listbox');
      this.list.setAttribute('aria-multiselectable', 'true');
      this.trigger.setAttribute('aria-controls', this.list.id);

      const foot = el('div', 'ms-foot');
      this.selectAllBtn = el('button', null, 'Select all');
      this.selectAllBtn.type = 'button';
      this.clearBtn = el('button', null, 'Clear');
      this.clearBtn.type = 'button';
      this.hintEl = el('span', 'ms-hint');
      foot.append(this.selectAllBtn, this.clearBtn, this.hintEl);

      this.panel.append(searchRow, this.list, foot);
      this.root.append(this.trigger, this.panel);
      this.mount.append(this.root);

      this._wire();
    }

    _wire() {
      this.trigger.addEventListener('click', () => this.toggle());
      this.trigger.addEventListener('keydown', e => {
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); this.openPanel(); }
      });

      this.search.addEventListener('input', () => {
        this.query = this.search.value.trim().toLowerCase();
        this.cursor = -1;
        this._renderList();
      });
      this.search.addEventListener('keydown', e => this._onPanelKey(e));
      this.list.addEventListener('keydown', e => this._onPanelKey(e));

      this.selectAllBtn.addEventListener('click', () => {
        // Selects what the current search shows, not the whole list, so it
        // composes with the search box instead of fighting it.
        this._visible().forEach(o => this.selected.add(o.value));
        this._afterChange();
      });
      this.clearBtn.addEventListener('click', () => {
        this.selected.clear();
        this._afterChange();
        this.search.focus();
      });

      // Close on outside click / focus leaving the widget entirely.
      this._onDocDown = e => {
        if (this.open && !this.root.contains(e.target)) this.closePanel(false);
      };
      document.addEventListener('mousedown', this._onDocDown);
    }

    _onPanelKey(e) {
      const vis = this._visible();
      if (e.key === 'Escape') {
        e.preventDefault(); e.stopPropagation();
        this.closePanel(true);
      } else if (e.key === 'ArrowDown') {
        e.preventDefault();
        this.cursor = vis.length ? (this.cursor + 1) % vis.length : -1;
        this._paintCursor();
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        this.cursor = vis.length ? (this.cursor - 1 + vis.length) % vis.length : -1;
        this._paintCursor();
      } else if (e.key === 'Enter' || (e.key === ' ' && e.target !== this.search)) {
        if (this.cursor >= 0 && vis[this.cursor]) {
          e.preventDefault();
          this._toggleValue(vis[this.cursor].value);
        }
      } else if (e.key === 'Home') {
        e.preventDefault(); this.cursor = 0; this._paintCursor();
      } else if (e.key === 'End') {
        e.preventDefault(); this.cursor = vis.length - 1; this._paintCursor();
      } else if (e.key === 'Tab') {
        this.closePanel(false);
      }
    }

    // ---------- state ----------
    setOptions(options) {
      this.options = options.map(o =>
        typeof o === 'string' ? { value: o, label: o, title: o } : o);
      const valid = new Set(this.options.map(o => o.value));
      // Drop selections that no longer exist (e.g. after a Refresh).
      let dropped = false;
      this.selected.forEach(v => { if (!valid.has(v)) { this.selected.delete(v); dropped = true; } });
      this._render();
      if (dropped) this.onChange(this.getValues());
    }

    getValues() { return Array.from(this.selected); }

    setValues(values) {
      this.selected = new Set(values || []);
      this._render();
    }

    clear() {
      if (!this.selected.size) return;
      this.selected.clear();
      this._afterChange();
    }

    _toggleValue(value) {
      if (this.selected.has(value)) this.selected.delete(value);
      else this.selected.add(value);
      this._afterChange();
    }

    _afterChange() {
      this._render();
      this.onChange(this.getValues());
    }

    // ---------- open/close ----------
    toggle() { this.open ? this.closePanel(true) : this.openPanel(); }

    openPanel() {
      if (this.open) return;
      // Only one panel open at a time.
      MultiSelect._openInstances.forEach(i => { if (i !== this) i.closePanel(false); });
      this.open = true;
      this.panel.hidden = false;
      this.trigger.setAttribute('aria-expanded', 'true');
      this.search.value = '';
      this.query = '';
      this.cursor = -1;
      this._renderList();
      this.search.focus();
    }

    closePanel(refocus) {
      if (!this.open) return;
      this.open = false;
      this.panel.hidden = true;
      this.trigger.setAttribute('aria-expanded', 'false');
      if (refocus) this.trigger.focus();
    }

    // ---------- rendering ----------
    _visible() {
      if (!this.query) return this.options;
      return this.options.filter(o =>
        o.label.toLowerCase().includes(this.query) ||
        String(o.value).toLowerCase().includes(this.query));
    }

    _render() {
      const n = this.selected.size;
      const isEmpty = n === 0;
      this.trigger.classList.toggle('is-empty', isEmpty);
      this.countEl.hidden = isEmpty;
      if (isEmpty) {
        this.summaryEl.textContent = this.allLabel;
        this.trigger.title = this.allLabel;
      } else if (n === 1) {
        const only = this.options.find(o => o.value === this.getValues()[0]);
        const text = only ? only.label : this.getValues()[0];
        this.summaryEl.textContent = text;
        this.trigger.title = (only && only.title) || text;
        this.countEl.textContent = '1';
      } else {
        this.summaryEl.textContent = n + ' ' + this.noun + 's';
        this.trigger.title = this.getValues()
          .map(v => { const o = this.options.find(x => x.value === v); return (o && o.title) || v; })
          .join('\n');
        this.countEl.textContent = String(n);
      }
      if (this.open) this._renderList();
    }

    _renderList() {
      const vis = this._visible();
      this.list.textContent = '';
      if (!vis.length) {
        const empty = el('li', 'ms-empty', 'No ' + this.noun + ' matches “' + this.search.value + '”');
        this.list.append(empty);
      } else {
        vis.forEach((o, i) => {
          const li = el('li', 'ms-opt');
          li.setAttribute('role', 'option');
          const on = this.selected.has(o.value);
          li.setAttribute('aria-selected', on ? 'true' : 'false');
          if (i === this.cursor) li.classList.add('cursor');
          const cb = el('input');
          cb.type = 'checkbox';
          cb.checked = on;
          cb.tabIndex = -1;
          cb.setAttribute('aria-hidden', 'true');
          const text = el('span', 'ms-opt-text', o.label);
          if (o.title && o.title !== o.label) li.title = o.title;
          li.append(cb, text);
          li.addEventListener('click', e => {
            e.preventDefault();
            this.cursor = i;
            this._toggleValue(o.value);
          });
          this.list.append(li);
        });
      }
      const allVisibleOn = vis.length > 0 && vis.every(o => this.selected.has(o.value));
      this.selectAllBtn.disabled = allVisibleOn;
      this.selectAllBtn.textContent = this.query ? 'Select these' : 'Select all';
      this.clearBtn.disabled = this.selected.size === 0;
      this.hintEl.textContent = this.selected.size
        ? this.selected.size + ' of ' + this.options.length + ' selected'
        : this.options.length + ' available';
    }

    _paintCursor() {
      const items = this.list.querySelectorAll('.ms-opt');
      items.forEach((n, i) => n.classList.toggle('cursor', i === this.cursor));
      if (this.cursor >= 0 && items[this.cursor]) {
        items[this.cursor].scrollIntoView({ block: 'nearest' });
      }
    }

    destroy() {
      document.removeEventListener('mousedown', this._onDocDown);
      this.root.remove();
      MultiSelect._openInstances.delete(this);
    }
  }

  // Track instances so opening one closes the others.
  MultiSelect._openInstances = new Set();
  const _origOpen = MultiSelect.prototype.openPanel;
  MultiSelect.prototype.openPanel = function () {
    MultiSelect._openInstances.add(this);
    _origOpen.call(this);
  };

  // A single Escape anywhere closes whatever is open.
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') MultiSelect._openInstances.forEach(i => i.closePanel(false));
  });

  global.MultiSelect = MultiSelect;
})(window);
