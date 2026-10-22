"""Terminal-inspired application palettes and persistent local appearance."""
import tkinter as tk
from tkinter import ttk
from .preferences import read_preferences, write_preference

PALETTES = {
    'dark': dict(bg='#0c0c0c', panel='#202020', field='#161616', fg='#cccccc', muted='#aaaaaa',
                 select='#264f78', accent='#61afef', add='#153820', remove='#482026', spacer='#252525',
                 word_add='#245b35', word_remove='#75323e',
                 search='#66551c', search_active='#f2cf66', search_text='#101010',
                 comment='#8fbf87', keyword='#82aaff', string='#e6c07b', number='#d699e5',
                 function='#76d7e4', type='#81c995', operator='#cccccc'),
    'light': dict(bg='#f3f3f3', panel='#e4e4e4', field='#ffffff', fg='#202020', muted='#505050',
                  select='#cce5ff', accent='#005a9e', add='#e1f3e4', remove='#fbe5e8', spacer='#e9e9e9',
                  word_add='#a9dbb3', word_remove='#efb4bf',
                  search='#fff0a3', search_active='#e8a83d', search_text='#101010',
                  comment='#356b32', keyword='#003e99', string='#734b00', number='#742583',
                  function='#005f6b', type='#245c35', operator='#303030')}


class Theme:
    def __init__(self, root, path):
        self.root, self.path = root, path
        value = read_preferences(path).get('theme', 'dark')
        self.mode = tk.StringVar(root, value=value if isinstance(value, str) and value in PALETTES else 'dark')
        root._apptrail_theme = self
        ttk.Style(root).theme_use('clam')
        self.apply()

    @property
    def colors(self):
        return PALETTES[self.mode.get()]

    def toggle(self):
        self.mode.set('light' if self.mode.get() == 'dark' else 'dark')
        self.apply()
        try:
            write_preference(self.path, 'theme', self.mode.get())
        except OSError:
            from tkinter import messagebox
            messagebox.showwarning('Appearance preference', 'Theme changed for this session, but the preference could not be saved.', parent=self.root)

    def apply(self):
        p = self.colors
        style = ttk.Style(self.root)
        style.configure('.', background=p['bg'], foreground=p['fg'], troughcolor=p['field'],
                        bordercolor=p['panel'], lightcolor=p['panel'], darkcolor=p['panel'],
                        selectbackground=p['select'], selectforeground=p['fg'])
        style.map('.', background=[('disabled', p['panel']), ('active', p['panel'])],
                  foreground=[('disabled', p['muted'])], selectbackground=[('!focus', p['select'])],
                  selectforeground=[('!focus', p['fg'])])
        style.configure('TButton', background=p['panel'], padding=5)
        style.map('TButton', background=[('active', p['select']), ('pressed', p['select'])])
        style.configure('TEntry', fieldbackground=p['field'], insertcolor=p['fg'])
        style.configure('TSpinbox', fieldbackground=p['field'], insertcolor=p['fg'], arrowcolor=p['fg'])
        style.configure('Treeview', background=p['field'], fieldbackground=p['field'], foreground=p['fg'])
        style.map('Treeview', background=[('selected', p['select'])], foreground=[('selected', p['fg'])])
        style.configure('Treeview.Heading', background=p['panel'])
        style.map('Treeview.Heading', background=[('active', p['select'])])
        style.configure('Subtitle.TLabel', foreground=p['muted'])
        style.configure('TNotebook.Tab', background=p['panel'])
        style.map('TNotebook.Tab', background=[('selected', p['field']), ('active', p['select'])])
        style.map('TCheckbutton', background=[('active', p['panel'])], foreground=[('disabled', p['muted'])])
        style.configure('TCheckbutton', indicatorbackground=p['field'], indicatorforeground=p['fg'])
        style.map('TCheckbutton', indicatorbackground=[('pressed', p['select']), ('selected', p['select']), ('disabled', p['panel'])],
                  indicatorforeground=[('disabled', p['muted']), ('selected', p['fg'])])
        style.configure('TProgressbar', background=p['accent'])
        style.configure('TScrollbar', background=p['panel'], arrowcolor=p['fg'])
        style.map('TButton', foreground=[('disabled', p['muted'])])
        self.paint(self.root)

    def paint(self, widget):
        p = self.colors
        if isinstance(widget, (tk.Tk, tk.Toplevel)):
            widget.configure(background=p['bg'])
        if isinstance(widget, tk.Frame) and not isinstance(widget, ttk.Widget):
            widget.configure(background=p['bg'])
        if isinstance(widget, tk.Scrollbar) and not isinstance(widget, ttk.Widget):
            widget.configure(background=p['panel'], troughcolor=p['field'], activebackground=p['select'])
        if isinstance(widget, tk.Text):
            widget.configure(background=p['field'], foreground=p['fg'], insertbackground=p['fg'],
                             selectbackground=p['select'], selectforeground=p['fg'],
                             highlightbackground=p['panel'], highlightcolor=p['accent'])
            for category in ('comment', 'keyword', 'string', 'number', 'function', 'type', 'operator'):
                widget.tag_configure('syntax_' + category, foreground=p[category])
            widget.tag_configure('line_number', foreground=p['muted'])
            for tag, bg in (('add', 'add'), ('remove', 'remove'), ('spacer', 'spacer'), ('hunk', 'panel')):
                widget.tag_configure(tag, background=p[bg], foreground=p['fg'])
            if getattr(widget, '_diff_side', None):
                widget.tag_configure('changed', background=p[widget._diff_side])
                widget.tag_configure('word_change', background=p['word_' + widget._diff_side])
                widget.tag_raise('word_change', 'changed')
            widget.tag_configure('header', foreground=p['accent'])
            widget.tag_configure('meta', foreground=p['muted'])
            widget.tag_configure('search_match', background=p['search'])
            widget.tag_configure('search_current', background=p['search_active'], foreground=p['search_text'])
            widget.tag_raise('search_match')
            widget.tag_raise('search_current')
        for child in widget.winfo_children():
            self.paint(child)
