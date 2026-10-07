# -*- coding: utf-8 -*-
r"""
视觉层：一套 ttk 主题 + 两组配色（深色 / 浅色），不引入任何新依赖。

用法：
    from gui_theme import apply
    apply(root, ttk.Style())          # 建控件之前调用

设计约束：
  * 颜色一律从 gui_common 的 DARK_UI 派生。这台机器上系统把经典控件底色给成近黑，
    同一份代码在浅色系统上也必须照常好看，所以每个角色色都配一对。
  * 只改样式，不动布局结构 —— 布局在 2026-09 那轮重做里已经定稿。
  * Vista 主题能改的就这些：Treeview 做不了隔行换色（要逐行打 tag），就不硬做。
"""

import tkinter.font as tkfont

from gui_common import (C_BAD, C_INFO, C_MUTED, C_OK, C_WARN, DARK_UI)

FONT_NAME = 'Microsoft YaHei UI'
FONT_UI = (FONT_NAME, 9)
FONT_UI_BOLD = (FONT_NAME, 9, 'bold')
FONT_STEP = (FONT_NAME, 9, 'bold')          # ① ② ③ 这种分区标题
FONT_MONO = ('Consolas', 9)

if DARK_UI:
    WINDOW = '#202124'        # 窗口底
    SURFACE = '#2a2b2f'       # 输入框 / 列表 / 按钮等"浮起来"的面
    SURFACE_HI = '#35363c'    # 悬停 / 按下
    TEXT = '#e8eaed'
    BORDER = '#3c4043'
    ACCENT = '#8ab4f8'
    ACCENT_FG = '#10161f'     # 主按钮上的字（浅底配深字）
    SELECTION = '#39435c'
    TROUGH = '#1b1c1e'        # 进度条槽
else:
    WINDOW = '#f3f3f3'
    SURFACE = '#ffffff'
    SURFACE_HI = '#e8eaed'
    TEXT = '#1f1f1f'
    BORDER = '#d4d7da'
    ACCENT = '#1a73e8'
    ACCENT_FG = '#ffffff'
    SELECTION = '#cfe1ff'
    TROUGH = '#e3e5e8'

LOG_BG = '#1b1c1e'            # 日志区固定深色，两种主题下都一样（终端观感）
LOG_FG = '#d4d4d4'


def apply(root, style):
    """把主题装到 root 上。必须在创建其它控件之前调用。"""
    try:
        if 'vista' in style.theme_names():
            style.theme_use('vista')
    except Exception:
        pass

    try:
        base = tkfont.nametofont('TkDefaultFont')
        base.configure(family=FONT_NAME, size=9)
        tkfont.nametofont('TkTextFont').configure(family=FONT_NAME, size=9)
        tkfont.nametofont('TkMenuFont').configure(family=FONT_NAME, size=9)
        tkfont.nametofont('TkHeadingFont').configure(family=FONT_NAME, size=9, weight='bold')
    except Exception:
        pass
    root.option_add('*Font', FONT_UI)
    root.configure(bg=WINDOW)

    for cls in ('TFrame', 'TLabel', 'TLabelframe', 'TCheckbutton', 'TRadiobutton',
                'TNotebook', 'TNotebook.Tab'):
        style.configure(cls, background=WINDOW, foreground=TEXT)
    style.configure('TLabelframe', background=WINDOW, bordercolor=BORDER,
                    relief='flat', borderwidth=1)
    # 分区标题（① ② ③ ④）加粗，和正文拉开一级
    style.configure('TLabelframe.Label', font=FONT_STEP, background=WINDOW, foreground=TEXT)

    style.configure('TButton', font=FONT_UI, padding=(10, 4), relief='flat',
                    background=SURFACE, foreground=TEXT, borderwidth=0,
                    focusthickness=0, focuscolor=WINDOW)
    style.map('TButton',
              background=[('disabled', WINDOW), ('pressed', SURFACE_HI), ('active', SURFACE_HI)],
              foreground=[('disabled', C_MUTED)])
    style.configure('Primary.TButton', font=FONT_UI_BOLD, background=ACCENT,
                    foreground=ACCENT_FG, padding=(12, 5))
    style.map('Primary.TButton',
              background=[('disabled', SURFACE), ('pressed', SURFACE_HI), ('active', SURFACE_HI)],
              foreground=[('disabled', C_MUTED), ('!disabled', ACCENT_FG)])

    for cls in ('TEntry', 'TCombobox', 'TSpinbox'):
        style.configure(cls, fieldbackground=SURFACE, background=SURFACE, foreground=TEXT,
                        arrowcolor=TEXT, bordercolor=BORDER, lightcolor=BORDER,
                        darkcolor=BORDER, insertcolor=TEXT, padding=(5, 3), relief='flat')
        style.map(cls,
                  fieldbackground=[('readonly', SURFACE), ('disabled', WINDOW)],
                  bordercolor=[('focus', ACCENT)],
                  lightcolor=[('focus', ACCENT)],
                  darkcolor=[('focus', ACCENT)],
                  arrowcolor=[('pressed', ACCENT), ('active', ACCENT)])

    style.configure('Treeview', font=FONT_UI, rowheight=26, background=SURFACE,
                    fieldbackground=SURFACE, foreground=TEXT, bordercolor=BORDER,
                    relief='flat', borderwidth=0)
    style.map('Treeview', background=[('selected', SELECTION)],
              foreground=[('selected', TEXT)])
    style.configure('Treeview.Heading', font=FONT_UI_BOLD, background=WINDOW,
                    foreground=C_MUTED, relief='flat', bordercolor=BORDER,
                    padding=(6, 6))
    style.map('Treeview.Heading', background=[('active', SURFACE)], foreground=[('active', TEXT)])

    style.configure('TNotebook', borderwidth=0, tabmargins=(6, 4, 6, 0))
    style.configure('TNotebook.Tab', font=FONT_UI, padding=(16, 7),
                    background=WINDOW, foreground=C_MUTED, borderwidth=0)
    style.map('TNotebook.Tab',
              background=[('selected', SURFACE)],
              foreground=[('selected', TEXT), ('active', TEXT)],
              expand=[('selected', (0, 0, 0, 0))])

    style.configure('Horizontal.TProgressbar', troughcolor=TROUGH, background=ACCENT,
                    borderwidth=0, lightcolor=ACCENT, darkcolor=ACCENT, thickness=14)

    style.configure('Vertical.TScrollbar', background=SURFACE, troughcolor=WINDOW,
                    bordercolor=WINDOW, arrowcolor=C_MUTED, relief='flat', width=12)
    style.map('Vertical.TScrollbar', background=[('active', SURFACE_HI), ('pressed', ACCENT)])

    style.configure('TSeparator', background=BORDER)

    return style
