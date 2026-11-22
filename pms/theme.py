#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主题与样式

浅色 / 深色 / 跟随系统的 QSS 生成（含界面缩放）。
"""

from __future__ import annotations

from pms.constants import (
    PMS_IS_WINDOWS,
    PMS_THEME_DARK,
    PMS_THEME_LIGHT,
    PMS_THEME_NATIVE,
    PMS_THEME_SYSTEM,
)

class PmsThemeManager:
    """浅色 / 深色主题（QSS），可跟随系统。"""

    pthm_LIGHT = PMS_THEME_LIGHT
    pthm_DARK = PMS_THEME_DARK

    def pthm_system_theme(self) -> str:
        """读注册表判断系统是浅色还是深色。"""
        if not PMS_IS_WINDOWS:
            return self.pthm_LIGHT
        try:
            import winreg  # type: ignore

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return self.pthm_LIGHT if int(value) == 1 else self.pthm_DARK
        except OSError:
            return self.pthm_DARK

    def pthm_resolve(self, pms_theme: str) -> str:
        if pms_theme == PMS_THEME_SYSTEM:
            return self.pthm_system_theme()
        if pms_theme == PMS_THEME_NATIVE:
            return PMS_THEME_NATIVE
        return pms_theme if pms_theme in (self.pthm_LIGHT, self.pthm_DARK) else self.pthm_DARK

    def pthm_is_dark(self, pms_theme: str) -> bool:
        """当前主题是否按深色处理（原生模式走系统判定，用于表格状态染色）。"""
        resolved = self.pthm_resolve(pms_theme)
        if resolved == PMS_THEME_NATIVE:
            return self.pthm_system_theme() == self.pthm_DARK
        return resolved == self.pthm_DARK

    def pthm_build_qss(self, pms_theme: str, pms_scale: float = 1.0) -> str:
        """生成全局样式表（含字体缩放，适配不同分辨率）。"""
        dark = self.pthm_resolve(pms_theme) == self.pthm_DARK
        base_size = max(8.0, 9.5 * max(0.75, min(2.0, float(pms_scale))))
        # v1.9.7：原生模式不套任何自定义 QSS，让 Qt 用系统默认绘制
        if pms_theme == PMS_THEME_NATIVE:
            return ""
        if dark:
            window = "#1e2228"
            panel = "#252a31"
            panel_alt = "#2c323a"
            border = "#3a424c"
            text = "#e6e9ee"
            sub_text = "#9aa4b1"
            accent = "#3d8bfd"
            accent_hover = "#5b9dff"
            selection = "#2f4a72"
            danger = "#e5534b"
            ok = "#3fb950"
        else:
            window = "#f4f6f9"
            panel = "#ffffff"
            panel_alt = "#eef1f5"
            border = "#cfd6e0"
            text = "#1f2430"
            sub_text = "#5b6472"
            accent = "#1f6feb"
            accent_hover = "#3b82f6"
            selection = "#cfe1ff"
            danger = "#c1352c"
            ok = "#1a7f37"
        return f"""
        QWidget {{ background-color: {window}; color: {text}; font-size: {base_size:.1f}pt; }}
        QMainWindow, QDialog {{ background-color: {window}; }}
        QGroupBox {{ border: 1px solid {border}; border-radius: 6px; margin-top: 14px; padding: 8px 8px 6px 8px; background-color: {panel}; }}
        QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 10px; padding: 0 4px; color: {accent}; }}
        QPushButton {{ background-color: {panel_alt}; border: 1px solid {border}; border-radius: 5px; padding: 5px 12px; min-height: 22px; }}
        QPushButton:hover {{ border-color: {accent}; color: {accent}; }}
        QPushButton:pressed {{ background-color: {selection}; }}
        QPushButton:disabled {{ color: {sub_text}; border-color: {border}; }}
        QPushButton#primaryButton {{ background-color: {accent}; border-color: {accent}; color: #ffffff; font-weight: bold; }}
        QPushButton#primaryButton:hover {{ background-color: {accent_hover}; }}
        QPushButton#dangerButton {{ background-color: {danger}; border-color: {danger}; color: #ffffff; }}
        QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
            background-color: {panel}; border: 1px solid {border}; border-radius: 5px; padding: 4px 6px; selection-background-color: {accent}; }}
        QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {accent}; }}
        QComboBox::drop-down {{ border: none; width: 18px; }}
        QComboBox QAbstractItemView {{ background-color: {panel}; border: 1px solid {border}; selection-background-color: {selection}; }}
        QTableView, QTableWidget, QTreeWidget, QListWidget {{
            background-color: {panel}; alternate-background-color: {panel_alt}; gridline-color: {border}; border: 1px solid {border}; }}
        QHeaderView::section {{ background-color: {panel_alt}; border: none; border-right: 1px solid {border}; border-bottom: 1px solid {border}; padding: 4px 6px; }}
        QTableView::item:selected, QTreeWidget::item:selected, QListWidget::item:selected {{ background-color: {selection}; color: {text}; }}
        QTabWidget::pane {{ border: 1px solid {border}; border-radius: 5px; background-color: {panel}; }}
        QTabBar::tab {{ background: {panel_alt}; border: 1px solid {border}; border-bottom: none; padding: 5px 10px; margin-right: 2px; border-top-left-radius: 5px; border-top-right-radius: 5px; }}
        QTabBar::tab:selected {{ background: {panel}; color: {accent}; font-weight: bold; }}
        QToolBar {{ background-color: {panel}; border-bottom: 1px solid {border}; spacing: 4px; padding: 3px; }}
        QToolButton {{ background-color: transparent; border: 1px solid transparent; border-radius: 4px; padding: 3px 8px; }}
        QToolButton:hover {{ border-color: {accent}; color: {accent}; background-color: {panel_alt}; }}
        QToolButton:checked {{ background-color: {selection}; border-color: {accent}; color: {accent}; }}
        QStatusBar {{ background-color: {panel}; border-top: 1px solid {border}; }}
        QStatusBar QLabel {{ padding: 0 6px; }}
        QProgressBar {{ background-color: {panel_alt}; border: 1px solid {border}; border-radius: 5px; text-align: center; height: 16px; }}
        QProgressBar::chunk {{ background-color: {accent}; border-radius: 4px; }}
        QScrollBar:vertical {{ background: {panel}; width: 12px; margin: 0; }}
        QScrollBar::handle:vertical {{ background: {border}; border-radius: 6px; min-height: 24px; }}
        QScrollBar::handle:vertical:hover {{ background: {accent}; }}
        QScrollBar:horizontal {{ background: {panel}; height: 12px; }}
        QScrollBar::handle:horizontal {{ background: {border}; border-radius: 6px; min-width: 24px; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
        QSplitter::handle {{ background-color: {border}; }}
        QSplitter::handle:horizontal {{ width: 3px; }}
        QSplitter::handle:vertical {{ height: 3px; }}
        QLabel#hintLabel {{ color: {sub_text}; }}
        QLabel#okLabel {{ color: {ok}; font-weight: bold; }}
        QLabel#errorLabel {{ color: {danger}; font-weight: bold; }}
        QLabel#accentLabel {{ color: {accent}; font-weight: bold; }}
        QCheckBox::indicator, QRadioButton::indicator {{ width: 14px; height: 14px; }}
        """
