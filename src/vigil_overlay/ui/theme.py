"""Host-owned Compact Mode styling."""

from __future__ import annotations

from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication

_COMMON_STYLESHEET = """
QWidget#overlayRoot {
    /* The separate DimBackdropWindow owns the full-monitor dimming surface. */
    background-color: transparent;
}
QFrame#compactPanel {
    background-color: transparent;
    border: none;
}
QScrollArea#widgetStripScroller, QWidget#widgetStripContent {
    background-color: transparent;
    border: none;
}
QFrame#overlayHeader,
QFrame#overlayBranding,
QFrame#overlayWidgetStripRow,
QLabel#overlayBrandIcon {
    background-color: transparent;
    border: none;
}
QLabel#overlayBrandTitle {
    background-color: transparent;
    border: none;
    font-size: 18px;
    font-weight: 700;
}
QScrollArea#widgetPageScroller {
    border-radius: 11px;
}
QScrollArea#widgetPageScroller QWidget#widgetPageViewport {
    background-color: transparent;
    border: none;
}
QScrollArea#widgetPageScroller QScrollBar#widgetPageVerticalScrollBar:vertical {
    width: 18px;
    margin: 8px 3px 8px 3px;
    padding: 0;
    border: none;
    border-radius: 8px;
}
QScrollArea#widgetPageScroller QScrollBar#widgetPageVerticalScrollBar::handle:vertical {
    min-height: 56px;
    min-width: 10px;
    margin: 0 2px;
    border: none;
    border-radius: 5px;
}
QScrollArea#widgetPageScroller QScrollBar#widgetPageVerticalScrollBar::add-line:vertical,
QScrollArea#widgetPageScroller QScrollBar#widgetPageVerticalScrollBar::sub-line:vertical {
    height: 0;
    border: none;
    background-color: transparent;
}
QScrollArea#widgetPageScroller QScrollBar#widgetPageVerticalScrollBar::up-arrow:vertical,
QScrollArea#widgetPageScroller QScrollBar#widgetPageVerticalScrollBar::down-arrow:vertical {
    width: 0;
    height: 0;
    background-color: transparent;
}
QScrollArea#widgetPageScroller QScrollBar#widgetPageVerticalScrollBar::add-page:vertical,
QScrollArea#widgetPageScroller QScrollBar#widgetPageVerticalScrollBar::sub-page:vertical {
    background-color: transparent;
}
QPushButton#compactWidgetButton, QPushButton#compactPowerButton {
    border-radius: 9px;
    padding: 0;
}
QPushButton#compactWidgetButton[navigationFocus="true"],
QPushButton#compactPowerButton[navigationFocus="true"] {
    border-width: 3px;
    border-style: solid;
}
QWidget#widgetStripLeftOverflowSlot,
QWidget#widgetStripRightOverflowSlot {
    background-color: transparent;
    border: none;
}
QLabel[widgetStripOverflowIndicator="true"] {
    background-color: transparent;
    border: none;
    padding: 0;
    font-family: "Segoe UI Symbol";
    font-size: 28px;
    font-weight: 600;
}
QWidget[compactPage="true"] {
    background-color: transparent;
    border: none;
}
QLabel#compactPageTitle, QLabel#performanceTitle {
    font-size: 20px;
    font-weight: 700;
}
QLabel#compactPageDescription {
    font-size: 11px;
    padding-bottom: 8px;
}
QLabel#compactEmptyState {
    font-size: 12px;
    padding: 28px 16px;
}
QPushButton#compactListItem {
    border: 1px solid transparent;
    border-radius: 8px;
    min-height: 56px;
    padding: 5px 10px;
    text-align: left;
    font-size: 12px;
    font-weight: 600;
}
QPushButton#compactListItem[navigationFocus="true"] {
    border-width: 3px;
    border-style: solid;
}
QPushButton#compactListItemSecondaryAction[navigationFocus="true"] {
    border-width: 4px;
}
QPushButton#compactListItemSecondaryAction {
    border-width: 2px;
    border-style: solid;
    border-radius: 9px;
    min-width: 54px;
    max-width: 54px;
    min-height: 54px;
    max-height: 54px;
    padding: 0;
    font-size: 20px;
    font-weight: 500;
}

QLabel#audioTitle {
    font-size: 20px;
    font-weight: 700;
}
QLabel#audioSectionLabel {
    font-size: 16px;
    font-weight: 700;
}
QPushButton#audioToggleButton {
    min-height: 56px;
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 0;
    text-align: left;
}
QPushButton#audioVolumeRowButton {
    min-height: 56px;
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 0;
    text-align: left;
}
QPushButton#audioToggleButton[navigationFocus="true"],
QPushButton#audioVolumeRowButton[navigationFocus="true"],
QPushButton#audioSelectorButton[navigationFocus="true"],
QPushButton#wifiProfileSelectorButton[navigationFocus="true"],
QPushButton#displaySelectorButton[navigationFocus="true"] {
    border-width: 3px;
    border-style: solid;
}
QLabel#audioVolumeRowTitle {
    font-size: 14px;
    font-weight: 700;
}
QLabel#audioVolumeValue {
    font-size: 12px;
    font-weight: 600;
}
QSlider#audioVolumeSlider {
    min-height: 18px;
}
QSlider#audioVolumeSlider::groove:horizontal,
QSlider#audioVolumeSlider::sub-page:horizontal,
QSlider#audioVolumeSlider::add-page:horizontal {
    height: 8px;
    border: none;
    border-radius: 4px;
}
QSlider#audioVolumeSlider::handle:horizontal {
    width: 14px;
    height: 14px;
    margin: -3px 0;
    border-radius: 7px;
}
QPushButton#audioSelectorButton, QPushButton#wifiProfileSelectorButton,
QPushButton#displaySelectorButton {
    min-height: 56px;
    max-height: 56px;
    border-width: 2px;
    border-style: solid;
    border-radius: 10px;
    padding: 0 16px;
    text-align: left;
    font-size: 17px;
    font-weight: 600;
}
QFrame#audioDropdownPopup, QFrame#wifiDropdownPopup,
QFrame#displayDropdownPopup {
    border-width: 2px;
    border-style: solid;
    border-radius: 10px;
}
QScrollArea#audioDropdownScroll, QWidget#audioDropdownContent,
QScrollArea#wifiDropdownScroll, QWidget#wifiDropdownContent,
QScrollArea#displayDropdownScroll, QWidget#displayDropdownContent {
    background-color: transparent;
    border: none;
}
QPushButton#audioDropdownOption, QPushButton#wifiDropdownOption,
QPushButton#displayDropdownOption {
    min-height: 38px;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 3px 10px;
    text-align: left;
    font-size: 14px;
    font-weight: 600;
}
QPushButton#audioDropdownOption[navigationFocus="true"],
QPushButton#wifiDropdownOption[navigationFocus="true"],
QPushButton#displayDropdownOption[navigationFocus="true"] {
    border-width: 3px;
    border-style: solid;
}
QLabel#audioErrorLabel, QLabel#audioEmptyMixer {
    font-size: 12px;
    font-weight: 600;
}


QLabel#wifiTitle {
    font-size: 20px;
    font-weight: 700;
}
QLabel#wifiSectionLabel {
    font-size: 16px;
    font-weight: 700;
}
QLabel#wifiStatusLabel, QLabel#wifiHelpLabel, QLabel#wifiErrorLabel {
    font-size: 12px;
    font-weight: 600;
}
QPushButton#wifiToggleButton, QPushButton#wifiActionButton {
    min-height: 56px;
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 0 12px;
    text-align: left;
    font-size: 14px;
    font-weight: 600;
}
QPushButton#wifiToggleButton {
    padding: 0;
}
QPushButton#wifiToggleButton[navigationFocus="true"],
QPushButton#wifiActionButton[navigationFocus="true"] {
    border-width: 3px;
    border-style: solid;
}
QLabel#displayTitle {
    font-size: 20px;
    font-weight: 700;
}
QLabel#displaySectionLabel {
    font-size: 16px;
    font-weight: 700;
}
QFrame#displaySectionUnderline {
    border: none;
    border-radius: 2px;
}
QLabel#displayPageHint {
    font-size: 14px;
    font-weight: 600;
}
QLabel#displayFieldLabel {
    font-size: 15px;
    font-weight: 700;
}
QFrame#displayConfirmationPopup, QFrame#integrationConfirmationPopup {
    border-width: 2px;
    border-style: solid;
    border-radius: 10px;
}
QScrollArea#displayDropdownScroll, QWidget#displayDropdownContent {
    background-color: transparent;
    border: none;
}
QPushButton#displayConfirmationButton, QPushButton#integrationConfirmationAction {
    min-height: 38px;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 3px 10px;
    text-align: left;
    font-size: 14px;
    font-weight: 600;
}
QPushButton#displayConfirmationButton[navigationFocus="true"],
QPushButton#integrationConfirmationAction[navigationFocus="true"] {
    border-width: 3px;
    border-style: solid;
}
QLabel#displayConfirmationTitle, QLabel#integrationConfirmationTitle {
    font-size: 17px;
    font-weight: 700;
}
QLabel#displayConfirmationCountdown, QLabel#displayErrorLabel,
QLabel#integrationConfirmationDetail, QLabel#integrationOperationStatus {
    font-size: 12px;
    font-weight: 600;
}


QLabel#settingsTitle, QLabel#integrationsTitle {
    font-size: 20px;
    font-weight: 700;
}
QLabel#settingsSectionLabel {
    font-size: 16px;
    font-weight: 700;
}
QFrame#settingsSectionUnderline {
    border: none;
    border-radius: 2px;
}
QPushButton#settingsRowButton, QPushButton#integrationRowButton {
    min-height: 58px;
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 0;
    text-align: left;
}
QPushButton#settingsRowButton[navigationFocus="true"],
QPushButton#integrationRowButton[navigationFocus="true"] {
    border-width: 3px;
    border-style: solid;
}
QLabel#settingsRowTitle, QLabel#integrationRowTitle {
    font-size: 14px;
    font-weight: 700;
}
QLabel#settingsRowDescription, QLabel#integrationRowDescription, QLabel#integrationsDescription {
    font-size: 11px;
    font-weight: 400;
}
QLabel#settingsRowTrailing, QLabel#integrationRowStatus, QLabel#integrationRowAction {
    font-size: 13px;
    font-weight: 600;
}
QCheckBox#vigilToggleSwitch {
    background-color: transparent;
}
QLabel#toggleRowTitle {
    font-size: 14px;
    font-weight: 700;
}
QDialog[vigilDialog="true"] {
    background-color: transparent;
}
QFrame#vigilDialogSurface {
    border-width: 1px;
    border-style: solid;
    border-radius: 12px;
}
QLabel#vigilDialogTitle {
    font-size: 18px;
    font-weight: 700;
}
QLabel#vigilDialogMessage, QLabel#vigilDialogDetail, QLabel#vigilDialogError {
    font-size: 12px;
    font-weight: 500;
}
QLabel#vigilDialogDetail {
    font-size: 13px;
    font-weight: 600;
}
QKeySequenceEdit#hotkeySequenceEdit {
    min-height: 48px;
    border-width: 2px;
    border-style: solid;
    border-radius: 8px;
    padding: 0 12px;
    font-size: 15px;
    font-weight: 600;
}
QDialogButtonBox#vigilDialogButtons QPushButton,
QPushButton[vigilDialogButton="true"] {
    min-width: 92px;
    min-height: 40px;
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 0 12px;
    font-size: 13px;
    font-weight: 600;
}
QPushButton[vigilDialogButton="true"][dialogButtonKind="row"] {
    min-width: 0;
    min-height: 48px;
    text-align: left;
}
QDialogButtonBox#vigilDialogButtons QPushButton:focus,
QPushButton[vigilDialogButton="true"]:focus {
    border-width: 3px;
    border-style: solid;
}

QFrame#performanceMetricSelector, QFrame#performanceMetricDetail {
    background-color: transparent;
    border: none;
}
QPushButton#performanceMetricButton {
    border: 1px solid transparent;
    border-radius: 0;
    min-height: 31px;
    padding: 3px 8px;
    text-align: left;
    font-size: 14px;
    font-weight: 700;
}
QPushButton#performanceMetricButton[navigationFocus="true"] {
    border-width: 2px;
    border-style: solid;
}
QLabel#performanceLargeValue {
    font-size: 38px;
    font-weight: 500;
}
QLabel#performanceSecondaryValue {
    font-size: 13px;
}
QLabel#performanceScaleLabel, QLabel#performanceHistoryLabel {
    font-size: 11px;
}
QFrame#overlayStatusCluster {
    background-color: transparent;
    border: none;
}
QLabel#statusGlyph {
    font-size: 24px;
    font-weight: 700;
}
QLabel#overlayClock {
    font-size: 34px;
    font-weight: 700;
}
QPushButton#overlayHideButton {
    border: none;
    border-radius: 5px;
    min-width: 28px;
    min-height: 28px;
    font-size: 22px;
    padding: 0;
}
QWidget#widgetOptionsContainer {
    background-color: transparent;
}
QPushButton#widgetOptionsButton {
    border: none;
    border-radius: 6px;
    min-height: 36px;
    padding: 4px 10px;
    font-size: 14px;
    font-weight: 600;
}
QFrame#widgetOptionsPopup {
    min-width: 170px;
    border-width: 1px;
    border-style: solid;
    border-radius: 8px;
}
QLabel#widgetOptionsTitle {
    font-size: 13px;
    font-weight: 700;
}
QPushButton#widgetOptionsAction {
    min-height: 38px;
    border-width: 2px;
    border-style: solid;
    border-radius: 6px;
    padding: 3px 9px;
    text-align: left;
    font-size: 13px;
    font-weight: 600;
}
"""

_DARK_STYLESHEET = (
    _COMMON_STYLESHEET
    + """
QLabel#overlayBrandTitle {
    color: #f4f5f7;
}
QPushButton#compactWidgetButton, QPushButton#compactPowerButton {
    color: #e7e8eb;
    background-color: rgba(42, 46, 52, 245);
    border: 1px solid #555b65;
}
QPushButton#compactWidgetButton:hover, QPushButton#compactPowerButton:hover {
    background-color: #424750;
}
QPushButton#compactWidgetButton[activeWidget="true"] {
    color: #1c2026;
    background-color: #ececef;
    border-color: #ffffff;
}
QPushButton#compactWidgetButton[navigationFocus="true"],
QPushButton#compactPowerButton[navigationFocus="true"] {
    border-color: #ffffff;
}
QLabel[widgetStripOverflowIndicator="true"] {
    color: #cdd2d9;
}
QScrollArea#widgetPageScroller {
    background-color: rgba(41, 44, 50, 248);
    border: 1px solid #555b65;
}
QScrollArea#widgetPageScroller QScrollBar#widgetPageVerticalScrollBar:vertical {
    background: rgba(225, 228, 233, 42);
}
QScrollArea#widgetPageScroller QScrollBar#widgetPageVerticalScrollBar::handle:vertical {
    background: #d4d8de;
}
QScrollArea#widgetPageScroller QScrollBar#widgetPageVerticalScrollBar::handle:vertical:hover {
    background: #eef0f3;
}
QScrollArea#widgetPageScroller QScrollBar#widgetPageVerticalScrollBar::handle:vertical:pressed {
    background: #ffffff;
}
QLabel#compactPageTitle, QLabel#performanceTitle {
    color: #f4f5f7;
}
QLabel#compactPageDescription, QLabel#compactEmptyState {
    color: #aeb4bd;
}
QPushButton#compactListItem, QPushButton#performanceMetricButton {
    color: #eef0f3;
    background-color: transparent;
}
QPushButton#compactListItem:hover, QPushButton#performanceMetricButton:hover {
    background-color: #3b4048;
}
QPushButton#compactListItem[selectedItem="true"],
QPushButton#performanceMetricButton[selectedItem="true"] {
    background-color: #454a52;
}
QPushButton#compactListItem[navigationFocus="true"],
QPushButton#performanceMetricButton[navigationFocus="true"] {
    border-color: #f1f2f4;
}
QPushButton#compactListItem:disabled, QPushButton#performanceMetricButton:disabled {
    color: #767d87;
}
QPushButton#compactListItemSecondaryAction {
    color: #eef0f3;
    background-color: #454a52;
    border-color: #eef0f3;
}
QPushButton#compactListItemSecondaryAction:hover {
    background-color: #555c66;
}
QPushButton#compactListItemSecondaryAction:pressed {
    background-color: #343941;
}

QLabel#audioTitle, QLabel#audioSectionLabel, QLabel#audioVolumeRowTitle, QLabel#audioVolumeValue,
QLabel#toggleRowTitle {
    color: #f4f5f7;
}
QPushButton#audioToggleButton {
    color: #f4f5f7;
    background-color: transparent;
}
QPushButton#audioSelectorButton, QPushButton#wifiProfileSelectorButton,
QPushButton#displaySelectorButton {
    color: #f4f5f7;
    background-color: rgba(50, 57, 70, 235);
    border-color: #a8e8ff;
}
QPushButton#audioSelectorButton:hover,
QPushButton#audioSelectorButton[selectedItem="true"],
QPushButton#wifiProfileSelectorButton:hover,
QPushButton#wifiProfileSelectorButton[selectedItem="true"],
QPushButton#displaySelectorButton:hover,
QPushButton#displaySelectorButton[selectedItem="true"] {
    background-color: rgba(65, 75, 91, 245);
}
QPushButton#audioVolumeRowButton {
    color: #f4f5f7;
    background-color: transparent;
}
QPushButton#audioToggleButton:hover, QPushButton#audioToggleButton[selectedItem="true"],
QPushButton#audioVolumeRowButton:hover, QPushButton#audioVolumeRowButton[selectedItem="true"] {
    background-color: #454a52;
}
QPushButton#audioToggleButton[navigationFocus="true"],
QPushButton#audioVolumeRowButton[navigationFocus="true"],
QPushButton#audioSelectorButton[navigationFocus="true"],
QPushButton#wifiProfileSelectorButton[navigationFocus="true"],
QPushButton#displaySelectorButton[navigationFocus="true"] {
    border-color: #ffffff;
}
QSlider#audioVolumeSlider::groove:horizontal,
QSlider#audioVolumeSlider::add-page:horizontal {
    background-color: #4c5664;
}
QSlider#audioVolumeSlider::sub-page:horizontal {
    background-color: #6ddcff;
}
QSlider#audioVolumeSlider::handle:horizontal {
    background-color: #a8e8ff;
}
QFrame#audioDropdownPopup, QFrame#wifiDropdownPopup,
QFrame#displayDropdownPopup {
    background-color: #343b47;
    border-color: #a8e8ff;
}
QPushButton#audioDropdownOption, QPushButton#wifiDropdownOption,
QPushButton#displayDropdownOption {
    color: #f4f5f7;
    background-color: rgba(50, 57, 70, 245);
}
QPushButton#audioDropdownOption:hover, QPushButton#audioDropdownOption[navigationFocus="true"],
QPushButton#wifiDropdownOption:hover, QPushButton#wifiDropdownOption[navigationFocus="true"],
QPushButton#displayDropdownOption:hover, QPushButton#displayDropdownOption[navigationFocus="true"] {
    background-color: #505a68;
    border-color: #ffffff;
}
QLabel#audioErrorLabel { color: #ffd4d4; }
QLabel#audioEmptyMixer { color: #aeb4bd; }

QLabel#wifiTitle, QLabel#wifiSectionLabel, QLabel#wifiStatusLabel {
    color: #f4f5f7;
}
QPushButton#wifiToggleButton, QPushButton#wifiActionButton {
    color: #f4f5f7;
    background-color: transparent;
}
QPushButton#wifiToggleButton:hover, QPushButton#wifiToggleButton[selectedItem="true"],
QPushButton#wifiActionButton:hover, QPushButton#wifiActionButton[selectedItem="true"] {
    background-color: #454a52;
}
QPushButton#wifiToggleButton[navigationFocus="true"],
QPushButton#wifiActionButton[navigationFocus="true"] { border-color: #ffffff; }
QLabel#wifiHelpLabel { color: #aeb4bd; }
QLabel#wifiErrorLabel { color: #ffd4d4; }

QLabel#displayTitle, QLabel#displaySectionLabel, QLabel#displayPageHint,
QLabel#displayFieldLabel {
    color: #f4f5f7;
}
QFrame#displaySectionUnderline {
    background-color: #e9faff;
}
QFrame#displayConfirmationPopup, QFrame#integrationConfirmationPopup {
    background-color: #343b47;
    border-color: #a8e8ff;
}
QPushButton#displayConfirmationButton, QPushButton#integrationConfirmationAction {
    color: #f4f5f7;
    background-color: rgba(50, 57, 70, 245);
}
QPushButton#displayConfirmationButton:hover,
QPushButton#displayConfirmationButton[navigationFocus="true"],
QPushButton#integrationConfirmationAction:hover,
QPushButton#integrationConfirmationAction[navigationFocus="true"] {
    background-color: #505a68;
    border-color: #ffffff;
}
QLabel#displayConfirmationTitle, QLabel#displayConfirmationCountdown,
QLabel#integrationConfirmationTitle, QLabel#integrationConfirmationDetail,
QLabel#integrationOperationStatus {
    color: #f4f5f7;
}
QLabel#displayErrorLabel, QLabel#integrationOperationStatus[operationError="true"] {
    color: #ffd4d4;
}

QLabel#settingsTitle,
QLabel#integrationsTitle,
QLabel#settingsSectionLabel,
QLabel#settingsRowTitle,
QLabel#integrationRowTitle {
    color: #f4f5f7;
}
QLabel#settingsRowDescription,
QLabel#settingsRowTrailing,
QLabel#integrationsDescription,
QLabel#integrationRowDescription,
QLabel#integrationRowStatus,
QLabel#integrationRowAction {
    color: #aeb4bd;
}
QFrame#settingsSectionUnderline {
    background-color: #e9faff;
}
QPushButton#settingsRowButton, QPushButton#integrationRowButton {
    color: #f4f5f7;
    background-color: rgba(50, 57, 70, 150);
}
QPushButton#settingsRowButton:hover,
QPushButton#settingsRowButton[selectedItem="true"],
QPushButton#integrationRowButton:hover,
QPushButton#integrationRowButton[selectedItem="true"] {
    background-color: rgba(65, 75, 91, 220);
}
QPushButton#settingsRowButton[navigationFocus="true"],
QPushButton#integrationRowButton[navigationFocus="true"] {
    border-color: #ffffff;
}
QPushButton#settingsRowButton:disabled, QPushButton#integrationRowButton:disabled {
    background-color: rgba(50, 57, 70, 85);
}
QPushButton#settingsRowButton:disabled QLabel, QPushButton#integrationRowButton:disabled QLabel {
    color: #767d87;
}
QFrame#vigilDialogSurface {
    color: #f4f5f7;
    background-color: rgba(41, 44, 50, 252);
    border-color: #555b65;
}
QLabel#vigilDialogTitle {
    color: #f4f5f7;
}
QLabel#vigilDialogMessage, QLabel#vigilDialogDetail {
    color: #aeb4bd;
}
QLabel#vigilDialogError {
    color: #ffd4d4;
}
QKeySequenceEdit#hotkeySequenceEdit {
    color: #f4f5f7;
    background-color: #323946;
    border-color: #697383;
}
QKeySequenceEdit#hotkeySequenceEdit:focus {
    border-color: #ffffff;
}
QDialogButtonBox#vigilDialogButtons QPushButton,
QPushButton[vigilDialogButton="true"] {
    color: #eef0f3;
    background-color: #323946;
    border-color: #697383;
}
QPushButton[vigilDialogButton="true"][dialogButtonKind="row"] {
    background-color: transparent;
}
QDialogButtonBox#vigilDialogButtons QPushButton:hover,
QDialogButtonBox#vigilDialogButtons QPushButton:focus,
QPushButton[vigilDialogButton="true"]:hover,
QPushButton[vigilDialogButton="true"]:focus {
    background-color: #454a52;
    border-color: #f1f2f4;
}
QDialogButtonBox#vigilDialogButtons QPushButton:pressed,
QPushButton[vigilDialogButton="true"]:pressed {
    background-color: #343941;
}
QDialogButtonBox#vigilDialogButtons QPushButton:disabled,
QPushButton[vigilDialogButton="true"]:disabled {
    color: #767d87;
    background-color: rgba(50, 57, 70, 110);
    border-color: rgba(105, 115, 131, 130);
}

QLabel#performanceLargeValue {
    color: #f0f1f3;
}
QLabel#performanceSecondaryValue, QLabel#performanceScaleLabel,
QLabel#performanceHistoryLabel {
    color: #9da4ae;
}
QLabel#statusGlyph, QLabel#overlayClock {
    color: #ffffff;
}
QPushButton#overlayHideButton {
    color: #ffffff;
    background-color: transparent;
}
QPushButton#overlayHideButton:hover {
    background-color: rgba(72, 78, 87, 220);
}
QPushButton#widgetOptionsButton {
    color: #f4f5f7;
    background-color: transparent;
}
QPushButton#widgetOptionsButton:hover {
    background-color: rgba(72, 78, 87, 220);
}
QFrame#widgetOptionsPopup {
    color: #f4f5f7;
    background-color: rgba(52, 59, 71, 248);
    border-color: #626a76;
}
QLabel#widgetOptionsTitle {
    color: #f4f5f7;
}
QPushButton#widgetOptionsAction {
    color: #f4f5f7;
    background-color: rgba(64, 70, 81, 245);
    border-color: #dce0e5;
}
QPushButton#widgetOptionsAction:hover,
QPushButton#widgetOptionsAction[navigationFocus="true"] {
    background-color: #505866;
    border-color: #ffffff;
}
"""
)

_LIGHT_STYLESHEET = (
    _COMMON_STYLESHEET
    + """
QLabel#overlayBrandTitle {
    color: #20242b;
}
QPushButton#compactWidgetButton, QPushButton#compactPowerButton {
    color: #20242b;
    background-color: rgba(229, 232, 236, 248);
    border: 1px solid #aeb4bd;
}
QPushButton#compactWidgetButton:hover, QPushButton#compactPowerButton:hover {
    background-color: #f4f5f7;
}
QPushButton#compactWidgetButton[activeWidget="true"] {
    color: #ffffff;
    background-color: #343a43;
    border-color: #1d2127;
}
QPushButton#compactWidgetButton[navigationFocus="true"],
QPushButton#compactPowerButton[navigationFocus="true"] {
    border-color: #ffffff;
}
QPushButton#compactPowerButton[navigationFocus="true"] {
    border-color: #343a43;
}
QLabel[widgetStripOverflowIndicator="true"] {
    color: #4f5864;
}
QScrollArea#widgetPageScroller {
    background-color: rgba(224, 227, 232, 248);
    border: 1px solid #aeb4bd;
}
QScrollArea#widgetPageScroller QScrollBar#widgetPageVerticalScrollBar:vertical {
    background: rgba(72, 79, 89, 36);
}
QScrollArea#widgetPageScroller QScrollBar#widgetPageVerticalScrollBar::handle:vertical {
    background: #7d8793;
}
QScrollArea#widgetPageScroller QScrollBar#widgetPageVerticalScrollBar::handle:vertical:hover {
    background: #626b76;
}
QScrollArea#widgetPageScroller QScrollBar#widgetPageVerticalScrollBar::handle:vertical:pressed {
    background: #4f5761;
}
QLabel#compactPageTitle, QLabel#performanceTitle {
    color: #171a1f;
}
QLabel#compactPageDescription, QLabel#compactEmptyState {
    color: #555d68;
}
QPushButton#compactListItem, QPushButton#performanceMetricButton {
    color: #20242b;
    background-color: transparent;
}
QPushButton#compactListItem:hover, QPushButton#performanceMetricButton:hover {
    background-color: #f1f3f5;
}
QPushButton#compactListItem[selectedItem="true"],
QPushButton#performanceMetricButton[selectedItem="true"] {
    background-color: #cbd0d7;
}
QPushButton#compactListItem[navigationFocus="true"],
QPushButton#performanceMetricButton[navigationFocus="true"] {
    border-color: #343a43;
}
QPushButton#compactListItem:disabled, QPushButton#performanceMetricButton:disabled {
    color: #838a94;
}
QPushButton#compactListItemSecondaryAction {
    color: #20242b;
    background-color: #e4e7eb;
    border-color: #343a43;
}
QPushButton#compactListItemSecondaryAction:hover {
    background-color: #d4d8de;
}
QPushButton#compactListItemSecondaryAction:pressed {
    background-color: #c4c9d0;
}

QLabel#audioTitle, QLabel#audioSectionLabel, QLabel#audioVolumeRowTitle, QLabel#audioVolumeValue,
QLabel#toggleRowTitle {
    color: #20242a;
}
QPushButton#audioToggleButton {
    color: #20242a;
    background-color: transparent;
}
QPushButton#audioSelectorButton, QPushButton#wifiProfileSelectorButton,
QPushButton#displaySelectorButton {
    color: #20242b;
    background-color: rgba(244, 246, 249, 248);
    border-color: #5d7481;
}
QPushButton#audioSelectorButton:hover,
QPushButton#audioSelectorButton[selectedItem="true"],
QPushButton#wifiProfileSelectorButton:hover,
QPushButton#wifiProfileSelectorButton[selectedItem="true"],
QPushButton#displaySelectorButton:hover,
QPushButton#displaySelectorButton[selectedItem="true"] {
    background-color: #ffffff;
}
QPushButton#audioVolumeRowButton { color: #20242a; background-color: transparent; }
QPushButton#audioToggleButton:hover,
QPushButton#audioToggleButton[selectedItem="true"],
QPushButton#audioVolumeRowButton:hover,
QPushButton#audioVolumeRowButton[selectedItem="true"] {
    background-color: #e4e8ed;
}
QPushButton#audioToggleButton[navigationFocus="true"],
QPushButton#audioVolumeRowButton[navigationFocus="true"],
QPushButton#audioSelectorButton[navigationFocus="true"],
QPushButton#wifiProfileSelectorButton[navigationFocus="true"],
QPushButton#displaySelectorButton[navigationFocus="true"] {
    border-color: #171a1f;
}
QSlider#audioVolumeSlider::groove:horizontal,
QSlider#audioVolumeSlider::add-page:horizontal { background-color: #cbd2da; }
QSlider#audioVolumeSlider::sub-page:horizontal { background-color: #277da1; }
QSlider#audioVolumeSlider::handle:horizontal { background-color: #277da1; }
QFrame#audioDropdownPopup, QFrame#wifiDropdownPopup,
QFrame#displayDropdownPopup {
    background-color: #f4f6f9;
    border-color: #5d7481;
}
QPushButton#audioDropdownOption, QPushButton#wifiDropdownOption,
QPushButton#displayDropdownOption {
    color: #20242b;
    background-color: #ffffff;
}
QPushButton#audioDropdownOption:hover,
QPushButton#audioDropdownOption[navigationFocus="true"],
QPushButton#wifiDropdownOption:hover,
QPushButton#wifiDropdownOption[navigationFocus="true"],
QPushButton#displayDropdownOption:hover,
QPushButton#displayDropdownOption[navigationFocus="true"] {
    background-color: #e5e9ee;
    border-color: #171a1f;
}
QLabel#audioErrorLabel { color: #8a1f1f; }
QLabel#audioEmptyMixer { color: #5d6670; }

QLabel#wifiTitle, QLabel#wifiSectionLabel, QLabel#wifiStatusLabel {
    color: #20242a;
}
QPushButton#wifiToggleButton, QPushButton#wifiActionButton {
    color: #20242a;
    background-color: transparent;
}
QPushButton#wifiToggleButton:hover, QPushButton#wifiToggleButton[selectedItem="true"],
QPushButton#wifiActionButton:hover, QPushButton#wifiActionButton[selectedItem="true"] {
    background-color: #e4e8ed;
}
QPushButton#wifiToggleButton[navigationFocus="true"],
QPushButton#wifiActionButton[navigationFocus="true"] { border-color: #111111; }
QLabel#wifiHelpLabel { color: #5d6670; }
QLabel#wifiErrorLabel { color: #8a1f1f; }

QLabel#displayTitle, QLabel#displaySectionLabel, QLabel#displayPageHint,
QLabel#displayFieldLabel {
    color: #171a1f;
}
QFrame#displaySectionUnderline {
    background-color: #343a43;
}
QFrame#displayConfirmationPopup, QFrame#integrationConfirmationPopup {
    background-color: #f4f6f9;
    border-color: #5d7481;
}
QPushButton#displayConfirmationButton, QPushButton#integrationConfirmationAction {
    color: #20242b;
    background-color: #ffffff;
}
QPushButton#displayConfirmationButton:hover,
QPushButton#displayConfirmationButton[navigationFocus="true"],
QPushButton#integrationConfirmationAction:hover,
QPushButton#integrationConfirmationAction[navigationFocus="true"] {
    background-color: #e5e9ee;
    border-color: #171a1f;
}
QLabel#displayConfirmationTitle, QLabel#displayConfirmationCountdown,
QLabel#integrationConfirmationTitle, QLabel#integrationConfirmationDetail,
QLabel#integrationOperationStatus {
    color: #171a1f;
}
QLabel#displayErrorLabel, QLabel#integrationOperationStatus[operationError="true"] {
    color: #8a1f1f;
}

QLabel#settingsTitle,
QLabel#integrationsTitle,
QLabel#settingsSectionLabel,
QLabel#settingsRowTitle,
QLabel#integrationRowTitle {
    color: #171a1f;
}
QLabel#settingsRowDescription,
QLabel#settingsRowTrailing,
QLabel#integrationsDescription,
QLabel#integrationRowDescription,
QLabel#integrationRowStatus,
QLabel#integrationRowAction {
    color: #5a626d;
}
QFrame#settingsSectionUnderline {
    background-color: #343a43;
}
QPushButton#settingsRowButton, QPushButton#integrationRowButton {
    color: #20242b;
    background-color: rgba(244, 246, 249, 190);
}
QPushButton#settingsRowButton:hover,
QPushButton#settingsRowButton[selectedItem="true"],
QPushButton#integrationRowButton:hover,
QPushButton#integrationRowButton[selectedItem="true"] {
    background-color: #ffffff;
}
QPushButton#settingsRowButton[navigationFocus="true"],
QPushButton#integrationRowButton[navigationFocus="true"] {
    border-color: #171a1f;
}
QPushButton#settingsRowButton:disabled, QPushButton#integrationRowButton:disabled {
    background-color: rgba(224, 227, 232, 150);
}
QPushButton#settingsRowButton:disabled QLabel, QPushButton#integrationRowButton:disabled QLabel {
    color: #838a94;
}
QFrame#vigilDialogSurface {
    color: #171a1f;
    background-color: rgba(224, 227, 232, 252);
    border-color: #aeb4bd;
}
QLabel#vigilDialogTitle {
    color: #171a1f;
}
QLabel#vigilDialogMessage, QLabel#vigilDialogDetail {
    color: #5a626d;
}
QLabel#vigilDialogError {
    color: #8a1f1f;
}
QKeySequenceEdit#hotkeySequenceEdit {
    color: #20242b;
    background-color: #ffffff;
    border-color: #aab0b8;
}
QKeySequenceEdit#hotkeySequenceEdit:focus {
    border-color: #171a1f;
}
QDialogButtonBox#vigilDialogButtons QPushButton,
QPushButton[vigilDialogButton="true"] {
    color: #20242b;
    background-color: #ffffff;
    border-color: #aab0b8;
}
QPushButton[vigilDialogButton="true"][dialogButtonKind="row"] {
    background-color: transparent;
}
QDialogButtonBox#vigilDialogButtons QPushButton:hover,
QDialogButtonBox#vigilDialogButtons QPushButton:focus,
QPushButton[vigilDialogButton="true"]:hover,
QPushButton[vigilDialogButton="true"]:focus {
    background-color: #cbd0d7;
    border-color: #343a43;
}
QDialogButtonBox#vigilDialogButtons QPushButton:pressed,
QPushButton[vigilDialogButton="true"]:pressed {
    background-color: #b9c0c9;
}
QDialogButtonBox#vigilDialogButtons QPushButton:disabled,
QPushButton[vigilDialogButton="true"]:disabled {
    color: #838a94;
    background-color: rgba(224, 227, 232, 150);
    border-color: rgba(170, 176, 184, 150);
}

QLabel#performanceLargeValue {
    color: #171a1f;
}
QLabel#performanceSecondaryValue, QLabel#performanceScaleLabel,
QLabel#performanceHistoryLabel {
    color: #5a626d;
}
QLabel#statusGlyph, QLabel#overlayClock {
    color: #171a1f;
}
QPushButton#overlayHideButton {
    color: #171a1f;
    background-color: transparent;
}
QPushButton#overlayHideButton:hover {
    background-color: rgba(203, 208, 215, 220);
}
QPushButton#widgetOptionsButton {
    color: #171a1f;
    background-color: transparent;
}
QPushButton#widgetOptionsButton:hover {
    background-color: rgba(203, 208, 215, 220);
}
QFrame#widgetOptionsPopup {
    color: #171a1f;
    background-color: rgba(244, 246, 249, 248);
    border-color: #aeb4bd;
}
QLabel#widgetOptionsTitle {
    color: #171a1f;
}
QPushButton#widgetOptionsAction {
    color: #171a1f;
    background-color: #ffffff;
    border-color: #5d6672;
}
QPushButton#widgetOptionsAction:hover,
QPushButton#widgetOptionsAction[navigationFocus="true"] {
    background-color: #e5e9ee;
    border-color: #171a1f;
}
"""
)


def apply_host_theme(application: QApplication, requested_theme: str) -> str:
    """Apply the host theme and return the resolved theme name."""

    theme = requested_theme
    if requested_theme == "system":
        window_color = application.palette().color(QPalette.ColorRole.Window)
        theme = "dark" if window_color.lightness() < 128 else "light"

    application.setProperty("vigilResolvedTheme", theme)
    application.setStyleSheet(_DARK_STYLESHEET if theme == "dark" else _LIGHT_STYLESHEET)
    return theme
