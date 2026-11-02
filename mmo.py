#!/usr/bin/env python3
"""MetaMovie Organiser: review movie names, repair titles and inspect a library."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

import support
import mmo_core as core
import media_tools as media
import workflow
import collections_tools as collections_api
from tvdb_provider import TVDBClient
from ui_theme import (apply_theme, load_image, set_icon, fit_dialog, ScrollPage, card,
                      field, BG, PANEL, FIELD, BORDER, TEXT, MUTED, ACCENT, SUCCESS, WARNING)

APP_NAME = 'MetaMovie Organiser'
APP_VERSION = '1.1.0'
MODES = {'Rename in place': 'rename', 'Copy to library': 'copy', 'Move to library': 'move'}


def assets_path():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / 'assets'


def enable_dpi():
    if sys.platform == 'win32':
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


class MovieApp:
    def __init__(self, root):
        self.root = root
        self.settings = support.load_settings()
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.thread = None
        self.closing = False
        self.closed = False
        self.saved_states = []
        self.plan = []
        self.report = None
        self.collections_report = None
        self.dialogs = set()
        self.store = None
        self.last_manifest = None
        self.scale = max(1.0, float(root.tk.call('tk', 'scaling')) * 72 / 96)
        apply_theme(root)
        root.title(f'{APP_NAME} · {APP_VERSION}')
        set_icon(root, assets_path() / 'MMO.ico')
        width, height = self.settings.get('last_window_size', [1220, 900])
        screen_w,screen_h=root.winfo_screenwidth(),root.winfo_screenheight()
        width=min(round(width*self.scale),screen_w-40)
        height=min(round(height*self.scale),screen_h-90)
        x=max(0,(screen_w-width)//2)
        y=max(0,(screen_h-height-40)//2)
        root.geometry(f'{width}x{height}+{x}+{y}')
        root.minsize(800, 620)
        self.source = tk.StringVar()
        self.destination = tk.StringVar()
        self.mode = tk.StringVar(value=next((label for label, value in MODES.items() if value == self.settings.get('mode')), 'Rename in place'))
        self.recursive = tk.BooleanVar(value=self.settings.get('recursive', True))
        self.online = tk.BooleanVar(value=False)
        self.api_key = tk.StringVar()
        self.tvdb_key = tk.StringVar()
        self.tvdb_pin = tk.StringVar()
        self.provider = tk.StringVar(value=self.settings.get('provider','TMDB'))
        self.dry_run = tk.BooleanVar(value=self.settings.get('dry_run',True))
        self.write_nfo = tk.BooleanVar(value=self.settings.get('write_nfo',False))
        self.embedded_tags = tk.BooleanVar(value=self.settings.get('embedded_tags',False))
        self.replace_comments = tk.BooleanVar(value=self.settings.get('replace_comments',True))
        self.verification_mode = tk.StringVar(value=self.settings.get('verification_mode','Verified'))
        self.verification_method = tk.StringVar(value=self.settings.get('verification_method','BLAKE3'))
        self.quick_sample = tk.BooleanVar(value=self.settings.get('quick_sample',True))
        self.validation_help = tk.StringVar()
        self.metadata_comment = tk.StringVar()
        self.metadata_comment_enabled = tk.BooleanVar(value=True)
        self.language = tk.StringVar(value=self.settings.get('language', 'en'))
        self.portable = tk.BooleanVar(value=support.portable_mode_enabled())
        self.status = tk.StringVar(value='Ready · choose a movie folder to begin')
        self.progress_value = tk.DoubleVar(value=0)
        self.selection_note = tk.StringVar(value='Your plan will appear here. No changes happen until you review and apply it.')
        self.preview_note = tk.StringVar(value='RENAME IN PLACE  ·  no video copying')
        self.search = tk.StringVar()
        self.collections_note=tk.StringVar(value='Use TMDB to identify movie collections and check which released films were not found among confirmed matches.')
        self.collection_detail=tk.StringVar(value='Select a collection to inspect owned, missing and upcoming films.')
        self.collection_refresh=tk.BooleanVar(value=False)
        self.tool_vars = {name:tk.StringVar(value=self.settings.get(name,'')) for name in ('mediainfo','ffprobe','mkvpropedit')}
        self.metadata_title = tk.StringVar()
        self.metadata_year = tk.StringVar()
        self.metadata_consent = tk.BooleanVar(value=False)
        self.metadata_note = tk.StringVar(value='Select a matched movie in the file plan to fill its title and year.')
        self.analytics_note = tk.StringVar(value='Analyse a folder to see real container and stream information. No API key is needed.')
        self.metric_vars = {name:tk.StringVar(value='—') for name in ('Files','Storage','Video codecs','4K / 8K','HDR files')}
        self.build()
        for variable in (self.source,self.destination,self.recursive,self.online,self.language,self.provider):
            variable.trace_add('write',lambda *_:self.invalidate_plan())
        self.source.trace_add('write',lambda *_:self.invalidate_report('Folder changed. Analyse again to read this library.'))
        try:
            support.configure_logging()
            self.store = support.CredentialsStore(self.portable.get())
            if not self.portable.get():
                for name,variable in (('tmdb_key',self.api_key),('tvdb_key',self.tvdb_key),('tvdb_pin',self.tvdb_pin)):
                    value=self.store.get(name)
                    if value: variable.set(value)
                if self.selected_api_key().get(): self.online.set(True)
        except Exception as exc:
            self.log(f'Saved connection unavailable: {exc}')
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.bind('<Control-Return>', lambda event:self.preview())
        root.bind('<F1>', lambda event:self.about())
        self.root.after(60, self.pump)

    def build(self):
        header = ttk.Frame(self.root, padding=(24,18,24,14))
        header.pack(fill='x')
        self.mark = load_image(assets_path()/'MMO.png',(round(48*self.scale),round(48*self.scale)))
        if self.mark:
            ttk.Label(header,image=self.mark).pack(side='left',padx=(0,12))
        title = ttk.Frame(header)
        title.pack(side='left')
        ttk.Label(title,text='MetaMovie Organiser',style='Brand.TLabel').pack(anchor='w')
        ttk.Label(title,text='A tidy movie library. A clear view of every file.',style='Muted.TLabel').pack(anchor='w',pady=(3,0))
        ttk.Label(header,text='MMO  '+APP_VERSION,style='Badge.TLabel').pack(side='right')
        ttk.Separator(self.root).pack(fill='x',padx=24)
        self.form = ttk.Frame(self.root,padding=(24,12,24,0))
        self.form.pack(fill='both',expand=True)
        self.notebook = ttk.Notebook(self.form)
        self.notebook.pack(fill='both',expand=True)
        self.library_tab = ttk.Frame(self.notebook)
        self.analytics_tab = ttk.Frame(self.notebook)
        self.output_tab = ScrollPage(self.notebook)
        self.metadata_tab = ScrollPage(self.notebook)
        self.settings_tab = ScrollPage(self.notebook)
        for widget,label in ((self.library_tab,'Movie library'),(self.output_tab,'Output & validation'),(self.analytics_tab,'Media analytics'),(self.metadata_tab,'Embedded metadata'),(self.settings_tab,'Connections & tools')):
            self.notebook.add(widget,text=label)
        self.build_library()
        self.build_output()
        self.build_analytics()
        self.build_metadata()
        self.build_settings()
        footer = ttk.Frame(self.root,padding=(24,8,24,16))
        footer.pack(fill='x',side='bottom',before=self.form)
        controls = ttk.Frame(footer)
        controls.pack(fill='x',pady=(0,6))
        self.cancel_button = ttk.Button(controls,text='Cancel operation',command=self.request_cancel,state='disabled')
        self.cancel_button.pack(side='right')
        ttk.Button(controls,text='About',style='Quiet.TButton',command=self.about).pack(side='right',padx=8)
        ttk.Button(controls,text='Activity',style='Quiet.TButton',command=self.show_activity).pack(side='left')
        ttk.Label(controls,textvariable=self.status,style='Muted.TLabel').pack(side='left',padx=12)
        ttk.Progressbar(footer,variable=self.progress_value,maximum=100).pack(fill='x')

    def build_library(self):
        setup = ttk.Frame(self.library_tab,style='Card.TFrame',padding=16)
        setup.pack(fill='x',pady=(4,12))
        setup.columnconfigure(1,weight=1)
        field(setup,0,'Movies folder',self.source)
        ttk.Button(setup,text='Browse…',command=self.choose_source).grid(row=0,column=2,padx=(10,0))
        ttk.Label(setup,text='Operation',style='Card.TLabel').grid(row=1,column=0,sticky='w',padx=(0,16),pady=6)
        ttk.Combobox(setup,textvariable=self.mode,values=list(MODES),state='readonly',width=22).grid(row=1,column=1,sticky='w',pady=6)
        self.destination_entry = field(setup,2,'Destination',self.destination)
        self.destination_button = ttk.Button(setup,text='Browse…',command=lambda:self.destination.set(filedialog.askdirectory(parent=self.root,title='Choose Movies destination') or self.destination.get()))
        self.destination_button.grid(row=2,column=2,padx=(10,0))
        options = ttk.Frame(setup,style='Card.TFrame')
        options.grid(row=3,column=0,columnspan=3,sticky='ew',pady=(8,0))
        ttk.Checkbutton(options,text='Include subfolders',variable=self.recursive).pack(side='left',padx=(0,18))
        ttk.Checkbutton(options,text='Match with provider',variable=self.online).pack(side='left')
        ttk.Combobox(options,textvariable=self.provider,values=['TMDB','TVDB'],state='readonly',width=8).pack(side='left',padx=8)
        ttk.Checkbutton(options,text='Dry run',variable=self.dry_run).pack(side='left',padx=8)
        ttk.Label(setup,textvariable=self.preview_note,style='CardMuted.TLabel').grid(row=4,column=0,columnspan=3,sticky='w',pady=(6,0))
        self.mode.trace_add('write',self.mode_changed)
        self.mode_changed()
        actions = ttk.Frame(self.library_tab)
        actions.pack(fill='x',pady=(0,10))
        self.preview_button = ttk.Button(actions,text='Preview names',style='Primary.TButton',command=self.preview)
        self.preview_button.pack(side='left')
        self.apply_button = ttk.Button(actions,text='Apply approved plan',command=self.apply_plan,state='disabled')
        self.apply_button.pack(side='left',padx=8)
        self.undo_button = ttk.Button(actions,text='Undo file operation',command=self.undo_files)
        self.undo_button.pack(side='right')
        ttk.Button(actions,text='Edit selected match',command=self.edit_match).pack(side='right',padx=8)
        self.plan_tree = self.table(self.library_tab,[('approve','Use',55),('title','Movie / year',220),('source','Original',260),('target','Proposed',260),('status','Status',150)])
        self.plan_tree.bind('<<TreeviewSelect>>',self.select_movie)
        self.plan_tree.bind('<Double-1>',lambda event:self.toggle_approval())
        self.plan_tree.bind('<space>',lambda event:self.toggle_approval())
        self.plan_tree.tag_configure('warning',foreground=WARNING)
        self.plan_tree.tag_configure('good',foreground=SUCCESS)
        note = ttk.Label(self.library_tab,textvariable=self.selection_note,style='Muted.TLabel',wraplength=1000,justify='left')
        note.pack(side='bottom',fill='x',pady=10,before=self.plan_tree.master)
        self.library_tab.bind('<Configure>',lambda event:note.configure(wraplength=max(200,self.library_tab.winfo_width()-10)),add='+')

    def build_output(self):
        checks=card(self.output_tab.body,'CHECK','Validate file transfers','Rename in place changes names using identity guards. Copy and Move use the verification policy below. Quick mode provides less assurance; it can miss changes that full hashes detect.')
        ttk.Label(checks,text='Validation mode',style='Card.TLabel').grid(row=0,column=0,sticky='w',padx=(0,16),pady=6)
        self.validation_combo=ttk.Combobox(checks,textvariable=self.verification_mode,values=['Quick','Verified','Custom'],state='readonly',width=20)
        self.validation_combo.grid(row=0,column=1,sticky='w',pady=6)
        ttk.Label(checks,text='Custom method',style='Card.TLabel').grid(row=1,column=0,sticky='w',pady=6)
        self.hash_combo=ttk.Combobox(checks,textvariable=self.verification_method,values=list(core.VERIFICATION_DESCRIPTIONS),state='readonly',width=30)
        self.hash_combo.grid(row=1,column=1,sticky='w',pady=6)
        self.sample_check=ttk.Checkbutton(checks,text='Sample copied data in Quick mode',variable=self.quick_sample)
        self.sample_check.grid(row=2,column=0,columnspan=2,sticky='w',pady=6)
        ttk.Label(checks,textvariable=self.validation_help,style='CardMuted.TLabel',wraplength=900).grid(row=3,column=0,columnspan=2,sticky='ew',pady=6)
        extra=card(self.output_tab.body,'OUTPUT','Optional outputs for every operation','These options apply to Rename in place, Copy and Move. Copy edits the destination only. Dry run reports the planned outputs without creating files or changing tags.')
        ttk.Checkbutton(extra,text='Dry run · preview every change without applying it',variable=self.dry_run).pack(anchor='w')
        ttk.Checkbutton(extra,text='Create a movie NFO file · existing NFO files are left unchanged',variable=self.write_nfo).pack(anchor='w',pady=6)
        ttk.Checkbutton(extra,text='Correct embedded title and year after the file operation',variable=self.embedded_tags).pack(anchor='w',pady=6)
        self.comment_output_check=ttk.Checkbutton(extra,text='Replace the embedded comment with Movie title (Year)',variable=self.replace_comments)
        self.comment_output_check.pack(anchor='w',padx=(22,0))
        ttk.Label(extra,text='Embedded edits preserve media streams but can rewrite container metadata. The linked recovery record restores tag values before undoing file operations.',style='CardMuted.TLabel',wraplength=900).pack(fill='x',pady=(12,0))
        for variable in (self.verification_mode,self.verification_method,self.quick_sample,self.dry_run,self.write_nfo,self.embedded_tags,self.replace_comments):
            variable.trace_add('write',self.output_changed)
        self.output_changed()

    def output_changed(self,*_):
        rename=MODES[self.mode.get()]=='rename'
        mode=self.verification_mode.get()
        self.validation_combo.configure(state='disabled' if rename else 'readonly')
        self.hash_combo.configure(state='readonly' if not rename and mode=='Custom' else 'disabled')
        self.sample_check.configure(state='normal' if not rename and mode=='Quick' else 'disabled')
        self.comment_output_check.configure(state='normal' if self.embedded_tags.get() else 'disabled')
        if rename:
            help_text='Rename-only uses file identity and recorded snapshots. It never hashes or copies movie bytes. Optional embedded edits are a separate write.'
        elif mode=='Verified':
            help_text='BLAKE3 checks the full file during transfer and reads the destination back to compare.'
        elif mode=='Quick':
            help_text='Same-volume moves use directory-entry identity. Copies use '+('sampled content checks.' if self.quick_sample.get() else 'size and identity checks.')
        else:
            help_text=core.VERIFICATION_DESCRIPTIONS.get(self.verification_method.get(),'Choose a validation method.')
        self.validation_help.set(help_text)
        if hasattr(self,'apply_button'):
            self.apply_button.configure(text='Run dry run' if self.dry_run.get() else 'Apply approved plan')
            self.apply_button.configure(state='normal' if self.thread is None and any(self.eligible(g) for g in self.plan) else 'disabled')
        extras=[]
        if self.write_nfo.get(): extras.append('NFO')
        if self.embedded_tags.get(): extras.append('embedded tags'+(' + comment' if self.replace_comments.get() else ''))
        self.preview_note.set(('DRY RUN  ·  ' if self.dry_run.get() else '')+('RENAME IN PLACE  ·  no video copying' if rename else self.mode.get().upper()+'  ·  '+mode)+('  ·  '+', '.join(extras) if extras else ''))
        if hasattr(self,'plan_tree') and self.plan and self.thread is None:
            self.refresh_plan()

    def build_analytics(self):
        self.analytics_book=ttk.Notebook(self.analytics_tab)
        self.analytics_book.pack(fill='both',expand=True)
        self.media_stats_tab=ttk.Frame(self.analytics_book)
        self.collections_tab=ttk.Frame(self.analytics_book)
        self.analytics_book.add(self.media_stats_tab,text='Media formats & streams')
        self.analytics_book.add(self.collections_tab,text='Film collections')
        toolbar = ttk.Frame(self.media_stats_tab,padding=(0,10))
        toolbar.pack(fill='x')
        self.analyse_button = ttk.Button(toolbar,text='Analyse folder',style='Primary.TButton',command=self.analyse)
        self.analyse_button.pack(side='left')
        ttk.Button(toolbar,text='Choose folder…',command=self.choose_source).pack(side='left',padx=8)
        ttk.Button(toolbar,text='Export CSV',command=lambda:self.export('csv')).pack(side='right',padx=(8,0))
        ttk.Button(toolbar,text='Export JSON',command=lambda:self.export('json')).pack(side='right')
        ttk.Button(toolbar,text='Export counts',command=self.export_counts).pack(side='right',padx=8)
        ttk.Label(self.media_stats_tab,textvariable=self.analytics_note,style='Muted.TLabel',wraplength=1050).pack(fill='x',pady=(0,12))
        metrics = ttk.Frame(self.media_stats_tab)
        metrics.pack(fill='x',pady=(0,12))
        for index,(name,variable) in enumerate(self.metric_vars.items()):
            metrics.columnconfigure(index,weight=1)
            box = ttk.Frame(metrics,style='Card.TFrame',padding=12)
            box.grid(row=0,column=index,sticky='nsew',padx=(0,8 if index<4 else 0))
            ttk.Label(box,text=name.upper(),style='CardMuted.TLabel').pack(anchor='w')
            ttk.Label(box,textvariable=variable,style='Metric.TLabel').pack(anchor='w',pady=(6,0))
        style=ttk.Style(self.root)
        style.configure('Metric.TLabel',background=PANEL,foreground=TEXT,font=('Segoe UI',19,'bold'))
        panes = ttk.Panedwindow(self.media_stats_tab,orient='horizontal')
        panes.pack(fill='both',expand=True)
        categories = ttk.Frame(panes)
        rows = ttk.Frame(panes)
        panes.add(categories,weight=1)
        panes.add(rows,weight=3)
        self.category_tree = self.table(categories,[('category','Category / detected value',210),('count','Files',65)])
        self.category_tree.bind('<<TreeviewSelect>>',self.filter_category)
        filter_row = ttk.Frame(rows)
        filter_row.pack(fill='x',pady=(0,8))
        ttk.Label(filter_row,text='Filter',style='Muted.TLabel').pack(side='left',padx=(0,8))
        ttk.Entry(filter_row,textvariable=self.search).pack(side='left',fill='x',expand=True)
        ttk.Button(filter_row,text='All files',command=lambda:self.clear_filter()).pack(side='right',padx=(8,0))
        self.analytics_tree = self.table(rows,[('file','File',240),('container','Container',105),('video','Video',115),('resolution','Resolution',100),('hdr','HDR',160),('audio','Audio',130)])
        self.analytics_tree.bind('<Double-1>',lambda event:self.inspect_selected())
        inspect_button=ttk.Button(rows,text='Inspect selected file',command=self.inspect_selected)
        inspect_button.pack(side='bottom',anchor='e',pady=8,before=self.analytics_tree.master)
        self.search.trace_add('write',lambda *_:self.populate_records())
        self.category_filter = None

        self.build_collections()

    def build_collections(self):
        toolbar=ttk.Frame(self.collections_tab,padding=(0,10));toolbar.pack(fill='x')
        ttk.Button(toolbar,text='Check film collections',style='Primary.TButton',command=self.check_collections).pack(side='left')
        ttk.Checkbutton(toolbar,text='Refresh provider data',style='Footer.TCheckbutton',variable=self.collection_refresh).pack(side='left',padx=12)
        ttk.Button(toolbar,text='Export CSV',command=lambda:self.export_collections('csv')).pack(side='right',padx=(8,0))
        ttk.Button(toolbar,text='Export JSON',command=lambda:self.export_collections('json')).pack(side='right')
        note=ttk.Label(self.collections_tab,textvariable=self.collections_note,style='Muted.TLabel',wraplength=1050,justify='left')
        note.pack(fill='x',pady=(0,12))
        panes=ttk.Panedwindow(self.collections_tab,orient='horizontal');panes.pack(fill='both',expand=True)
        left=ttk.Frame(panes);right=ttk.Frame(panes)
        panes.add(left,weight=2);panes.add(right,weight=3)
        self.collections_tree=self.table(left,[('name','Collection',260),('owned','Owned',60),('missing','Not found',75),('upcoming','Upcoming',75)])
        self.collections_tree.bind('<<TreeviewSelect>>',self.select_collection)
        self.collection_members=self.table(right,[('status','Status',105),('title','Movie',250),('year','Year',60),('release','Release date',110),('paths','Local file(s)',280)])
        self.collection_members.tag_configure('owned',foreground=SUCCESS)
        self.collection_members.tag_configure('missing',foreground=WARNING)
        self.collection_members.tag_configure('upcoming',foreground=ACCENT)
        self.collection_members.bind('<<TreeviewSelect>>',self.select_collection_member)
        detail=ttk.Label(self.collections_tab,textvariable=self.collection_detail,style='Muted.TLabel',wraplength=1050,justify='left')
        detail.pack(side='bottom',fill='x',pady=8,before=panes)
        ttk.Button(self.collections_tab,text='Show unmatched files / errors',command=self.show_collection_issues).pack(side='bottom',anchor='e',pady=(4,0),before=panes)
        self.collections_tab.bind('<Configure>',lambda event:(note.configure(wraplength=max(200,self.collections_tab.winfo_width()-8)),detail.configure(wraplength=max(200,self.collections_tab.winfo_width()-8))),add='+')

    def build_metadata(self):
        body = card(self.metadata_tab.body,'TAG','Correct descriptive metadata','Update the title, release year and optional comment inside supported MP4/M4V/MOV and MKV files. Media streams are preserved. The Windows package supplies the tools needed for MKV edits.')
        field(body,0,'Correct title',self.metadata_title)
        field(body,1,'Release year',self.metadata_year)
        field(body,2,'Correct comment',self.metadata_comment)
        ttk.Checkbutton(body,text='Update comment (empty removes it)',variable=self.metadata_comment_enabled).grid(row=3,column=0,columnspan=2,sticky='w',pady=6)
        ttk.Label(body,textvariable=self.metadata_note,style='CardMuted.TLabel',wraplength=880).grid(row=4,column=0,columnspan=2,sticky='ew',pady=10)
        ttk.Checkbutton(body,text='I want to modify the embedded tags of the selected file',variable=self.metadata_consent).grid(row=5,column=0,columnspan=2,sticky='w',pady=8)
        buttons = ttk.Frame(body,style='Card.TFrame')
        buttons.grid(row=6,column=0,columnspan=2,sticky='ew',pady=8)
        ttk.Button(buttons,text='Choose a video…',command=self.choose_metadata).pack(side='left')
        ttk.Button(buttons,text='Correct embedded tags',style='Primary.TButton',command=self.update_metadata).pack(side='left',padx=10)
        ttk.Button(buttons,text='Restore last tag edit',command=self.undo_metadata).pack(side='right')
        body2=card(self.metadata_tab.body,'INFO','Technical properties stay factual','Codec, resolution, bit depth, HDR and audio languages describe the actual media streams. MMO reports them from the file; it does not invent them or transcode your movies.')
        ttk.Label(body2,text='Tag restoration restores the previous values. A container may have a different byte layout afterwards. MP4 tag saving can rewrite container data; rename-only mode never calls the tag writer.',style='CardMuted.TLabel',wraplength=920,justify='left').pack(fill='x')
        self.metadata_path = None

    def build_settings(self):
        connection=card(self.settings_tab.body,'API','Movie matching','Choose TMDB or TheTVDB. Matching sends the parsed movie title/year to the selected provider. Filename-only previews and media analysis work offline.')
        ttk.Label(connection,text='Provider',style='Card.TLabel').grid(row=0,column=0,sticky='w',padx=(0,16),pady=6)
        ttk.Combobox(connection,textvariable=self.provider,values=['TMDB','TVDB'],state='readonly',width=16).grid(row=0,column=1,sticky='w',pady=6)
        self.tmdb_fields=ttk.Frame(connection,style='Card.TFrame');self.tmdb_fields.columnconfigure(1,weight=1)
        field(self.tmdb_fields,0,'TMDB API key',self.api_key,password=True)
        self.tvdb_fields=ttk.Frame(connection,style='Card.TFrame');self.tvdb_fields.columnconfigure(1,weight=1)
        field(self.tvdb_fields,0,'TVDB API key',self.tvdb_key,password=True)
        field(self.tvdb_fields,1,'TVDB PIN (if issued)',self.tvdb_pin,password=True)
        def show_fields(*_):
            self.tmdb_fields.grid_remove();self.tvdb_fields.grid_remove()
            current=self.tvdb_fields if self.provider.get()=='TVDB' else self.tmdb_fields
            current.grid(row=1,column=0,columnspan=2,sticky='ew',pady=6)
        self.provider.trace_add('write',show_fields);show_fields()
        field(connection,2,'Title language',self.language)
        buttons=ttk.Frame(connection,style='Card.TFrame');buttons.grid(row=3,column=0,columnspan=2,sticky='ew',pady=10)
        ttk.Button(buttons,text='Save key securely',command=self.save_key).pack(side='left')
        ttk.Button(buttons,text='Load saved key',command=self.load_key).pack(side='left',padx=8)
        ttk.Button(buttons,text='Test connection',command=self.test_key).pack(side='left')
        tools = card(self.settings_tab.body,'TOOLS','Media tools','The Windows package includes MediaInfo CLI, FFprobe and MKVToolNix tools. Leave paths empty to use the bundled versions, or choose your own tools below.')
        for row,name in enumerate(self.tool_vars):
            field(tools,row,{'mediainfo':'MediaInfo CLI','ffprobe':'FFprobe fallback','mkvpropedit':'MKVPropEdit'}[name],self.tool_vars[name])
            ttk.Button(tools,text='Browse…',command=lambda name=name:self.pick_tool(name)).grid(row=row,column=2,padx=(8,0))
        ttk.Button(tools,text='Use bundled tools',command=self.use_bundled_tools).grid(row=3,column=1,sticky='w',pady=8)
        self.tool_status=tk.StringVar(value=self.detect_tools())
        ttk.Label(tools,textvariable=self.tool_status,style='CardMuted.TLabel',wraplength=900).grid(row=4,column=0,columnspan=3,sticky='ew',pady=6)
        storage = card(self.settings_tab.body,'APP','Independent application settings','MMO uses its own settings, credentials, activity logs and recovery records. Portable mode stores them beside MMO and protects saved keys with a passphrase.')
        ttk.Checkbutton(storage,text='Use portable mode',variable=self.portable,command=self.change_portable).pack(anchor='w')
        ttk.Button(storage,text='Save settings',command=self.persist).pack(anchor='w',pady=(10,0))

    def table(self,parent,columns):
        frame = ttk.Frame(parent)
        frame.pack(fill='both',expand=True)
        tree=ttk.Treeview(frame,columns=[c[0] for c in columns],show='headings',selectmode='browse')
        for key,label,width in columns:
            tree.heading(key,text=label)
            tree.column(key,width=width,minwidth=min(70,width),stretch=key not in ('approve','count'))
        vertical=ttk.Scrollbar(frame,orient='vertical',command=tree.yview)
        horizontal=ttk.Scrollbar(frame,orient='horizontal',command=tree.xview)
        tree.configure(yscrollcommand=vertical.set,xscrollcommand=horizontal.set)
        tree.grid(row=0,column=0,sticky='nsew')
        vertical.grid(row=0,column=1,sticky='ns')
        horizontal.grid(row=1,column=0,sticky='ew')
        frame.rowconfigure(0,weight=1)
        frame.columnconfigure(0,weight=1)
        return tree

    def mode_changed(self,*_):
        rename=MODES[self.mode.get()]=='rename'
        self.destination_entry.configure(state='disabled' if rename else 'normal')
        self.destination_button.configure(state='disabled' if rename else 'normal')
        self.preview_note.set('RENAME IN PLACE  ·  no video copying' if rename else self.mode.get().upper()+'  ·  verified transfers')
        self.invalidate_plan()
        if hasattr(self,'validation_combo'): self.output_changed()

    def invalidate_plan(self):
        self.plan=[]
        if hasattr(self,'plan_tree'):
            self.plan_tree.delete(*self.plan_tree.get_children())
            self.apply_button.configure(state='disabled')

    def choose_source(self):
        value=filedialog.askdirectory(parent=self.root,title='Choose your Movies folder')
        if value:
            self.source.set(value)
            self.invalidate_plan()

    def pick_tool(self,name):
        value=filedialog.askopenfilename(parent=self.root,title='Choose '+name, filetypes=[('Applications','*.exe'),('All files','*.*')])
        if value:
            self.tool_vars[name].set(value)

    def tool_paths(self):
        return {key:variable.get().strip() for key,variable in self.tool_vars.items() if variable.get().strip()}

    def log(self,message):
        clean=support.redact_secrets(message)
        support.logger.info(clean)
        if not hasattr(self,'activity_lines'):
            self.activity_lines=[]
        self.activity_lines.append(f'[{time.strftime("%H:%M:%S")}] {clean}')
        if hasattr(self,'activity_text') and self.activity_text.winfo_exists():
            self.activity_text.configure(state='normal')
            self.activity_text.insert('end',self.activity_lines[-1]+'\n')
            self.activity_text.configure(state='disabled')
            self.activity_text.see('end')

    def post(self,callback):
        self.events.put(callback)

    def worker_log(self,message):
        self.post(lambda message=message:self.log(message))

    def worker_progress(self,current,total,phase='Working'):
        percent=max(0,min(100,100*current/max(total,1)))
        self.post(lambda:(self.progress_value.set(percent),self.status.set(f'{phase} · {percent:.0f}%')))

    def busy(self,value):
        if value:
            self.saved_states=[]
            for widget in descendants(self.form):
                if isinstance(widget,(ttk.Button,ttk.Entry,ttk.Combobox,ttk.Checkbutton)):
                    self.saved_states.append((widget,str(widget.cget('state'))))
                    widget.configure(state='disabled')
        else:
            for widget,state in self.saved_states:
                if widget.winfo_exists():
                    widget.configure(state=state)
            self.saved_states=[]
            self.apply_button.configure(state='normal' if any(self.eligible(g) for g in self.plan) else 'disabled')
            self.output_changed()
        self.cancel_button.configure(state='normal' if value else 'disabled')

    def launch(self,task,done=None):
        if self.thread is not None:
            return
        self.cancel.clear()
        self.progress_value.set(0)
        self.busy(True)
        self.status.set('Working…')
        def worker():
            try:
                result=task()
                if done:
                    self.post(lambda result=result:done(result))
            except support.Cancelled:
                self.post(lambda:self.status.set('Cancelled · completed changes remain recorded for Undo'))
                self.worker_log('Cancelled safely. Check the activity and recovery records before retrying.')
            except Exception as exc:
                clean=support.redact_secrets(f'{type(exc).__name__}: {exc}')
                self.worker_log('Failed: '+clean)
                self.post(lambda clean=clean:self.status.set('Could not finish · '+clean[:140]))
            finally:
                self.post(lambda:self.finish_worker())
        self.thread=threading.Thread(target=worker,daemon=False,name='MMO-worker')
        self.thread.start()

    def finish_worker(self):
        if self.thread is not None and self.thread.is_alive():
            self.root.after(20,self.finish_worker)
            return
        if self.thread:
            self.thread.join()
        self.thread=None
        self.busy(False)
        if self.closing:
            self.closed=True
            self.root.destroy()

    def pump(self):
        while True:
            try:
                self.events.get_nowait()()
                if self.closed:
                    return
            except queue.Empty:
                break
            except Exception as exc:
                self.log(f'Interface error: {exc}')
        if not self.closed and self.root.winfo_exists():
            self.root.after(60,self.pump)

    def request_cancel(self):
        self.cancel.set()
        self.status.set('Cancelling at the next safe checkpoint…')

    def get_source(self):
        path=Path(self.source.get().strip())
        if not self.source.get().strip() or not path.is_dir():
            raise ValueError('Choose a readable Movies folder first.')
        return path.resolve()

    def preview(self):
        try:
            source=self.get_source()
            mode=MODES[self.mode.get()]
            destination=None
            if mode!='rename':
                if not self.destination.get().strip():
                    raise ValueError('Choose a destination for Copy or Move.')
                destination=Path(self.destination.get().strip()).resolve()
            matcher=None
            if self.online.get():
                matcher=self.make_provider()
            recursive=self.recursive.get()
        except Exception as exc:
            messagebox.showwarning('Cannot preview',support.redact_secrets(exc),parent=self.root)
            return
        self.invalidate_plan()
        self.persist()
        self.launch(lambda:core.build_plan(source,destination,mode,recursive,matcher,self.cancel,self.worker_log,self.worker_progress),self.receive_plan)

    def receive_plan(self,plan):
        self.plan=plan
        self.refresh_plan()
        self.status.set(f'Preview ready · {len(plan)} movie group(s) · no files changed')
        self.log(f'Preview ready: {len(plan)} group(s). Double-click a row to include or exclude it; edit unresolved matches before applying.')
        self.notebook.select(self.library_tab)

    def eligible(self,group):
        return group.approved and group.resolved and group.status!='Conflict' and (group.status!='Already correct' or self.write_nfo.get() or self.embedded_tags.get())

    def refresh_plan(self):
        core.validate_plan(self.plan)
        selected=self.plan_tree.selection()
        self.plan_tree.delete(*self.plan_tree.get_children())
        for index,group in enumerate(self.plan):
            title=f'{group.title} ({group.year or "year needed"})'
            target=next((change.dst for change in group.files if change.kind=='video'),group.destination)
            try:
                original=group.source.relative_to(group.source_root)
                proposed=target.relative_to(group.source_root if group.mode=='rename' else group.output_root)
            except (ValueError,TypeError):
                original,proposed=group.source,target
            subtitle_count=sum(change.kind=='subtitle' for change in group.files)
            display_status=group.status+(f' · {subtitle_count} subtitle(s)' if subtitle_count else '')
            self.plan_tree.insert('', 'end',iid=str(index), values=('✓' if self.eligible(group) else '',title,str(original),str(proposed),display_status),tags=('good' if self.eligible(group) else 'warning',))
        if selected and self.plan_tree.exists(selected[0]):
            self.plan_tree.selection_set(selected)
        self.apply_button.configure(state='normal' if any(self.eligible(g) for g in self.plan) and self.thread is None else 'disabled')
        self.selection_note.set(f'{len(self.plan)} group(s) · {sum(self.eligible(g) for g in self.plan)} ready and approved · double-click a row to toggle approval. Select a row to inspect every associated file.')

    def selected_group(self):
        selected=self.plan_tree.selection()
        return self.plan[int(selected[0])] if selected else None

    def select_movie(self,event=None):
        group=self.selected_group()
        if group:
            changes='\n'.join(f'{item.kind}: {item.src.name} → {item.dst.name}' for item in group.files[:8])
            self.selection_note.set(f'Folder: {group.source.parent} → {group.destination}\n{changes}'+ ('\n'+ '; '.join(group.warnings) if group.warnings else ''))
            self.metadata_path=Path(group.source)
            self.metadata_title.set(group.title)
            self.metadata_year.set(str(group.year or ''))
            self.metadata_comment.set(f'{group.title} ({group.year})' if group.year else group.title)
            self.metadata_note.set(str(self.metadata_path))

    def toggle_approval(self):
        if self.thread:
            return
        group=self.selected_group()
        if group:
            if not group.resolved or group.status=='Conflict':
                self.edit_match()
                return
            group.approved=not group.approved
            self.refresh_plan()

    def new_dialog(self,title,width=900,height=600):
        dialog=tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        fit_dialog(dialog,self.root,width,height)
        set_icon(dialog,assets_path()/'MMO.ico')
        dialog.bind('<Escape>',lambda event:dialog.destroy())
        return dialog

    def edit_match(self):
        group=self.selected_group()
        if not group or self.thread:
            return
        dialog=self.new_dialog('Review movie identity',850,570)
        content=ttk.Frame(dialog,padding=18)
        content.pack(fill='both',expand=True)
        ttk.Label(content,text=group.source.name,style='Heading.TLabel',wraplength=780).pack(anchor='w',pady=(0,12))
        title=tk.StringVar(value=group.title)
        year=tk.StringVar(value=str(group.year or ''))
        fields=ttk.Frame(content,style='Card.TFrame',padding=12)
        fields.pack(fill='x',pady=(0,12))
        fields.columnconfigure(1,weight=1)
        field(fields,0,'Movie title',title)
        field(fields,1,'Release year',year)
        ttk.Label(content,text='Choose a TMDB suggestion or correct the title and year manually.',style='Muted.TLabel').pack(anchor='w',pady=(0,8))
        tree=self.table(content,[('title','Suggested title',360),('year','Year',85),('score','Match',80)])
        for index,candidate in enumerate(group.candidates):
            tree.insert('','end',iid=str(index),values=(candidate.title,candidate.year,f'{candidate.score:.0%}'))
        def choose(event=None):
            selection=tree.selection()
            if selection:
                candidate=group.candidates[int(selection[0])]
                title.set(candidate.title)
                year.set(str(candidate.year or ''))
        tree.bind('<<TreeviewSelect>>',choose)
        def save():
            try:
                if not title.get().strip() or not year.get().isdigit() or not 1870<=int(year.get())<=2200:
                    raise ValueError('Enter a title and four-digit release year.')
                candidate=next((c for c in group.candidates if c.title==title.get().strip() and c.year==int(year.get())),None)
                core.revise_group(group,title.get().strip(),int(year.get()),candidate.tmdb_id if candidate else None,provider_id=getattr(candidate,'provider_id',None),provider=getattr(candidate,'provider',self.provider.get()))
                self.refresh_plan()
                self.select_movie()
                dialog.destroy()
            except Exception as exc:
                messagebox.showwarning('Check movie identity',str(exc),parent=dialog)
        ttk.Button(content,text='Use this title and year',style='Primary.TButton',command=save).pack(anchor='e',pady=(12,0))
        dialog.grab_set()

    def apply_plan(self):
        if self.thread or not self.plan: return
        core.validate_plan(self.plan)
        count=sum(self.eligible(g) for g in self.plan)
        if not count: return
        mode=self.plan[0].mode
        dry=self.dry_run.get()
        extras=[]
        if self.write_nfo.get(): extras.append('create missing NFO files')
        if self.embedded_tags.get(): extras.append('correct embedded title/year'+(' and comment' if self.replace_comments.get() else ''))
        question=f'{"Dry run" if dry else "Apply "+mode} for {count} approved movie group(s)?'
        if extras: question+='\n\nOptions: '+', '.join(extras)+'.'
        if dry: question+='\n\nNo media, NFO or embedded tags will be changed.'
        elif mode=='rename': question+='\n\nNames change without copying video data. Enabled embedded edits can rewrite container metadata.'
        else: question+='\n\nValidation: '+self.verification_mode.get()+'. Embedded edits affect the destination only.'
        if not messagebox.askyesno('Apply reviewed plan',question,parent=self.root): return
        verification='BLAKE3' if mode=='rename' else core.resolve_verification(self.verification_mode.get(),self.verification_method.get(),self.quick_sample.get())
        options=dict(mode=mode,verification=verification,write_nfo=self.write_nfo.get(),embedded_tags=self.embedded_tags.get(),replace_comments=self.replace_comments.get(),dry_run=dry,journal_dir=support.state_root()/'manifests',cancel_event=self.cancel,log=self.worker_log,progress=self.worker_progress,tool_paths=self.tool_paths())
        self.persist()
        self.launch(lambda:workflow.run(self.plan,**options),self.applied)

    def applied(self,result):
        if getattr(result,'dry_run',False):
            self.status.set('Dry run complete · no media files, NFOs or tags changed')
            self.log('Dry run complete. Turn off Dry run to apply the reviewed changes.')
            return
        self.last_manifest=result.manifest
        self.log(f'File operation recorded: {result.manifest}')
        self.log(f'{result.completed} completed; {result.failures} issue(s). NFO: {getattr(result,"nfo_created",0)}; embedded tags: {getattr(result,"tags_updated",0)}.')
        self.status.set(f'Files: {result.completed} · Tags: {getattr(result,"tags_updated",0)} · NFO: {getattr(result,"nfo_created",0)} · Issues: {result.failures}'+(' · cancelled' if result.cancelled else ''))
        if result.paths:
            self.metadata_path=Path(result.paths[0]);self.metadata_note.set(str(self.metadata_path))
        self.invalidate_plan()
        self.invalidate_report('Files or metadata may have changed. Analyse again to refresh the report.')

    def latest_journal(self,folder):
        candidates=sorted((support.state_root()/folder).glob('*.json'),key=lambda p:p.stat().st_mtime_ns,reverse=True)
        for path in candidates:
            try:
                data=json.loads(path.read_text(encoding='utf-8'))
                if not data.get('undone') and data.get('status')!='undone':
                    return path
            except (ValueError,OSError):
                continue
        return None

    def undo_files(self):
        if self.thread:
            return
        path=self.latest_journal('manifests')
        if not path:
            self.status.set('No file operation is waiting to be undone')
            return
        if messagebox.askyesno('Undo file operation','Restore linked tag values, remove unchanged generated NFOs, then undo the latest file operation?',parent=self.root):
            tools=self.tool_paths()
            self.launch(lambda:workflow.undo_run(path,self.cancel,self.worker_log,self.worker_progress,tools),self.applied)

    def analyse(self):
        try:
            source=self.get_source()
            recursive=self.recursive.get()
            tools=self.tool_paths()
        except Exception as exc:
            messagebox.showwarning('Cannot analyse',str(exc),parent=self.root)
            return
        self.notebook.select(self.analytics_tab)
        self.analytics_book.select(self.media_stats_tab)
        self.analytics_note.set('Reading container and stream headers. Large libraries and network shares can take time.')
        def task():
            paths=core.discover_movies(source,recursive=recursive)
            return media.scan_library(paths,self.cancel,self.worker_progress,support.state_root()/'cache'/'media.json',tools,external_subtitles=core.discover_subtitles(paths))
        self.launch(task,self.receive_report)

    def receive_report(self,report):
        self.report=report
        summary=report.summary
        total=summary.get('files_total',len(report.records))
        self.metric_vars['Files'].set(str(total))
        self.metric_vars['Storage'].set(f'{summary.get("total_size",0)/1024**3:,.1f} GiB')
        self.metric_vars['Video codecs'].set(str(len(summary.get('video_codecs',{}))))
        resolutions=summary.get('resolutions',{})
        self.metric_vars['4K / 8K'].set(f'{resolutions.get("4K",0)} / {resolutions.get("8K",0)}')
        self.metric_vars['HDR files'].set(str(sum(1 for record in report.records if any(h not in ('SDR','Unknown') for v in record.get('video',[]) for h in v.get('hdr',[])))))
        self.category_tree.delete(*self.category_tree.get_children())
        self.category_map={}
        for category,label in (('containers','Containers'),('video_codecs','Video codecs'),('audio_codecs','Audio codecs'),('resolutions','Resolution'),('hdr','HDR formats'),('bit_depths','Video bit depth'),('video_profiles','Video profiles'),('frame_rates','Frame rates'),('chroma_subsampling','Chroma subsampling'),('audio_channels','Audio channels'),('audio_sample_rates','Audio sample rates'),('audio_bit_depths','Audio bit depth'),('audio_profiles','Audio profiles'),('audio_languages','Audio languages'),('subtitle_codecs','Subtitle codecs'),('subtitle_languages','Embedded subtitle languages'),('external_subtitle_codecs','External subtitle codecs'),('external_subtitle_languages','External subtitle languages')):
            self.category_tree.insert('','end',values=(label,''),tags=('heading',))
            for value,count in sorted(summary.get(category,{}).items(),key=lambda pair:(-pair[1],pair[0])):
                iid=self.category_tree.insert('','end',values=('    '+str(value),count))
                self.category_map[iid]=(category,str(value))
        self.category_tree.tag_configure('heading',foreground=ACCENT)
        self.category_filter=None
        self.populate_records()
        self.analytics_note.set(f'{total} files · {summary.get("files_failed",len(report.errors))} unreadable · {report.engine}. Category counts are distinct files; a movie with several audio codecs may appear in several categories. External subtitles: {summary.get("external_subtitles_total",0)}. Double-click for every stream and raw fields.')
        self.status.set(f'Analysis complete · {total} files · {len(report.errors)} issue(s)')
        self.log(self.analytics_note.get())

    def invalidate_report(self,message):
        self.report=None
        self.analytics_tree.delete(*self.analytics_tree.get_children())
        self.category_tree.delete(*self.category_tree.get_children())
        self.category_filter=None
        for variable in self.metric_vars.values(): variable.set('—')
        self.analytics_note.set(message)
        self.collections_report=None
        self.collections_tree.delete(*self.collections_tree.get_children())
        self.collection_members.delete(*self.collection_members.get_children())
        self.collections_note.set('Library changed. Check film collections again to refresh the inventory.')
        self.collection_detail.set('Collection results will appear after checking the current library.')

    def record_values(self,record,category):
        if category=='containers': return [record.get('container','Unknown')]
        if category=='video_codecs': return [v.get('codec','Unknown') for v in record.get('video',[])]
        if category=='audio_codecs': return [v.get('codec','Unknown') for v in record.get('audio',[])]
        if category=='resolutions': return [v.get('resolution','Unknown') for v in record.get('video',[])]
        if category=='hdr': return [h for v in record.get('video',[]) for h in v.get('hdr',[])]
        if category=='bit_depths': return [str(v.get('bit_depth')) if v.get('bit_depth') is not None else 'Unknown' for v in record.get('video',[])]
        mapping={'video_profiles':('video','profile'),'frame_rates':('video','frame_rate'),'chroma_subsampling':('video','chroma_subsampling'),'audio_channels':('audio','channels'),'audio_sample_rates':('audio','sample_rate'),'audio_bit_depths':('audio','bit_depth'),'audio_profiles':('audio','profile')}
        if category in mapping:
            kind,key=mapping[category]
            return [str(v[key]) if v.get(key) is not None and v[key]!='' else 'Unknown' for v in record.get(kind,[])]
        if category=='audio_languages': return [v.get('language','Unknown') for v in record.get('audio',[])]
        if category=='subtitle_languages': return [v.get('language','Unknown') for v in record.get('text',[])]
        if category=='external_subtitle_codecs': return [v.get('codec','Unknown') for v in record.get('external_text',[])]
        if category=='external_subtitle_languages': return [v.get('language','Unknown') for v in record.get('external_text',[])]
        if category=='subtitle_codecs': return [v.get('codec','Unknown') for v in record.get('text',[])]
        return []

    def populate_records(self):
        self.analytics_tree.delete(*self.analytics_tree.get_children())
        if not self.report:
            return
        query=self.search.get().casefold()
        for index,record in enumerate(self.report.records):
            if query and query not in json.dumps(record,ensure_ascii=False).casefold():
                continue
            if self.category_filter:
                category,value=self.category_filter
                if value not in self.record_values(record,category): continue
            videos=record.get('video',[])
            audio=record.get('audio',[])
            self.analytics_tree.insert('','end',iid=str(index),values=(Path(record['path']).name,record.get('container','Unknown'),', '.join(dict.fromkeys(v.get('codec','Unknown') for v in videos)),', '.join(dict.fromkeys(v.get('resolution','Unknown') for v in videos)),', '.join(dict.fromkeys(h for v in videos for h in v.get('hdr',[]))),', '.join(dict.fromkeys(v.get('codec','Unknown') for v in audio))))

    def filter_category(self,event=None):
        selected=self.category_tree.selection()
        self.category_filter=self.category_map.get(selected[0]) if selected else None
        self.populate_records()

    def clear_filter(self):
        self.search.set('')
        self.category_filter=None
        self.category_tree.selection_remove(self.category_tree.selection())
        self.populate_records()

    def check_collections(self):
        if self.thread: return
        try:
            source=self.get_source()
            key=self.api_key.get().strip()
            if not key: raise ValueError('Film collections need a TMDB API key in Connections & tools. You can keep TVDB selected for movie naming.')
            language=self.language.get().strip() or 'en'
            recursive=self.recursive.get();refresh=self.collection_refresh.get()
            known={}
            for group in self.plan:
                if group.approved and group.resolved and getattr(group,'provider','TMDB')=='TMDB':
                    identifier=getattr(group,'provider_id',None) or group.tmdb_id
                    if identifier:
                        for path in group.video_paths: known[str(path.resolve())]=identifier
        except Exception as exc:
            messagebox.showwarning('Cannot check collections',str(exc),parent=self.root);return
        self.notebook.select(self.analytics_tab);self.analytics_book.select(self.collections_tab)
        self.collections_note.set('Checking TMDB movie and collection records. Only parsed titles, years and provider IDs are sent; no files are uploaded.')
        def task():
            paths=core.discover_movies(source,recursive=recursive)
            return collections_api.analyse_collections(paths,key,language,self.cancel,self.worker_progress,
                support.state_root()/'cache'/'collections.json',known_matches=known,force_refresh=refresh)
        self.launch(task,self.receive_collections)

    def receive_collections(self,report):
        self.collections_report=report
        self.collections_tree.delete(*self.collections_tree.get_children());self.collection_members.delete(*self.collection_members.get_children())
        missing=0;owned=0;upcoming=0
        for index,collection in enumerate(report.collections):
            have=len(collection.get('owned',[]));absent=len(collection.get('missing',[]));future=len(collection.get('upcoming',[]))
            owned+=have;missing+=absent;upcoming+=future
            self.collections_tree.insert('','end',iid=str(index),values=(collection.get('name','Collection'),have,absent,future))
        uncertainty=len(report.unmatched)
        self.collections_note.set(f'{len(report.collections)} collections · {owned} owned members · {missing} released films not found among confirmed matches · {upcoming} upcoming/undated · {uncertainty} unmatched files · {len(report.errors)} issue(s). Unmatched files can affect completeness; review them before treating a film as missing.')
        self.collection_detail.set('Select a collection to see its films. Owned films are matched by TMDB ID; upcoming or undated films are listed separately.')
        self.status.set(f'Collection check complete · {len(report.collections)} collections · {missing} not found · {uncertainty} unmatched')
        self.log(self.collections_note.get())
        if report.collections: self.collections_tree.selection_set('0')

    def select_collection(self,event=None):
        selected=self.collections_tree.selection()
        self.collection_members.delete(*self.collection_members.get_children())
        self.collection_member_records={}
        if not selected or not self.collections_report: return
        collection=self.collections_report.collections[int(selected[0])]
        for category,label in (('owned','Owned'),('missing','Not found'),('upcoming','Upcoming / TBA')):
            for movie in collection.get(category,[]):
                paths=' | '.join(movie.get('paths',[]))
                iid=self.collection_members.insert('','end',values=(label,movie.get('title',''),movie.get('year') or '—',movie.get('release_date') or 'TBA',paths),tags=(category,))
                self.collection_member_records[iid]=movie
        self.collection_detail.set(collection.get('name','Collection')+' · not-found results are based on confirmed matches in the scanned folder and its selected subfolders.')

    def select_collection_member(self,event=None):
        selected=self.collection_members.selection()
        if not selected: return
        movie=self.collection_member_records[selected[0]]
        paths='\n'.join(movie.get('paths',[]))
        self.collection_detail.set(f'{movie.get("title","")} ({movie.get("year") or "TBA"}) · {movie.get("status","")}'+('\n'+paths if paths else ''))

    def show_collection_issues(self):
        if not self.collections_report:
            self.status.set('Check film collections first');return
        dialog=self.new_dialog('Collection matching · unmatched files and errors',1000,650)
        self.readonly_text(dialog,json.dumps({'unmatched':self.collections_report.unmatched,'errors':self.collections_report.errors,'notes':self.collections_report.notes},ensure_ascii=False,indent=2))

    def export_collections(self,format):
        if not self.collections_report:
            self.status.set('Check film collections before exporting');return
        path=filedialog.asksaveasfilename(parent=self.root,defaultextension='.'+format,initialfile='MMO-film-collections.'+format,filetypes=[(format.upper(),'*.'+format)])
        if path:
            try:
                collections_api.export_collections(self.collections_report,path,format)
                self.status.set('Exported film collections')
            except Exception as exc: messagebox.showerror('Export failed',str(exc),parent=self.root)

    def inspect_selected(self):
        selected=self.analytics_tree.selection()
        if not selected or not self.report: return
        record=self.report.records[int(selected[0])]
        dialog=self.new_dialog('Media details · '+Path(record['path']).name,1050,720)
        book=ttk.Notebook(dialog)
        book.pack(fill='both',expand=True,padx=16,pady=16)
        for key,label in (('general','Container'),('video','Video'),('audio','Audio'),('text','Embedded subtitles'),('external_text','External subtitles'),('tags','File tags'),('raw','All raw fields')):
            page=ttk.Frame(book)
            book.add(page,text=label)
            content=record if key=='general' else record.get(key,[])
            if key=='general':
                content={k:v for k,v in record.items() if k not in ('video','audio','text','external_text','raw','tags','other_tracks')}
            if key=='raw':
                self.readonly_text(page,json.dumps(content,ensure_ascii=False,indent=2,default=str))
                continue
            reader=ttk.Frame(page);reader.pack(side='bottom',fill='x',pady=(8,0))
            ttk.Label(reader,text='Select a property to read its full value below.',style='Muted.TLabel').pack(anchor='w',pady=4)
            full=self.readonly_text(reader,'',height=6)
            tree=self.table(page,[('property','Stream / property',300),('value','Detected value',650)])
            full_values={}
            def show_value(event=None,tree=tree,full=full,values=full_values):
                selected=tree.selection()
                if selected:
                    full.configure(state='normal');full.delete('1.0','end');full.insert('1.0',values.get(selected[0],''));full.configure(state='disabled')
            tree.bind('<<TreeviewSelect>>',show_value)
            tree.bind('<Double-1>',show_value)
            tree.tag_configure('heading',foreground=ACCENT)
            streams=content if isinstance(content,list) else [content]
            if not streams:
                tree.insert('','end',values=('No streams reported',''))
            for index,stream in enumerate(streams,1):
                if isinstance(content,list):
                    tree.insert('','end',values=(f'Stream {index} · {stream.get("codec",stream.get("format",""))}',''),tags=('heading',))
                for name,value in stream.items():
                    if isinstance(value,(dict,list)):
                        value=json.dumps(value,ensure_ascii=False,default=str)
                    display='Not reported' if value is None or value=='' else str(value)
                    iid=tree.insert('','end',values=(str(name).replace('_',' ').capitalize(),display))
                    full_values[iid]=display

    def readonly_text(self,parent,value,height=None):
        frame=ttk.Frame(parent);frame.pack(fill='both',expand=True)
        wrap=tk.BooleanVar(value=True)
        controls=ttk.Frame(frame);controls.grid(row=0,column=0,columnspan=2,sticky='ew')
        text=tk.Text(frame,background=FIELD,foreground=TEXT,selectbackground='#214968',font=('Consolas',10),relief='flat',wrap='word',padx=14,pady=10,height=height or 16)
        ttk.Checkbutton(controls,text='Wrap long text',style='Footer.TCheckbutton',variable=wrap,command=lambda:text.configure(wrap='word' if wrap.get() else 'none')).pack(side='left')
        ttk.Button(controls,text='Copy text',style='Quiet.TButton',command=lambda:(text.clipboard_clear(),text.clipboard_append(text.get('1.0','end-1c')))).pack(side='right')
        vertical=ttk.Scrollbar(frame,orient='vertical',command=text.yview)
        horizontal=ttk.Scrollbar(frame,orient='horizontal',command=text.xview)
        text.configure(yscrollcommand=vertical.set,xscrollcommand=horizontal.set)
        text.grid(row=1,column=0,sticky='nsew');vertical.grid(row=1,column=1,sticky='ns');horizontal.grid(row=2,column=0,sticky='ew')
        frame.rowconfigure(1,weight=1);frame.columnconfigure(0,weight=1)
        text.insert('1.0',value);text.configure(state='disabled')
        text._wrap_variable=wrap
        return text

    def export(self,format):
        if not self.report:
            self.status.set('Analyse a folder before exporting')
            return
        path=filedialog.asksaveasfilename(parent=self.root,defaultextension='.'+format,initialfile='MMO-library-report.'+format,filetypes=[(format.upper(),'*.'+format)])
        if path:
            try:
                media.export_report(self.report,path,format)
                self.status.set('Exported '+Path(path).name)
            except Exception as exc:
                messagebox.showerror('Export failed',str(exc),parent=self.root)

    def export_counts(self):
        if not self.report:
            self.status.set('Analyse a folder before exporting')
            return
        path=filedialog.asksaveasfilename(parent=self.root,defaultextension='.csv',initialfile='MMO-library-counts.csv',filetypes=[('CSV','*.csv')])
        if path:
            try:
                import csv
                with open(path,'w',encoding='utf-8-sig',newline='') as stream:
                    writer=csv.writer(stream)
                    writer.writerow(['category','value','count','basis'])
                    for category,counts in self.report.summary.items():
                        if isinstance(counts,dict) and category!='stream_counts':
                            for value,count in counts.items():
                                writer.writerow([category,value,count,'external subtitle files' if category.startswith('external_subtitle') else 'distinct video files'])
                    for category,counts in self.report.summary.get('stream_counts',{}).items():
                        for value,count in counts.items():
                            writer.writerow([category,value,count,'streams'])
                self.status.set('Exported library counts')
            except OSError as exc:
                messagebox.showerror('Export failed',str(exc),parent=self.root)

    def choose_metadata(self):
        path=filedialog.askopenfilename(parent=self.root,title='Choose a video for embedded title correction',filetypes=[('Video files','*.mp4 *.m4v *.mov *.mkv'),('All files','*.*')])
        if path:
            self.metadata_path=Path(path)
            guess=core.parse_movie(self.metadata_path)
            self.metadata_title.set(guess.title)
            self.metadata_year.set(str(guess.year or ''))
            self.metadata_comment.set(f'{guess.title} ({guess.year})' if guess.year else guess.title)
            self.metadata_note.set(str(self.metadata_path))
            self.metadata_consent.set(False)

    def update_metadata(self):
        if self.thread:
            return
        if not self.metadata_path or not self.metadata_path.is_file():
            messagebox.showwarning('Choose a file','Choose an existing video or select a movie in the plan.',parent=self.root)
            return
        if not self.metadata_consent.get():
            messagebox.showinfo('Embedded edits are optional','Tick the confirmation to modify embedded tags. Filename renaming alone leaves these tags unchanged.',parent=self.root)
            return
        title=self.metadata_title.get().strip()
        year=self.metadata_year.get().strip()
        if not title or (year and (not year.isdigit() or len(year)!=4)):
            messagebox.showwarning('Check metadata','Enter a title and an optional four-digit year.',parent=self.root)
            return
        path=self.metadata_path
        tools=self.tool_paths()
        comment=self.metadata_comment.get() if self.metadata_comment_enabled.get() else None
        self.launch(lambda:media.update_tags(path,title,int(year) if year else None,journal_dir=support.state_root()/'metadata',tool_paths=tools,comment=comment),self.metadata_done)

    def metadata_done(self,result):
        self.log(str(result.get('details',result)))
        self.status.set('Embedded tags · '+str(result.get('status','finished')))
        self.metadata_consent.set(False)
        self.invalidate_report('Embedded tags changed. Analyse again to refresh technical details and cached tags.')
        self.invalidate_plan()

    def undo_metadata(self):
        if self.thread: return
        path=self.latest_journal('metadata')
        if not path:
            self.status.set('No embedded tag edit is waiting to be restored')
            return
        tools=self.tool_paths()
        self.launch(lambda:workflow.restore_tag_edit(path,tool_paths=tools),self.metadata_done)

    def unlock(self):
        if self.store is None:
            self.store=support.CredentialsStore(self.portable.get())
        if self.store.portable and not self.store._passphrase:
            value=simpledialog.askstring('MMO portable vault','Enter the vault passphrase:',show='•',parent=self.root)
            if not value: return False
            self.store.set_session_passphrase(value)
            try:
                self.store._load_vault()
            except Exception:
                self.store.set_session_passphrase(None)
                raise
        return True

    def selected_api_key(self):
        return self.tvdb_key if self.provider.get()=='TVDB' else self.api_key

    def make_provider(self):
        name=self.provider.get();key=self.selected_api_key().get().strip()
        if not key: raise ValueError(f'Enter a {name} API key in Connections & tools, or use filename-only matching.')
        language=self.language.get().strip() or 'en'
        return TVDBClient(key,language,self.cancel,pin=self.tvdb_pin.get().strip()) if name=='TVDB' else core.TMDBClient(key,language,self.cancel)

    def save_key(self):
        try:
            key=self.selected_api_key().get().strip()
            if not key: raise ValueError(f'Enter a {self.provider.get()} API key first.')
            if not self.unlock(): return
            saved=self.store.set('tvdb_key' if self.provider.get()=='TVDB' else 'tmdb_key',key)
            if self.provider.get()=='TVDB': saved=self.store.set('tvdb_pin',self.tvdb_pin.get().strip()) and saved
            self.status.set('Key saved securely' if saved else 'Key available for this session · secure storage unavailable')
            self.online.set(True)
        except Exception as exc: messagebox.showwarning('Could not save key',support.redact_secrets(exc),parent=self.root)

    def load_key(self):
        try:
            if not self.unlock(): return
            value=self.store.get('tvdb_key' if self.provider.get()=='TVDB' else 'tmdb_key')
            if value:
                self.selected_api_key().set(value)
                if self.provider.get()=='TVDB': self.tvdb_pin.set(self.store.get('tvdb_pin') or '')
                self.online.set(True);self.status.set(f'Saved {self.provider.get()} key loaded')
            else: self.status.set('No saved key found for this provider')
        except Exception as exc: messagebox.showwarning('Could not load key',support.redact_secrets(exc),parent=self.root)

    def test_key(self):
        try: client=self.make_provider()
        except Exception as exc:
            self.status.set(str(exc));return
        name=self.provider.get()
        def task():
            if not client.test(): raise ValueError(name+' did not confirm this connection.')
        self.launch(task,lambda _:self.status.set(name+' connection succeeded'))

    def use_bundled_tools(self):
        for variable in self.tool_vars.values(): variable.set('')
        self.persist();self.tool_status.set(self.detect_tools());self.status.set('Bundled tools selected automatically')

    def detect_tools(self):
        found=[];missing=[]
        for name,label in (('mediainfo','MediaInfo CLI'),('ffprobe','FFprobe'),('mkvpropedit','MKVPropEdit'),('mkvextract','MKVExtract')):
            (found if media.find_tool(name,{}) else missing).append(label)
        return ('Available automatically: '+', '.join(found) if found else 'No bundled command-line tools found.')+('. Missing: '+', '.join(missing) if missing else '')

    def change_portable(self):
        old=support.portable_mode_enabled()
        try:
            support.check_state_writable(self.portable.get())
            support._PORTABLE_OVERRIDE=self.portable.get()
            self.store=support.CredentialsStore(self.portable.get())
            support.configure_logging()
            self.persist()
            self.status.set('Storage mode changed · save the key again to store it in this mode')
        except Exception as exc:
            support._PORTABLE_OVERRIDE=old
            self.portable.set(old)
            messagebox.showwarning('Storage mode',str(exc),parent=self.root)

    def persist(self):
        settings={'portable':self.portable.get(),'language':self.language.get().strip() or 'en','mode':MODES[self.mode.get()],'recursive':self.recursive.get(),'last_window_size':[round(self.root.winfo_width()/self.scale),round(self.root.winfo_height()/self.scale)]}
        settings.update({'provider':self.provider.get(),'dry_run':self.dry_run.get(),'write_nfo':self.write_nfo.get(),'embedded_tags':self.embedded_tags.get(),'replace_comments':self.replace_comments.get(),'verification_mode':self.verification_mode.get(),'verification_method':self.verification_method.get(),'quick_sample':self.quick_sample.get()})
        settings.update({key:variable.get().strip() for key,variable in self.tool_vars.items()})
        try:
            support.save_settings(settings)
        except Exception as exc:
            self.log(f'Settings could not be saved: {exc}')

    def show_activity(self):
        if hasattr(self,'activity_text') and self.activity_text.winfo_exists():
            self.activity_text.winfo_toplevel().lift()
            return
        dialog=self.new_dialog('MMO activity',1000,600)
        self.activity_text=self.readonly_text(dialog,'\n'.join(getattr(self,'activity_lines',[])))
        def export_activity():
            path=filedialog.asksaveasfilename(parent=dialog,defaultextension='.txt',initialfile='MMO-activity.txt')
            if path:
                Path(path).write_text('\n'.join(getattr(self,'activity_lines',[])),encoding='utf-8')
        ttk.Button(dialog,text='Export activity',command=export_activity).pack(side='bottom',anchor='e',padx=12,pady=10)

    def about(self):
        dialog=self.new_dialog('About MetaMovie Organiser',700,440)
        frame=ttk.Frame(dialog,padding=24)
        frame.pack(fill='both',expand=True)
        if self.mark:
            ttk.Label(frame,image=self.mark).pack(anchor='w')
        ttk.Label(frame,text=APP_NAME,style='Heading.TLabel').pack(anchor='w',pady=(12,6))
        ttk.Label(frame,text='Version '+APP_VERSION,style='Accent.TLabel').pack(anchor='w',pady=(0,18))
        ttk.Label(frame,text='Movie naming, embedded title correction and media analytics.\n\nThis product uses the TMDB API but is not endorsed or certified by TMDB.\nMetadata is also provided by TheTVDB.\nMedia analysis is powered by MediaInfo.\n\nCopyright © 2026 James. Application source: MIT.\nBundled components retain their own licences; see the included attribution.',style='Muted.TLabel',wraplength=620,justify='left').pack(anchor='w')
        ttk.Button(frame,text='Close',command=dialog.destroy).pack(anchor='e',pady=16)

    def close(self):
        self.persist()
        if self.thread:
            self.closing=True
            self.request_cancel()
        else:
            self.closed=True
            self.root.destroy()


def main():
    enable_dpi()
    root=tk.Tk()
    MovieApp(root)
    root.mainloop()


if __name__=='__main__':
    main()
